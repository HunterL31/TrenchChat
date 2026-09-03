"""
In-process file transport for tests.

Mirrors the semantics of RNSFileTransport without any RNS Links: a shared
FakeFileRegistry connects the transports of all peers in a test, start_serving
registers the peer's serve callback in the registry, and a fetch resolves
against the holder's serve callback on a short-lived thread after a small
delay, matching real link timing closely enough for the eventual-consistency
helpers.

Failure modes are injectable per transport: holders that cannot be reached,
(holder, chunk index) pairs that stall, a per-holder delay, and drop_link,
which fails whatever that holder is serving right now the way a dropped link
does.
"""

import threading
import time

from trenchchat.network.file_transport import (
    FETCH_REFUSED, FETCH_STALLED, FILE_FETCH_TIMEOUT_SECS,
    FILE_REQUEST_MAX_CHUNKS, FileTransportBase, max_response_for,
)
from trenchchat.network.link_client import (
    FETCH_LINK_CLOSED, FETCH_TOO_LARGE, FETCH_UNREACHABLE,
)

FAKE_DELIVERY_DELAY = 0.02


class FakeFileRegistry:
    """Shared lookup table connecting the fake transports in one test."""

    def __init__(self):
        self.holders: dict[str, "FakeFileTransport"] = {}
        # (requester, holder, file_hash, first, count, want_list)
        self.fetch_log: list[tuple[str, str, str, int, int, bool]] = []
        self.lock = threading.RLock()


class FakeFileTransport(FileTransportBase):
    def __init__(self, self_hex: str, registry: FakeFileRegistry, *,
                 delivery_delay: float = FAKE_DELIVERY_DELAY,
                 unreachable: set[str] | None = None,
                 stall_chunks: set[tuple[str, int]] | None = None,
                 holder_delays: dict[str, float] | None = None):
        super().__init__()
        self.self_hex = self_hex
        self.registry = registry
        self._delay = delivery_delay
        self.unreachable: set[str] = set(unreachable or ())
        self.stall_chunks: set[tuple[str, int]] = set(stall_chunks or ())
        self.holder_delays: dict[str, float] = dict(holder_delays or {})
        self.serving = False
        self.announces = 0
        # Holders a link has been opened to, and the fetches riding each one.
        self.linked: set[str] = set()
        self._in_flight: dict[str, str] = {}     # fetch_id -> holder_hex
        self._cancelled: set[str] = set()
        self._dropped: set[str] = set()
        self._lock = threading.RLock()
        self._threads: list[threading.Thread] = []

    # --- fetching ---

    def fetch_chunks(self, fetch_id: str, holder_hex: str,
                     file_hash_hex: str, first: int, count: int,
                     timeout: float = FILE_FETCH_TIMEOUT_SECS) -> None:
        if self._valid_fetch(fetch_id, file_hash_hex, first, count) is None:
            return
        self._start(fetch_id, holder_hex, file_hash_hex, first, count, False,
                    max_response_for(count))

    def fetch_chunk_list(self, fetch_id: str, holder_hex: str,
                         file_hash_hex: str,
                         timeout: float = FILE_FETCH_TIMEOUT_SECS) -> None:
        if self._valid_fetch(fetch_id, file_hash_hex, 0, 1) is None:
            return
        self._start(fetch_id, holder_hex, file_hash_hex, 0, 0, True,
                    max_response_for(FILE_REQUEST_MAX_CHUNKS))

    def _start(self, fetch_id: str, holder_hex: str, file_hash_hex: str,
               first: int, count: int, want_list: bool, max_size: int) -> None:
        with self.registry.lock:
            self.registry.fetch_log.append(
                (self.self_hex, holder_hex, file_hash_hex, first, count,
                 want_list))
        with self._lock:
            self._in_flight[fetch_id] = holder_hex
            self._dropped.discard(holder_hex)
        self.linked.add(holder_hex)
        delay = self.holder_delays.get(holder_hex, self._delay)

        def _resolve():
            time.sleep(delay)
            try:
                self._resolve_fetch(fetch_id, holder_hex, file_hash_hex,
                                    first, count, want_list, max_size)
            finally:
                with self._lock:
                    self._in_flight.pop(fetch_id, None)

        thread = threading.Thread(target=_resolve, daemon=True)
        self._threads.append(thread)
        thread.start()

    def _resolve_fetch(self, fetch_id: str, holder_hex: str,
                       file_hash_hex: str, first: int, count: int,
                       want_list: bool, max_size: int) -> None:
        with self._lock:
            if fetch_id in self._cancelled:
                self._cancelled.discard(fetch_id)
                return
            dropped = holder_hex in self._dropped
        if dropped:
            self._notify_result(fetch_id, False, None, FETCH_LINK_CLOSED)
            return
        if holder_hex in self.unreachable:
            self._notify_result(fetch_id, False, None, FETCH_UNREACHABLE)
            return
        if not want_list and any((holder_hex, idx) in self.stall_chunks
                                 for idx in range(first, first + count)):
            self._notify_result(fetch_id, False, None, FETCH_STALLED)
            return
        with self.registry.lock:
            holder = self.registry.holders.get(holder_hex)
        if holder is None:
            self._notify_result(fetch_id, False, None, FETCH_UNREACHABLE)
            return
        payload = holder.serve(self.self_hex, file_hash_hex, first, count,
                               want_list)
        if payload is None:
            self._notify_result(fetch_id, False, None, FETCH_REFUSED)
            return
        if len(payload) > max_size:
            self._notify_result(fetch_id, False, None, FETCH_TOO_LARGE)
            return
        self._notify_progress(fetch_id, 1.0)
        self._notify_result(fetch_id, True, payload, None)

    def cancel(self, fetch_id: str) -> None:
        with self._lock:
            if fetch_id in self._in_flight:
                self._cancelled.add(fetch_id)

    # --- links ---

    def drop_link(self, holder_hex: str) -> bool:
        """Drop the link to a holder, failing whatever it carries right now."""
        had_link = holder_hex in self.linked
        self.linked.discard(holder_hex)
        with self._lock:
            self._dropped.add(holder_hex)
        return had_link

    # --- serving ---

    def serve(self, requester_hex: str | None, file_hash_hex: str,
              first: int, count: int, want_list: bool) -> bytes | None:
        """Answer a request the way the real serve handler does."""
        if not self.serving:
            return None
        return self._call_serve(requester_hex, file_hash_hex, first, count,
                                want_list)

    def start_serving(self) -> None:
        self.serving = True
        with self.registry.lock:
            self.registry.holders[self.self_hex] = self

    def announce(self) -> None:
        """Record it. A fetch here needs no path, so only the count matters."""
        self.announces += 1

    def stop_serving(self) -> None:
        self.serving = False
        with self.registry.lock:
            if self.registry.holders.get(self.self_hex) is self:
                del self.registry.holders[self.self_hex]

    def tick(self) -> None:
        pass

    def join_threads(self, timeout: float = 2.0) -> None:
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads = [t for t in self._threads if t.is_alive()]
