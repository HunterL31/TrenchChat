"""
Announce-based peer presence tracking, plus an application-level beacon.

A peer is considered "online" if we last heard from them (announce or any
inbound LXMF message) within PRESENCE_TIMEOUT_SECS. Announces alone are not
reliable evidence of this in a multi-hop mesh: a transport node damps
repeat announces for a destination it already has a path to, so a fresh
observer behind a transport node can go tens of minutes without hearing one
even though the peer is online. PresenceBeacon below compensates by sending
a small signed LXMF packet -- not subject to that damping -- to channel
peers we have gone quiet with.

This module has no network side-effects of its own beyond PresenceBeacon's
sends -- PresenceManager only records timestamps from announces and messages
that are already being received elsewhere.
"""

import random
import time
import threading

import RNS
import LXMF
import msgpack

from trenchchat.core.actions import compute_channel_recipients
from trenchchat.core.protocol import F_MSG_TYPE, MT_GOODBYE, MT_PRESENCE, unpack_wire

PRESENCE_TIMEOUT_SECS = 300

# A peer is beaconed only after this long without inbound evidence of them --
# an active conversation never triggers a beacon. Leaves a 2-minute margin
# for the beacon to arrive before PRESENCE_TIMEOUT_SECS expires.
PRESENCE_BEACON_AFTER_SECS = 180

# Per-peer jitter applied to the beacon-after threshold, as a fraction of it,
# so peers that went quiet together don't beacon each other in lockstep.
PRESENCE_BEACON_JITTER_FRACTION = 0.2

# How long announce_offline waits for its goodbyes to leave the process before
# giving up on the stragglers. LXMF sends are asynchronous, so quitting straight
# after handing them over would kill the process before anything went out.
GOODBYE_DRAIN_SECS = 2.0
GOODBYE_DRAIN_POLL_SECS = 0.05

# An LXMessage in any of these has stopped moving -- nothing more will happen to
# it without another send.
_TERMINAL_SEND_STATES = (
    LXMF.LXMessage.SENT,
    LXMF.LXMessage.DELIVERED,
    LXMF.LXMessage.FAILED,
    LXMF.LXMessage.REJECTED,
    LXMF.LXMessage.CANCELLED,
)


def resolve_display_name(identity_hex: str, self_hex: str, storage, config=None) -> str:
    """Return the best available display name for a peer identity.

    Resolution order:
      1. Members table (any channel) -- name from a published member list
      2. LXMF announce app_data -- name the peer broadcasts in their announce
      3. Identity hash prefix -- consistent fallback used across the UI
    """
    if identity_hex == self_hex:
        name = (config.display_name if config else None)
        return name or identity_hex[:12] + "\u2026"

    # 1. Storage lookup across all channels
    try:
        stored = storage.get_display_name_for_identity(identity_hex)
        if stored:
            return stored
    except Exception:
        pass

    # 2. LXMF announce app_data -- packed as [display_name_bytes, stamp_cost]
    try:
        identity_bytes = bytes.fromhex(identity_hex)
        # recall() needs a delivery destination hash, not a raw identity hash
        delivery_hash = RNS.Destination.hash_from_name_and_identity(
            "lxmf.delivery", identity_bytes
        )
        raw = RNS.Identity.recall_app_data(delivery_hash)
        if raw:
            parsed = unpack_wire(raw)
            if isinstance(parsed, list) and len(parsed) >= 1:
                name = parsed[0]
            elif isinstance(parsed, dict):
                name = parsed.get("display_name") or parsed.get("name")
            else:
                name = None
            if isinstance(name, bytes):
                name = name.decode(errors="replace")
            if name:
                return str(name)
    except Exception:
        pass

    # 3. Hash prefix fallback
    return identity_hex[:12] + "\u2026"


