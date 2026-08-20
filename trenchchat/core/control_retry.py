"""
A retry queue for control messages sent before a peer's path is known.

Chat messages already survive this: `Messaging` queues anything it cannot
address and flushes it when the peer reappears. Subscribe and invite did not,
so a message sent in the seconds before Reticulum resolved a path was dropped
outright -- no queue, no retry, no error. The owner then never learned of a
subscriber, or an invite never arrived, and only doing it again recovered.

The queue is bounded on both age and count because identities are free to
mint: a peer that never becomes reachable must cost a fixed amount of memory,
not a growing one.
"""

import threading
import time

import RNS

MAX_QUEUED_PER_PEER = 16
QUEUE_TTL_SECS = 3600.0
MAX_TRACKED_PEERS = 256


class ControlRetryQueue:
    """Control messages held for peers whose path was not yet resolved."""

    def __init__(self, label: str, ttl_secs: float = QUEUE_TTL_SECS):
        self._label = label
        self._ttl = ttl_secs
        self._queued: dict[str, list[tuple[float, dict]]] = {}
        self._lock = threading.Lock()

    def queue(self, dest_hex: str, fields: dict) -> None:
        """Hold a message until this peer is reachable.

        Held no longer than *ttl_secs*: a message whose recipient will no
        longer act on it is worth nothing and costs bandwidth on a radio.
        """
        now = time.time()
        with self._lock:
            self._prune(now)
            waiting = self._queued.setdefault(dest_hex, [])
            if len(waiting) >= MAX_QUEUED_PER_PEER:
                waiting.pop(0)
            waiting.append((now, fields))
        RNS.log(
            f"TrenchChat [{self._label}]: queued a message for {dest_hex[:12]}… "
            f"until their path resolves",
            RNS.LOG_DEBUG,
        )

    def flush(self, dest_hex: str, send) -> int:
        """Re-send everything held for a peer. Returns how many went out.

        *send* is the manager's own send function and must return False when
        the message still could not go out, so it can be held for the next
        attempt rather than lost on the retry as it was on the first try.
        """
        now = time.time()
        with self._lock:
            self._prune(now)
            waiting = self._queued.pop(dest_hex, [])
        if not waiting:
            return 0

        # A failed send re-queues the message itself, through queue(), which
        # is where both bounds are applied. Re-adding it here as well stored a
        # second copy with a refreshed timestamp -- so the TTL could never
        # expire it -- and bypassed MAX_QUEUED_PER_PEER, growing the list by
        # the popped count on every failed flush.
        sent = 0
        for _queued_at, fields in waiting:
            try:
                if send(dest_hex, fields):
                    sent += 1
            except Exception as e:
                RNS.log(
                    f"TrenchChat [{self._label}]: retry to {dest_hex[:12]}… "
                    f"failed: {e}",
                    RNS.LOG_WARNING,
                )

        if sent:
            RNS.log(
                f"TrenchChat [{self._label}]: re-sent {sent} queued message(s) "
                f"to {dest_hex[:12]}…",
                RNS.LOG_NOTICE,
            )
        return sent

    def pending_for(self, dest_hex: str) -> int:
        with self._lock:
            return len(self._queued.get(dest_hex, []))

    def _prune(self, now: float) -> None:
        """Drop expired entries, and the oldest peers once there are too many."""
        for peer in list(self._queued):
            fresh = [e for e in self._queued[peer] if now - e[0] < self._ttl]
            if fresh:
                self._queued[peer] = fresh
            else:
                del self._queued[peer]
        if len(self._queued) > MAX_TRACKED_PEERS:
            oldest = sorted(self._queued, key=lambda p: self._queued[p][0][0])
            for peer in oldest[:len(self._queued) - MAX_TRACKED_PEERS]:
                del self._queued[peer]
