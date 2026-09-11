"""
The Reticulum path: everything TrenchChat knows about RNS and LXMF lives here.

This is the only module that builds an LXMessage, calls Identity.recall,
registers an announce handler or owns an RNS destination. A manager above the
seam names a peer by identity hash and gets a SendState back; the delivery
destination hash a peer is addressed by is this file's private alias for that
identity, mapped at the edge in both directions and never handed upward.

It is also the single choke point for inbound authentication: no message
reaches Router unless its LXMF signature validated. Messages whose source
identity is not yet known are held in a bounded quarantine and re-validated
from their packed bytes once the identity resolves.
"""

import threading
import time

import LXMF
import RNS

from trenchchat import APP_NAME, APP_ASPECT_CHANNEL, APP_ASPECT_USER
from trenchchat.config import Config, DATA_DIR
from trenchchat.core.protocol import pack_fields
from trenchchat.network.announce import (
    ChannelAnnounceHandler, FirstContactAnnouncer, NodeAnnounceHandler,
    PathResponseHandler, PeerAnnounceHandler, PropagationAnnounceHandler,
    UserAnnounceHandler,
)
from trenchchat.network.base import (
    InboundMessage, PATH_RETICULUM, SendState, Transport, TransportLimits,
    reticulum_limits,
)

_MESSAGE_STORE_PATH = str(DATA_DIR / "messagestore")

# Quarantine bounds for messages awaiting sender-identity resolution.  Both a
# per-sender and a global cap apply so the quarantine cannot itself be used as
# a memory-exhaustion vector by a peer that never announces.
QUARANTINE_TTL_SECS = 300
QUARANTINE_MAX_PER_SENDER = 8
QUARANTINE_MAX_TOTAL = 128

# Ceiling on path requests issued for unknown quarantine sources. These fire
# before authentication -- source_hash is attacker-chosen wire data -- so
# without a bound each unsigned packet turns into a broadcast on the shared
# mesh, one for one.
PATH_REQUEST_WINDOW_SECS = 60.0
PATH_REQUEST_BURST = 12
PATH_REQUEST_MAX_SOURCES = 256

# Global ceiling on quarantine path requests, whatever source they claim. The
# per-source bucket is keyed on wire data an unauthenticated sender chooses, so
# rotating it makes every bucket fresh and restores the one-broadcast-per-packet
# amplification the per-source limit exists to prevent. This is the bound that
# actually holds; the per-source one only paces a single honest peer.
PATH_REQUEST_GLOBAL_BURST = 60

# How often every entrypoint re-announces (delivery + user + owned channels).
# Transport nodes cap forwarded announces at 2% of interface bitrate and drop
# repeats, so a fast heartbeat mostly burns first-hop airtime: at the old 60s
# it cost ~41 kB/h idle (~8% duty cycle on 1.2 kbps LoRa). Three hours sits
# inside Sideband's 90-300 min range, short of NomadNet's 6 h. Meeting a peer
# does not depend on it (FirstContactAnnouncer answers the first time we hear
# one), and neither does reconnect catch-up -- peer announces, inbound
# messages, and LinkWatcher all drive that.
REANNOUNCE_INTERVAL_SECS = 3 * 3600.0

# How often the re-announce thread wakes to coalesce first-contact answers.
FIRST_CONTACT_TICK_SECS = 1.0

# Outbound messages watched for drain(). Terminal ones are dropped on every
# send, so this only ever holds what is genuinely still moving.
MAX_TRACKED_OUTBOUND = 256

# An LXMessage in any of these has stopped moving -- nothing more will happen to
# it without another send.
_TERMINAL_SEND_STATES = (
    LXMF.LXMessage.SENT,
    LXMF.LXMessage.DELIVERED,
    LXMF.LXMessage.FAILED,
    LXMF.LXMessage.REJECTED,
    LXMF.LXMessage.CANCELLED,
)

_DRAIN_POLL_SECS = 0.05