class PresenceManager:
    """Tracks peer online/offline status based on LXMF delivery announces."""

    def __init__(self, self_hex: str, config=None,
                timeout_secs: float = PRESENCE_TIMEOUT_SECS):
        self._self_hex = self_hex
        self._config = config  # optional Config; used to read current display_name
        self._timeout = timeout_secs
        # identity_hash_hex -> last announce timestamp
        self._last_seen: dict[str, float] = {}
        self._lock = threading.Lock()
        self._callbacks: list = []
        self._seen_callbacks: list = []

    # --- public API ---

    def add_presence_callback(self, cb) -> None:
        """Register a callback invoked with (peer_hex: str, is_online: bool) on status change."""
        self._callbacks.append(cb)

    def add_seen_callback(self, cb) -> None:
        """Register a callback invoked with (peer_hex: str) on every record_seen,
        not just online transitions."""
        self._seen_callbacks.append(cb)

    def record_seen(self, peer_hex: str) -> None:
        """Record that a peer announced their delivery destination right now."""
        if peer_hex == self._self_hex:
            return
        with self._lock:
            was_online = self._is_online_locked(peer_hex)
            self._last_seen[peer_hex] = time.time()
            became_online = not was_online
        if became_online:
            RNS.log(f"TrenchChat [presence]: peer online {peer_hex[:12]}…", RNS.LOG_DEBUG)
            self._fire_callbacks(peer_hex, True)
        self._fire_seen_callbacks(peer_hex)

    def record_offline(self, peer_hex: str) -> None:
        """Mark a peer offline now, on their graceful-shutdown notice.

        Deliberately leaves no trace behind: the next record_seen brings the
        peer straight back online, so a shutdown that gets cancelled -- or a
        client that restarts immediately -- recovers with no special handling.
        """
        if peer_hex == self._self_hex:
            return
        with self._lock:
            was_online = self._is_online_locked(peer_hex)
            self._last_seen.pop(peer_hex, None)
        if was_online:
            RNS.log(f"TrenchChat [presence]: peer signed off {peer_hex[:12]}…", RNS.LOG_DEBUG)
            self._fire_callbacks(peer_hex, False)

    def record_inbound(self, message: LXMF.LXMessage) -> str | None:
        """Record what an inbound message says about its sender's presence, and
        return their identity hash hex.

        A goodbye marks the sender offline; anything else is evidence they are
        alive. Both verdicts are decided here so the two never race: every
        inbound message reaches presence through this one call.

        Returns None if the sender's identity can't be resolved, since
        _last_seen is keyed by identity hash and source_hash is a delivery hash.
        """
        if not message.source_hash:
            return None
        sender_identity = RNS.Identity.recall(message.source_hash)
        if sender_identity is None:
            return None
        sender_hex = sender_identity.hash.hex()

        fields = message.fields or {}
        msg_type = fields.get(F_MSG_TYPE)
        if isinstance(msg_type, bytes):
            msg_type = msg_type.decode(errors="replace")

        if msg_type == MT_GOODBYE:
            self.record_offline(sender_hex)
        else:
            self.record_seen(sender_hex)
        return sender_hex

    def is_online(self, peer_hex: str) -> bool:
        """Return True if the peer is considered online (including self)."""
        if peer_hex == self._self_hex:
            return True
        with self._lock:
            return self._is_online_locked(peer_hex)

    def last_seen_at(self, peer_hex: str) -> float:
        """Unix timestamp of the last inbound evidence from peer_hex, or 0.0
        if none has ever been recorded. Inbound evidence only -- our own
        outbound sends never touch this."""
        with self._lock:
            return self._last_seen.get(peer_hex, 0.0)

    def get_online_peers(self) -> set[str]:
        """Return the set of identity hashes currently considered online (excluding self)."""
        now = time.time()
        with self._lock:
            return {
                hex_id
                for hex_id, ts in self._last_seen.items()
                if now - ts < self._timeout
            }

    def get_online_for_channel(
        self,
        channel_hash_hex: str,
        storage,
        subscription_mgr,
    ) -> list[dict]:
        """
        Return a list of dicts describing members/subscribers for a channel,
        with their online status.

        Each dict has keys: identity_hash, display_name, is_online.

        For invite-only channels: all members are listed (online + offline).
        For public channels: only currently-online subscribers are listed
        (the full subscriber list is only available to the channel owner).
        """
        from trenchchat.core.permissions import is_open_join, permissions_from_json

        channel = storage.get_channel(channel_hash_hex)
        if channel is None:
            return []

        perms = permissions_from_json(channel["permissions"])
        results: list[dict] = []

        if is_open_join(perms):
            online = self.get_online_peers()
            all_peers = set(online)
            all_peers.add(self._self_hex)
            subs = subscription_mgr.get_subscribers(channel_hash_hex)
            for peer_hex in all_peers:
                if peer_hex not in subs and peer_hex != self._self_hex:
                    continue
                results.append({
                    "identity_hash": peer_hex,
                    "display_name": self._resolve_display_name(peer_hex, storage),
                    "is_online": self.is_online(peer_hex),
                })
        else:
            members = storage.get_members(channel_hash_hex)
            for row in members:
                peer_hex = row["identity_hash"]
                # Prefer the stored member name; fall back to announce app_data
                display = (row["display_name"]
                           or self._resolve_display_name(peer_hex, storage))
                results.append({
                    "identity_hash": peer_hex,
                    "display_name": display,
                    "is_online": self.is_online(peer_hex),
                })

        results.sort(key=lambda r: (not r["is_online"], r["display_name"].lower()))
        return results

    # --- private helpers ---

    def _resolve_display_name(self, identity_hex: str, storage) -> str:
        """Return the best available display name for a peer identity."""
        return resolve_display_name(identity_hex, self._self_hex, storage, self._config)

    def prune(self) -> None:
        """Remove stale entries and fire callbacks for peers that went offline."""
        now = time.time()
        went_offline: list[str] = []
        with self._lock:
            stale = [
                hex_id
                for hex_id, ts in self._last_seen.items()
                if now - ts >= self._timeout
            ]
            for hex_id in stale:
                del self._last_seen[hex_id]
                went_offline.append(hex_id)

        for hex_id in went_offline:
            RNS.log(f"TrenchChat [presence]: peer offline {hex_id[:12]}…", RNS.LOG_DEBUG)
            self._fire_callbacks(hex_id, False)

    # --- private helpers ---

    def _is_online_locked(self, peer_hex: str) -> bool:
        """Must be called with self._lock held."""
        ts = self._last_seen.get(peer_hex)
        if ts is None:
            return False
        return time.time() - ts < self._timeout

    def _fire_callbacks(self, peer_hex: str, is_online: bool) -> None:
        for cb in self._callbacks:
            try:
                cb(peer_hex, is_online)
            except Exception as e:
                RNS.log(f"TrenchChat [presence]: callback error: {e}", RNS.LOG_ERROR)

    def _fire_seen_callbacks(self, peer_hex: str) -> None:
        for cb in self._seen_callbacks:
            try:
                cb(peer_hex)
            except Exception as e:
                RNS.log(f"TrenchChat [presence]: seen callback error: {e}", RNS.LOG_ERROR)


