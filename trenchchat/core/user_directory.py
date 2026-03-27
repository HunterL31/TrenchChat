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

    # --- public API ---

    def record_user(self, peer_hex: str, display_name: str) -> None:
        """Record or refresh a TrenchChat peer from a trenchchat.user announce.

        Skips the local user's own identity.  Updates the display name and
        resets the TTL clock on each call.
        """
        if peer_hex == self._self_hex:
            return
        with self._lock:
            self._entries[peer_hex] = (display_name, time.time())
        RNS.log(
            f"TrenchChat [user_directory]: recorded peer {peer_hex[:12]}… "
            f"name={display_name!r}",
            RNS.LOG_DEBUG,
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