def delivery_hash_for_identity(identity_hash: bytes) -> bytes:
    """Return the LXMF delivery destination hash for an identity hash."""
    return RNS.Destination.hash(identity_hash, "lxmf", "delivery")


class LXMFTransport(Transport):
    """TrenchChat over Reticulum and LXMF."""

    def __init__(self, config: Config, identity, storagepath: str | None = None):
        """
        identity: trenchchat.core.identity.Identity instance
        (passed in to avoid circular imports)
        storagepath: override for the LXMF message store directory
        """
        self._config = config
        self._identity = identity
        self._inbound_callback = None
        # source_hash hex -> list of (received_at, LXMessage) awaiting identity
        self._quarantine: dict[str, list] = {}
        self._quarantine_lock = threading.Lock()
        # source_hash hex -> recent quarantine path-request timestamps
        self._path_request_rate: dict[str, list] = {}
        # Its own bucket, never the per-source one: key eviction there could
        # drop the global counter and reset the ceiling it enforces.
        self._path_request_global: dict[str, list] = {}
        self._path_request_lock = threading.Lock()
        # channel hash hex -> the RNS destination this node owns for it
        self._channel_destinations: dict = {}
        self._outbound: list = []
        self._outbound_lock = threading.Lock()
        self._announce_all = None
        self._reannounce_stop = threading.Event()

        self._router = LXMF.LXMRouter(
            storagepath=storagepath or _MESSAGE_STORE_PATH,
            identity=identity.rns_identity,
            name=config.propagation_node_name or None,
        )

        # Register our delivery destination with the router.
        self._delivery_dest = self._router.register_delivery_identity(
            identity.rns_identity,
            display_name=config.display_name,
        )

        # Register a dedicated trenchchat.user destination so TrenchChat peers
        # can be distinguished from generic LXMF clients on the network.
        self._user_dest = RNS.Destination(
            identity.rns_identity,
            RNS.Destination.IN,
            RNS.Destination.SINGLE,
            APP_NAME,
            APP_ASPECT_USER,
        )

        self._router.register_delivery_callback(self._on_message_received)

        self._first_contact = FirstContactAnnouncer(
            self._announce_everything, identity.hash_hex,
        )
        self._register_announce_handlers()

        # Enable propagation node mode if configured.
        if config.propagation_enabled:
            self.enable_propagation()

    # --- inbound ---

    def set_inbound_callback(self, callback) -> None:
        """Register the single callback every authenticated message arrives on."""
        self._inbound_callback = callback

    def _on_message_received(self, message: LXMF.LXMessage):
        """Called by LXMFRouter for every inbound message."""
        if not self._authenticate(message):
            return
        self._deliver(message)

    def _deliver(self, message: LXMF.LXMessage) -> None:
        """Hand an authenticated LXMF message up as an InboundMessage."""
        if self._inbound_callback is None:
            return
        source_hex = self._identity_hex_for_delivery(message.source_hash)
        if not source_hex:
            RNS.log(
                "TrenchChat: dropped an authenticated message whose sender "
                "identity could not be resolved",
                RNS.LOG_WARNING,
            )
            return
        content = message.content or ""
        if isinstance(content, bytes):
            content = content.decode(errors="replace")
        try:
            timestamp = float(getattr(message, "timestamp", 0.0) or 0.0)
        except (TypeError, ValueError):
            timestamp = 0.0
        self._inbound_callback(InboundMessage(
            source_hex=source_hex,
            fields=getattr(message, "fields", None) or {},
            content=content,
            timestamp=timestamp,
            hash=getattr(message, "hash", b"") or b"",
            path=PATH_RETICULUM,
        ))

    def _identity_hex_for_delivery(self, delivery_hash: bytes | None) -> str:
        """The identity hash behind a delivery destination hash, or ""."""
        if not delivery_hash:
            return ""
        identity = RNS.Identity.recall(delivery_hash)
        return identity.hash.hex() if identity is not None else ""

    # --- inbound authentication ---

    def _authenticate(self, message: LXMF.LXMessage) -> bool:
        """Return True only if the message's LXMF signature validated."""
        if getattr(message, "signature_validated", False):
            return True

        reason = getattr(message, "unverified_reason", None)
        source_hex = message.source_hash.hex() if message.source_hash else "<none>"

        if reason == LXMF.LXMessage.SOURCE_UNKNOWN:
            self._quarantine_message(message)
            return False

        RNS.log(
            f"TrenchChat: dropped inbound message with invalid signature, "
            f"claimed source {source_hex[:16]}…",
            RNS.LOG_WARNING,
        )
        return False

    def _quarantine_message(self, message: LXMF.LXMessage):
        """Hold a message whose sender identity is not yet known.

        Messages with no packed representation cannot be re-validated later,
        so they are dropped rather than held.
        """
        if not message.source_hash or not getattr(message, "packed", None):
            RNS.log(
                "TrenchChat: dropped unverifiable message with unknown source",
                RNS.LOG_WARNING,
            )
            return

        source_hex = message.source_hash.hex()
        now = time.time()

        with self._quarantine_lock:
            self._prune_quarantine_locked(now)
            queued = self._quarantine.setdefault(source_hex, [])
            if len(queued) >= QUARANTINE_MAX_PER_SENDER:
                queued.pop(0)
            total = sum(len(v) for v in self._quarantine.values())
            if total >= QUARANTINE_MAX_TOTAL:
                oldest_key = min(
                    self._quarantine,
                    key=lambda k: self._quarantine[k][0][0] if self._quarantine[k] else now,
                )
                self._quarantine[oldest_key].pop(0)
                if not self._quarantine[oldest_key]:
                    del self._quarantine[oldest_key]
                queued = self._quarantine.setdefault(source_hex, [])
            queued.append((now, message))

        RNS.log(
            f"TrenchChat: quarantined message from unknown source "
            f"{source_hex[:16]}… pending identity resolution",
            RNS.LOG_DEBUG,
        )
        # Global bucket first: source_hex is unauthenticated wire data here, so
        # the per-source bucket below cannot bound a sender that varies it.
        if not allow_rate(self._path_request_global, self._path_request_lock,
                          "all", PATH_REQUEST_WINDOW_SECS,
                          PATH_REQUEST_GLOBAL_BURST, 1):
            return
        if not allow_rate(self._path_request_rate, self._path_request_lock,
                          source_hex, PATH_REQUEST_WINDOW_SECS,
                          PATH_REQUEST_BURST, PATH_REQUEST_MAX_SOURCES):
            return
        try:
            RNS.Transport.request_path(message.source_hash)
        except Exception as e:
            RNS.log(f"TrenchChat: path request failed for {source_hex[:16]}…: {e}",
                    RNS.LOG_DEBUG)

    def _prune_quarantine_locked(self, now: float):
        for key in list(self._quarantine.keys()):
            kept = [(ts, m) for ts, m in self._quarantine[key]
                    if now - ts < QUARANTINE_TTL_SECS]
            if kept:
                self._quarantine[key] = kept
            else:
                del self._quarantine[key]

    def release_quarantined(self, identity_hex: str) -> None:
        """Re-validate and dispatch messages held for a now-known identity.

        Each message is re-unpacked from its original bytes so LXMF re-runs
        the signature check against the newly recalled identity.
        """
        try:
            source_hash = delivery_hash_for_identity(bytes.fromhex(identity_hex))
        except ValueError:
            return
        source_hex = source_hash.hex()

        with self._quarantine_lock:
            self._prune_quarantine_locked(time.time())
            held = self._quarantine.pop(source_hex, [])

        for _ts, message in held:
            try:
                revalidated = LXMF.LXMessage.unpack_from_bytes(message.packed)
            except Exception as e:
                RNS.log(f"TrenchChat: could not re-validate quarantined message: {e}",
                        RNS.LOG_WARNING)
                continue
            if revalidated is None or not getattr(revalidated, "signature_validated", False):
                RNS.log(
                    f"TrenchChat: dropped quarantined message from "
                    f"{source_hex[:16]}…: signature still invalid after "
                    f"identity resolution",
                    RNS.LOG_WARNING,
                )
                continue
            # Released messages pass through Router's throttle like any other
            # inbound control message; otherwise a peer can park a burst while
            # unknown and have it all delivered at once on announce.
            self._deliver(revalidated)

    # --- send ---

    def send(self, dest_hex: str, fields: dict, content: str = "", *,
             on_delivered=None, on_failed=None, propagated: bool = False,
             envelope: bool = True) -> SendState:
        """Build and hand one LXMessage to LXMF. NO_PATH if it cannot be addressed."""
        dest_identity = self._recall(dest_hex)
        if dest_identity is None:
            return SendState.NO_PATH
        try:
            dest = RNS.Destination(
                dest_identity,
                RNS.Destination.OUT,
                RNS.Destination.SINGLE,
                "lxmf",
                "delivery",
            )
            method = (LXMF.LXMessage.PROPAGATED if propagated
                      else LXMF.LXMessage.DIRECT)
            lxm = LXMF.LXMessage(dest, self._delivery_dest, content,
                                 desired_method=method)
            lxm.fields = pack_fields(fields) if envelope else dict(fields)
            if on_delivered is not None:
                lxm.register_delivery_callback(lambda _m, d=dest_hex: on_delivered(d))
            if on_failed is not None:
                lxm.register_failed_callback(lambda _m, d=dest_hex: on_failed(d))
            self._track_outbound(lxm)
            self._router.handle_outbound(lxm)
            return SendState.SENT
        except Exception as e:
            RNS.log(f"TrenchChat: LXMF send to {dest_hex[:12]}… failed: {e}",
                    RNS.LOG_WARNING)
            return SendState.NO_PATH

    def can_reach(self, dest_hex: str) -> bool:
        """Whether this peer's delivery identity is known right now."""
        return self._recall(dest_hex) is not None

    def request_path(self, dest_hex: str) -> None:
        """Ask the mesh where a peer is. Fire and forget, never blocks."""
        try:
            RNS.Transport.request_path(
                delivery_hash_for_identity(bytes.fromhex(dest_hex)))
        except (ValueError, TypeError) as e:
            RNS.log(f"TrenchChat: could not request a path for {dest_hex[:12]}…: {e}",
                    RNS.LOG_WARNING)

    def limits_for(self, dest_hex: str) -> TransportLimits:
        """The Reticulum path's budgets. The same for every peer on it."""
        return reticulum_limits()

    def drain(self, timeout: float) -> int:
        """Wait until outbound messages stop moving. Returns how many settled."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._outbound_lock:
                moving = [m for m in self._outbound
                          if getattr(m, "state", None) not in _TERMINAL_SEND_STATES]
            if not moving:
                break
            time.sleep(_DRAIN_POLL_SECS)
        with self._outbound_lock:
            settled = sum(1 for m in self._outbound
                          if getattr(m, "state", None) in _TERMINAL_SEND_STATES)
            self._outbound.clear()
        return settled

    def _track_outbound(self, lxm: LXMF.LXMessage) -> None:
        """Watch one message for drain(), dropping anything already settled."""
        with self._outbound_lock:
            self._outbound[:] = [
                m for m in self._outbound
                if getattr(m, "state", None) not in _TERMINAL_SEND_STATES
            ]
            self._outbound.append(lxm)
            if len(self._outbound) > MAX_TRACKED_OUTBOUND:
                del self._outbound[0]

    def _recall(self, dest_hex: str) -> RNS.Identity | None:
        """The delivery identity for a peer named by identity hash."""
        try:
            delivery_hash = delivery_hash_for_identity(bytes.fromhex(dest_hex))
        except (ValueError, TypeError):
            return None
        return RNS.Identity.recall(delivery_hash)

    # --- peer keys ---

    def public_key_for(self, peer_hex: str) -> bytes | None:
        """A peer's public key as RNS remembers it, or None."""
        identity = self._recall(peer_hex)
        if identity is None:
            return None
        try:
            return identity.get_public_key()
        except Exception:
            return None

    def resolve_address(self, address_hex: str) -> str | None:
        """The identity hash behind an LXMF address, once its announce is heard.

        None while it has not been, and the path is requested so it can be.
        """
        try:
            address = bytes.fromhex(address_hex)
        except (ValueError, TypeError):
            return None
        identity = RNS.Identity.recall(address)
        if identity is not None:
            return identity.hash.hex()
        try:
            RNS.Transport.request_path(address)
        except Exception as e:
            RNS.log(f"TrenchChat: could not request a path for {address_hex[:12]}…: "
                    f"{e}", RNS.LOG_DEBUG)
        return None

    # --- peer events ---

    def set_peer_event_callbacks(self, *, peer_appeared=None,
                                 identity_resolved=None, channel_discovered=None,
                                 user_discovered=None, node_discovered=None,
                                 propagation_node_heard=None,
                                 path_changed=None) -> None:
        """Register the fan-outs Router dispatches announce events through."""
        self._peer_appeared = peer_appeared
        self._identity_resolved = identity_resolved
        self._channel_discovered = channel_discovered
        self._user_discovered = user_discovered
        self._node_discovered = node_discovered
        self._propagation_node_heard = propagation_node_heard
        self._path_changed = path_changed

    def _register_announce_handlers(self) -> None:
        """Listen for everything the mesh says about peers, channels and nodes."""
        self._peer_appeared = None
        self._identity_resolved = None
        self._channel_discovered = None
        self._user_discovered = None
        self._node_discovered = None
        self._propagation_node_heard = None
        self._path_changed = None
        self._announce_handlers = [
            ChannelAnnounceHandler(self._on_channel_announce),
            PeerAnnounceHandler(self._on_peer_announce),
            UserAnnounceHandler(self._on_user_announce),
            NodeAnnounceHandler(self._on_node_announce),
            PropagationAnnounceHandler(self._on_propagation_announce),
            PathResponseHandler(self._on_path_response),
        ]
        for handler in self._announce_handlers:
            RNS.Transport.register_announce_handler(handler)

    def _on_channel_announce(self, destination_hash: bytes,
                             announced_identity: RNS.Identity,
                             metadata: dict, iface) -> None:
        creator_hex = (announced_identity.hash.hex()
                       if announced_identity is not None else "")
        if self._channel_discovered is not None:
            self._channel_discovered(destination_hash.hex(), creator_hex,
                                     metadata, iface)

    def _on_peer_announce(self, peer_hex: str, iface) -> None:
        self._first_contact.note_peer(peer_hex, iface)
        if self._peer_appeared is not None:
            self._peer_appeared(peer_hex, iface)

    def _on_user_announce(self, peer_hex: str, display_name: str, iface) -> None:
        self._first_contact.note_peer(peer_hex, iface)
        if self._user_discovered is not None:
            self._user_discovered(peer_hex, display_name, iface)

    def _on_node_announce(self, node_hex: str, display_name: str, iface) -> None:
        if self._node_discovered is not None:
            self._node_discovered(node_hex, display_name, iface)

    def _on_propagation_announce(self, node_hex: str, hops: int) -> None:
        if self._propagation_node_heard is not None:
            self._propagation_node_heard(node_hex, hops)

    def _on_path_response(self, peer_hex: str) -> None:
        self.release_quarantined(peer_hex)
        if self._identity_resolved is not None:
            self._identity_resolved(peer_hex)

    # --- announce ---

    def set_announce_all(self, announce_all) -> None:
        """Register what the first-contact answer and the heartbeat announce.

        Called with an interface to target, or None for every interface.
        """
        self._announce_all = announce_all

    def _announce_everything(self, attached_interface=None) -> None:
        if self._announce_all is not None:
            self._announce_all(attached_interface)
            return
        self.announce(attached_interface=attached_interface)
        self.announce_user(attached_interface=attached_interface)

    def first_contact_tick(self, now: float | None = None) -> bool:
        """Send a queued first-contact answer once it has had time to coalesce."""
        return self._first_contact.tick(now)

    def announce(self, attached_interface=None) -> None:
        """Announce our LXMF delivery destination.

        If attached_interface is given the announce is sent only on that
        interface; otherwise it is broadcast on all interfaces.
        """
        self._router.announce(self._delivery_dest.hash,
                              attached_interface=attached_interface)

    def announce_user(self, attached_interface=None) -> None:
        """Announce our trenchchat.user destination.

        Tells other TrenchChat instances this identity runs TrenchChat, so
        they can add us to their user directory for discovery and invite
        lookup. It carries no payload: the display name already rides in the
        lxmf.delivery announce, where every LXMF client reads it. If
        attached_interface is given the announce is sent only on that
        interface; otherwise it is broadcast on all interfaces.
        """
        self._user_dest.announce(attached_interface=attached_interface)

    def register_channel(self, channel_hash_hex: str, aspect_name: str) -> None:
        """Take ownership of a channel's RNS destination."""
        if channel_hash_hex in self._channel_destinations:
            return
        dest = RNS.Destination(
            self._identity.rns_identity,
            RNS.Destination.IN,
            RNS.Destination.SINGLE,
            APP_NAME,
            APP_ASPECT_CHANNEL,
            aspect_name,
        )
        self._channel_destinations[channel_hash_hex] = dest

    def announce_channel(self, channel_hash_hex: str, app_data: bytes,
                         attached_interface=None) -> None:
        """Announce one owned channel with its discovery metadata."""
        dest = self._channel_destinations.get(channel_hash_hex)
        if dest is None:
            return
        dest.announce(app_data=app_data, attached_interface=attached_interface)

    def start_reannounce(self, interval_secs: float = REANNOUNCE_INTERVAL_SECS
                         ) -> None:
        """Re-announce on a timer, and answer peers met since the last tick.

        A daemon thread, so it never holds the process open.
        """
        def _loop():
            last_announce = 0.0
            while not self._reannounce_stop.wait(FIRST_CONTACT_TICK_SECS):
                now = time.time()
                try:
                    self._first_contact.tick(now)
                except Exception as e:
                    RNS.log(f"TrenchChat: first-contact answer failed: {e}",
                            RNS.LOG_WARNING)
                if now - last_announce < interval_secs:
                    continue
                last_announce = now
                try:
                    self._announce_everything()
                except Exception as e:
                    RNS.log(f"TrenchChat: re-announce failed: {e}", RNS.LOG_WARNING)

        threading.Thread(target=_loop, daemon=True, name="reannounce").start()

    def set_display_name(self, display_name: str) -> None:
        """Update the display name broadcast in LXMF delivery announces."""
        self._delivery_dest.display_name = display_name
        self._config.display_name = display_name

    # --- propagation node ---

    def enable_propagation(self) -> None:
        """Host an LXMF propagation node on this instance.

        This stores and relays mail for the wider LXMF network, not for
        TrenchChat: every TrenchChat message is sent DIRECT, so none of them
        ever enters a propagation store. What a node relays is not selectable
        -- propagated payloads are encrypted end to end, so a node cannot read
        the channel a message belongs to, or anything else about it.
        """
        try:
            limit_kb = self._config.propagation_storage_limit_mb * 1024
            self._router.set_message_storage_limit(kilobytes=limit_kb)
            self._router.enable_propagation()
            self._config.propagation_enabled = True
            RNS.log("TrenchChat: propagation node enabled", RNS.LOG_NOTICE)
        except Exception as e:
            RNS.log(f"TrenchChat: failed to enable propagation node: {e}", RNS.LOG_ERROR)
            raise

    def disable_propagation(self) -> None:
        """Stop hosting a propagation node."""
        self._router.disable_propagation()
        self._config.propagation_enabled = False
        RNS.log("TrenchChat: propagation node disabled", RNS.LOG_NOTICE)

    # --- outbound propagation (offline direct messages) ---

    @property
    def outbound_propagation_node(self) -> bytes | None:
        """The node this client leaves offline direct messages with, if any.

        Callers must check this before sending propagated: LXMF raises from
        handle_outbound when none is set, and fails the message on the way out.
        """
        return self._router.get_outbound_propagation_node()

    def set_outbound_propagation_node(self, destination_hash: bytes) -> None:
        """Choose the node to leave offline direct messages with."""
        self._router.set_outbound_propagation_node(destination_hash)

    def request_propagation_sync(self) -> bool:
        """Collect anything a propagation node is holding for us.

        Propagated messages are pulled, never pushed: without this call a
        direct message left at a node while this client was offline stays
        there. Collected messages arrive through the ordinary delivery
        callback, so they are authenticated exactly like any other.

        False if no node is configured, or the request could not be started.
        """
        if self.outbound_propagation_node is None:
            return False
        try:
            self._router.request_messages_from_propagation_node(
                self._identity.rns_identity)
            return True
        except Exception as e:
            RNS.log(f"TrenchChat: propagation sync request failed: {e}",
                    RNS.LOG_WARNING)
            return False

    def propagation_sync_state(self) -> int:
        """LXMF's transfer state for the last collection attempt."""
        return getattr(self._router, "propagation_transfer_state", 0)

    # --- lifecycle ---

    def stop(self) -> None:
        """Persist LXMF state and tear down delivery destinations.

        LXMF registers this as an atexit hook, but RNS exits the process with
        os._exit, which skips atexit entirely -- so shutdown has to call it.
        Safe to call twice; LXMF guards against re-entry.
        """
        self._reannounce_stop.set()
        try:
            self._router.exit_handler()
        except Exception as e:
            RNS.log(f"TrenchChat: LXMF shutdown error: {e}", RNS.LOG_ERROR)

    # --- accessors the test harness and shutdown need ---

    @property
    def lxmf_router(self) -> LXMF.LXMRouter:
        """The LXMF router, for lifecycle handling that LXMF exposes no API for."""
        return self._router

    @property
    def delivery_destination(self):
        """This node's own LXMF delivery destination."""
        return self._delivery_dest

    @property
    def user_destination(self):
        """This node's trenchchat.user destination."""
        return self._user_dest

    def owned_destinations(self) -> list:
        """Every RNS destination this transport registered, for teardown."""
        return ([self._user_dest, self._delivery_dest]
                + list(self._channel_destinations.values()))

    def announce_handlers(self) -> list:
        """Every announce handler this transport registered, for teardown."""
        return list(self._announce_handlers)


def allow_rate(bucket: dict[str, list], lock: threading.Lock, key: str,
               window_secs: float, burst: int, max_keys: int) -> bool:
    """Sliding-window rate limit, bounded in the number of keys it tracks."""
    now = time.time()
    with lock:
        times = bucket.setdefault(key, [])
        times[:] = [t for t in times if now - t < window_secs]
        if len(times) >= burst:
            return False
        times.append(now)
        if len(bucket) > max_keys:
            for stale, stamps in list(bucket.items()):
                if not stamps or now - stamps[-1] > window_secs:
                    del bucket[stale]
            # Eviction by age alone never fires under key rotation, where
            # every entry is fresh -- and past max_keys the scan above then
            # runs on every inbound packet, under this lock, on the
            # delivery thread. Drop oldest-first until the cap holds.
            while len(bucket) > max_keys:
                oldest = min(bucket, key=lambda k: bucket[k][-1] if bucket[k] else 0)
                del bucket[oldest]
    return True
