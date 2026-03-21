"""
Announce-based peer presence tracking.

A peer is considered "online" if their LXMF delivery announce was received
within PRESENCE_TIMEOUT_SECS (default 3 minutes, ~3 announce cycles at 60s
each with margin).

This module has no network side-effects -- it only records timestamps from
announces that are already being received by PeerAnnounceHandler.
"""

import time
import threading

import RNS
import msgpack

PRESENCE_TIMEOUT_SECS = 180


class PresenceManager:
    """Tracks peer online/offline status based on LXMF delivery announces."""

    def __init__(self, self_hex: str):
        self._self_hex = self_hex
        # identity_hash_hex -> last announce timestamp
        self._last_seen: dict[str, float] = {}
        self._lock = threading.Lock()
        self._callbacks: list = []

    # --- public API ---

    def add_presence_callback(self, cb) -> None:
        """Register a callback invoked with (peer_hex: str, is_online: bool) on status change."""
        self._callbacks.append(cb)

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

    def is_online(self, peer_hex: str) -> bool:
        """Return True if the peer is considered online (including self)."""
        if peer_hex == self._self_hex:
            return True
        with self._lock:
            return self._is_online_locked(peer_hex)

    def get_online_peers(self) -> set[str]:
        """Return the set of identity hashes currently considered online (excluding self)."""
        now = time.time()
        with self._lock:
            return {
                hex_id
                for hex_id, ts in self._last_seen.items()
                if now - ts < PRESENCE_TIMEOUT_SECS
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
        """Return the best available display name for a peer identity.

        Resolution order:
          1. Members table (any channel) — name from a published member list
          2. LXMF announce app_data — name the peer broadcasts in their announce
          3. Identity hash prefix — consistent fallback used across the UI
        """
        if identity_hex == self._self_hex:
            return "You"

        # 1. Storage lookup across all channels
        try:
            stored = storage.get_display_name_for_identity(identity_hex)
            if stored:
                return stored
        except Exception:
            pass

        # 2. LXMF announce app_data — packed as [display_name_bytes, stamp_cost]
        try:
            identity_bytes = bytes.fromhex(identity_hex)
            # recall() needs a delivery destination hash, not a raw identity hash
            delivery_hash = RNS.Destination.hash_from_name_and_identity(
                "lxmf.delivery", identity_bytes
            )
            raw = RNS.Identity.recall_app_data(delivery_hash)
            if raw:
                parsed = msgpack.unpackb(raw, raw=False)
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
        return identity_hex[:12] + "…"

    def prune(self) -> None:
        """Remove stale entries and fire callbacks for peers that went offline."""
        now = time.time()
        went_offline: list[str] = []
        with self._lock:
            stale = [
                hex_id
                for hex_id, ts in self._last_seen.items()
                if now - ts >= PRESENCE_TIMEOUT_SECS
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
        return time.time() - ts < PRESENCE_TIMEOUT_SECS

    def _fire_callbacks(self, peer_hex: str, is_online: bool) -> None:
        for cb in self._callbacks:
            try:
                cb(peer_hex, is_online)
            except Exception as e:
                RNS.log(f"TrenchChat [presence]: callback error: {e}", RNS.LOG_ERROR)
