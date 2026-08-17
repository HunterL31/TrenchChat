"""
Local-only saved contacts ("friends list").

A friend is an identity hash the user has chosen to remember, with an
optional nickname and free-text note. Nothing here ever goes on the wire --
no protocol fields, no message types. Last-seen tracking rides on
PresenceManager.add_seen_callback, which fires on every announce or inbound
message, not just online/offline transitions -- see presence.py.

The nickname stored here is friends-panel-only. It never feeds
presence.resolve_display_name(), which stays the peer's self-asserted name.
"""

import threading
import time

import RNS

from trenchchat.core.presence import resolve_display_name

IDENTITY_HASH_HEX_LEN = 32

# Durable last-seen writes are throttled to this often per friend -- announces
# are frequent and a DB write per packet would be wasteful.
FRIEND_SEEN_WRITE_INTERVAL_SECS = 60


class FriendsManager:
    """Add/update/remove saved contacts and track their durable last-seen time."""

    def __init__(self, storage, self_hex: str, presence_mgr=None) -> None:
        self._storage = storage
        self._self_hex = self_hex
        self._presence_mgr = presence_mgr
        self._lock = threading.Lock()
        self._friend_hashes: set[str] = storage.get_friend_hashes()
        self._last_write: dict[str, float] = {}
        self._callbacks: list = []

    # --- public API ---

    def add_friend(self, identity_hash_hex: str, nickname: str = "", note: str = "") -> bool:
        """Add or update a friend. Returns False for a malformed hash or self."""
        if not self._is_valid_hash(identity_hash_hex) or identity_hash_hex == self._self_hex:
            return False
        self._storage.upsert_friend(identity_hash_hex, nickname, note)
        with self._lock:
            self._friend_hashes.add(identity_hash_hex)
        self._fire_callbacks(identity_hash_hex)
        return True

    def update_friend(self, identity_hash_hex: str, *, nickname: str | None = None,
                      note: str | None = None) -> bool:
        """Apply only the given fields; None leaves that field unchanged.

        Returns False if the friend doesn't exist.
        """
        existing = self._storage.get_friend(identity_hash_hex)
        if existing is None:
            return False
        new_nickname = existing["nickname"] if nickname is None else nickname
        new_note = existing["note"] if note is None else note
        self._storage.upsert_friend(identity_hash_hex, new_nickname, new_note)
        self._fire_callbacks(identity_hash_hex)
        return True

    def remove_friend(self, identity_hash_hex: str) -> bool:
        """Returns False if the friend doesn't exist."""
        if self._storage.get_friend(identity_hash_hex) is None:
            return False
        self._storage.delete_friend(identity_hash_hex)
        with self._lock:
            self._friend_hashes.discard(identity_hash_hex)
            self._last_write.pop(identity_hash_hex, None)
        self._fire_callbacks(identity_hash_hex)
        return True

    def is_friend(self, identity_hash_hex: str) -> bool:
        with self._lock:
            return identity_hash_hex in self._friend_hashes

    def get_friends(self) -> list[dict]:
        """Return one dict per friend, joining the stored row with live presence.

        last_seen_at is the max of the stored (throttled) value and
        presence_mgr's live value, so a currently-online friend reads as
        live even before the throttled write lands.
        """
        results = []
        for row in self._storage.get_friends():
            identity_hash = row["identity_hash"]
            last_seen_at = row["last_seen_at"]
            is_online = False
            if self._presence_mgr is not None:
                is_online = self._presence_mgr.is_online(identity_hash)
                last_seen_at = max(last_seen_at, self._presence_mgr.last_seen_at(identity_hash))
            results.append({
                "identity_hash": identity_hash,
                "nickname": row["nickname"],
                "note": row["note"],
                "display_name": resolve_display_name(
                    identity_hash, self._self_hex, self._storage
                ),
                "added_at": row["added_at"],
                "last_seen_at": last_seen_at,
                "is_online": is_online,
            })
        return results

    def record_seen(self, peer_hex: str) -> None:
        """PresenceManager seen-callback. A no-op set lookup for non-friends;
        for a friend, a durable write throttled to once per
        FRIEND_SEEN_WRITE_INTERVAL_SECS."""
        with self._lock:
            if peer_hex not in self._friend_hashes:
                return
            now = time.time()
            if now - self._last_write.get(peer_hex, 0.0) < FRIEND_SEEN_WRITE_INTERVAL_SECS:
                return
            self._last_write[peer_hex] = now
        self._storage.touch_friend_seen(peer_hex, now)

    def add_friends_callback(self, cb) -> None:
        """Register a callback invoked with (identity_hash_hex: str) on add/update/remove.

        Never fired for throttled last-seen writes.
        """
        self._callbacks.append(cb)

    # --- private helpers ---

    def _fire_callbacks(self, identity_hash_hex: str) -> None:
        for cb in self._callbacks:
            try:
                cb(identity_hash_hex)
            except Exception as e:
                RNS.log(f"TrenchChat [friends]: callback error: {e}", RNS.LOG_ERROR)

    @staticmethod
    def _is_valid_hash(value: str) -> bool:
        if len(value) != IDENTITY_HASH_HEX_LEN:
            return False
        try:
            bytes.fromhex(value)
        except ValueError:
            return False
        return True
