"""
In-memory directory of discovered TrenchChat peers.

Entries are populated exclusively from trenchchat.user announces, so every
entry represents a confirmed TrenchChat peer (not a generic LXMF client).
Each entry stores the peer's identity hash, their self-reported display name,
and the timestamp of the last announce.

Stale entries are pruned after DIRECTORY_TTL_SECS (default 24 hours).  This
is long enough to survive offline periods between announce cycles while still
removing peers that have left the network.
"""

import time
import threading

import RNS

DIRECTORY_TTL_SECS: float = 86_400  # 24 hours


# Peers remembered at once. Identities are free to mint and every announce
# heard adds one, so the TTL alone bounds nothing.
MAX_TRACKED_PEERS = 512


class UserDirectory:
    """In-memory directory of discovered TrenchChat peers.

    Fed exclusively by trenchchat.user announces via record_user().  The
    directory is thread-safe and may be queried from any thread.
    """

    def __init__(self, self_hex: str, ttl_secs: float = DIRECTORY_TTL_SECS):
        """
        self_hex: identity hash hex of the local user (excluded from results).
        ttl_secs: seconds after which an unseen entry is pruned.
        """
        self._self_hex = self_hex
        self._ttl = ttl_secs
        # identity_hash_hex -> (display_name, last_seen_timestamp)
        self._entries: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()
        self._callbacks: list = []

    # --- public API ---

    def add_directory_callback(self, cb) -> None:
        """Register a callback invoked with (peer_hex, display_name) whenever a
        peer is first recorded or their display name changes.

        Fired only on a genuine change, not on every announce refresh, so a
        periodic reannounce heartbeat does not flood consumers.
        """
        self._callbacks.append(cb)

    def record_user(self, peer_hex: str, display_name: str) -> None:
        """Record or refresh a TrenchChat peer from a trenchchat.user announce.

        Skips the local user's own identity.  Updates the display name and
        resets the TTL clock on each call.  Fires directory callbacks when the
        peer is new or their display name changed.
        """
        if peer_hex == self._self_hex:
            return
        with self._lock:
            # Fed from every announce heard, and prune() only drops entries
            # once they are stale -- which under a stream of fresh identities
            # is never. Evict the least recently seen instead.
            if (len(self._entries) >= MAX_TRACKED_PEERS
                    and peer_hex not in self._entries):
                oldest = min(self._entries, key=lambda k: self._entries[k][1])
                del self._entries[oldest]
            previous = self._entries.get(peer_hex)
            changed = previous is None or previous[0] != display_name
            self._entries[peer_hex] = (display_name, time.time())
        RNS.log(
            f"TrenchChat [user_directory]: recorded peer {peer_hex[:12]}… "
            f"name={display_name!r}",
            RNS.LOG_DEBUG,
        )
        if changed:
            self._fire_callbacks(peer_hex, display_name)

    def _fire_callbacks(self, peer_hex: str, display_name: str) -> None:
        for cb in self._callbacks:
            try:
                cb(peer_hex, display_name)
            except Exception as e:
                RNS.log(
                    f"TrenchChat [user_directory]: callback error: {e}",
                    RNS.LOG_ERROR,
                )

    def search(self, query: str) -> list[dict]:
        """Return non-expired entries matching query (case-insensitive substring).

        Matches against both the display name and the identity hash hex.
        Returns a list of dicts with keys: identity_hash, display_name.
        An empty query returns all non-expired entries.
        """
        q = query.strip().lower()
        now = time.time()
        results: list[dict] = []
        with self._lock:
            for peer_hex, (display_name, last_seen) in self._entries.items():
                if now - last_seen >= self._ttl:
                    continue
                if q and q not in display_name.lower() and q not in peer_hex.lower():
                    continue
                results.append({"identity_hash": peer_hex, "display_name": display_name})
        results.sort(key=lambda r: r["display_name"].lower())
        return results

    def contains(self, peer_hex: str) -> bool:
        """Return True if the peer has a non-expired entry in the directory."""
        now = time.time()
        with self._lock:
            entry = self._entries.get(peer_hex)
            if entry is None:
                return False
            return now - entry[1] < self._ttl

    def get_all(self) -> list[dict]:
        """Return all non-expired entries sorted by display name.

        Equivalent to search("").
        """
        return self.search("")

    def prune(self) -> None:
        """Remove entries that have not been seen within the TTL window."""
        now = time.time()
        with self._lock:
            stale = [
                peer_hex
                for peer_hex, (_, last_seen) in self._entries.items()
                if now - last_seen >= self._ttl
            ]
            for peer_hex in stale:
                del self._entries[peer_hex]
                RNS.log(
                    f"TrenchChat [user_directory]: pruned stale peer {peer_hex[:12]}…",
                    RNS.LOG_DEBUG,
                )