class PresenceBeacon:
    """Sends a signed MT_PRESENCE liveness beacon to channel peers we have
    gone quiet with, so presence survives transport nodes that damp repeat
    announces (see module docstring).

    Two clocks matter here and must stay separate: PresenceManager's
    last_seen (inbound evidence a peer is alive) and this class's last_sent
    (our own outbound traffic, which only suppresses our beacon -- it must
    never make a peer appear online).
    """

    def __init__(self, identity, storage, router, subscription_mgr, presence_mgr,
                beacon_after_secs: float = PRESENCE_BEACON_AFTER_SECS,
                jitter_fraction: float = PRESENCE_BEACON_JITTER_FRACTION):
        self._identity = identity
        self._storage = storage
        self._router = router
        self._subscription_mgr = subscription_mgr
        self._presence_mgr = presence_mgr
        self._beacon_after = beacon_after_secs
        self._jitter_fraction = jitter_fraction
        self._last_sent: dict[str, float] = {}
        self._jitter: dict[str, float] = {}
        self._lock = threading.Lock()

    def record_sent(self, peer_hex: str) -> None:
        """Record outbound traffic to peer_hex. Suppresses our beacon to them
        only -- never treat this as evidence they are online."""
        with self._lock:
            self._last_sent[peer_hex] = time.time()

    def tick(self) -> None:
        """Beacon every channel peer gone quiet for longer than their
        (jittered) threshold. Call this periodically from the same loop that
        already prunes presence -- no dedicated timer needed."""
        now = time.time()
        for peer_hex in self._channel_peers():
            if now - self._quiet_since(peer_hex) >= self._threshold(peer_hex):
                self._send_beacon(peer_hex)

    def announce_offline(self, drain_secs: float = GOODBYE_DRAIN_SECS) -> int:
        """Tell every channel peer we are shutting down, so they can drop us to
        offline now rather than waiting out PRESENCE_TIMEOUT_SECS.

        Blocks until the sends stop moving or drain_secs elapses, then returns
        how many left the process. Best-effort: a peer whose path we don't hold,
        or whose link doesn't come up in time, simply times us out as before.
        """
        sent = [
            lxm for lxm in (
                self._send_presence(peer_hex, MT_GOODBYE)
                for peer_hex in self._channel_peers()
            )
            if lxm is not None
        ]
        if not sent:
            return 0

        deadline = time.time() + drain_secs
        while time.time() < deadline:
            if all(getattr(lxm, "state", None) in _TERMINAL_SEND_STATES for lxm in sent):
                break
            time.sleep(GOODBYE_DRAIN_POLL_SECS)

        delivered = sum(
            1 for lxm in sent if getattr(lxm, "state", None) in _TERMINAL_SEND_STATES
        )
        RNS.log(
            f"TrenchChat [presence]: sent going-offline notice to "
            f"{delivered}/{len(sent)} peers",
            RNS.LOG_NOTICE,
        )
        return delivered

    # --- private helpers ---

    def _quiet_since(self, peer_hex: str) -> float:
        with self._lock:
            last_sent = self._last_sent.get(peer_hex, 0.0)
        return max(self._presence_mgr.last_seen_at(peer_hex), last_sent)

    def _threshold(self, peer_hex: str) -> float:
        with self._lock:
            jitter = self._jitter.get(peer_hex)
            if jitter is None:
                spread = self._beacon_after * self._jitter_fraction
                jitter = random.uniform(-spread, spread)
                self._jitter[peer_hex] = jitter
        return max(0.0, self._beacon_after + jitter)

    def _channel_peers(self) -> set[str]:
        peers: set[str] = set()
        for sub in self._storage.get_subscriptions():
            peers.update(compute_channel_recipients(
                self._storage, self._subscription_mgr, sub["channel_hash"],
                self._identity.hash_hex,
            ))
        peers.discard(self._identity.hash_hex)
        return peers

    def _send_beacon(self, peer_hex: str) -> None:
        if self._send_presence(peer_hex, MT_PRESENCE) is None:
            return
        RNS.log(f"TrenchChat [presence]: beacon sent to {peer_hex[:12]}…", RNS.LOG_DEBUG)

    def _send_presence(self, peer_hex: str, msg_type: str) -> "LXMF.LXMessage | None":
        """Send a one-field presence control message. Returns the message, or
        None if the peer's path isn't known yet."""
        identity_hash = bytes.fromhex(peer_hex)
        delivery_dest_hash = RNS.Destination.hash(identity_hash, "lxmf", "delivery")
        dest_identity = RNS.Identity.recall(delivery_dest_hash)
        if dest_identity is None:
            RNS.Transport.request_path(delivery_dest_hash)
            return None

        dest = RNS.Destination(
            dest_identity,
            RNS.Destination.OUT,
            RNS.Destination.SINGLE,
            "lxmf",
            "delivery",
        )
        lxm = LXMF.LXMessage(
            dest,
            self._router.delivery_destination,
            "",
            desired_method=LXMF.LXMessage.DIRECT,
        )
        lxm.fields = {F_MSG_TYPE: msg_type}
        self._router.send(lxm)
        self.record_sent(peer_hex)
        return lxm
