"""
In-process nomad node transport for tests.

Mirrors the semantics of RNSNodeTransport without any RNS Links: a shared
FakeNodeRegistry connects the transports of all peers in a test, hosting
registers providers in the registry, and fetch() resolves against any
registered host on a short-lived thread after a small delay, matching real
link timing closely enough for the eventual-consistency helpers.

Failure modes are injectable per transport: unreachable node hashes, forced
timeouts, and a per-fetch delivery delay.
"""

import threading
import time
from pathlib import Path

from trenchchat.network.node_transport import (
    FETCH_BAD_PATH, FETCH_BAD_RESPONSE, FETCH_BUSY, FETCH_TIMEOUT,
    FETCH_TOO_LARGE, FETCH_UNREACHABLE, MAX_QUEUED_FETCHES_PER_NODE,
    NODE_FETCH_TIMEOUT_SECS, NodeTransportBase, is_valid_request_path,
)

FAKE_DELIVERY_DELAY = 0.02


class FakeNodeRegistry:
    """Shared lookup table connecting the fake transports in one test."""

    def __init__(self):
        self.hosts: dict[str, "FakeNodeTransport"] = {}   # node_hex -> host
        self.fetch_log: list[tuple[str, str, str]] = []   # (from, node, path)
        self.lock = threading.RLock()


class FakeNodeTransport(NodeTransportBase):
    def __init__(self, self_hex: str, registry: FakeNodeRegistry, *,
                 node_hex: str | None = None,
                 delivery_delay: float = FAKE_DELIVERY_DELAY,
                 unreachable: set[str] | None = None,
                 timeout_paths: set[str] | None = None):
        super().__init__()
        self.self_hex = self_hex
        # The hash this transport hosts under when hosting is enabled. Real
        # node hashes are destination hashes; tests just need a stable key.
        self.node_hex = node_hex or self_hex
        self.registry = registry
        self._delay = delivery_delay
        self.unreachable: set[str] = set(unreachable or ())
        self.timeout_paths: set[str] = set(timeout_paths or ())
        self.providers: dict = {}
        self.request_data: dict[str, dict | None] = {}
        self.hosting_name: str | None = None
        self.announce_count = 0
        self._pending: dict[str, int] = {}   # node_hex -> in-flight count
        self._lock = threading.RLock()
        self._threads: list[threading.Thread] = []

    # --- fetching ---

    def fetch(self, fetch_id: str, node_hash_hex: str, path: str, *,
              max_size: int, timeout: float = NODE_FETCH_TIMEOUT_SECS,
              data: dict | None = None) -> None:
        self.request_data[fetch_id] = data
        if not is_valid_request_path(path):
            self._notify_result(fetch_id, False, None, FETCH_BAD_PATH)
            return
        with self._lock:
            if self._pending.get(node_hash_hex, 0) >= MAX_QUEUED_FETCHES_PER_NODE:
                busy = True
            else:
                busy = False
                self._pending[node_hash_hex] = \
                    self._pending.get(node_hash_hex, 0) + 1
        if busy:
            self._notify_result(fetch_id, False, None, FETCH_BUSY)
            return
        with self.registry.lock:
            self.registry.fetch_log.append((self.self_hex, node_hash_hex, path))

        def _resolve():
            time.sleep(self._delay)
            try:
                self._resolve_fetch(fetch_id, node_hash_hex, path, max_size)
            finally:
                with self._lock:
                    self._pending[node_hash_hex] = \
                        max(0, self._pending.get(node_hash_hex, 1) - 1)

        thread = threading.Thread(target=_resolve, daemon=True)
        self._threads.append(thread)
        thread.start()

    def _resolve_fetch(self, fetch_id: str, node_hash_hex: str, path: str,
                       max_size: int) -> None:
        if node_hash_hex in self.unreachable:
            self._notify_result(fetch_id, False, None, FETCH_UNREACHABLE)
            return
        if path in self.timeout_paths:
            self._notify_result(fetch_id, False, None, FETCH_TIMEOUT)
            return
        with self.registry.lock:
            host = self.registry.hosts.get(node_hash_hex)
        provider = host.providers.get(path) if host is not None else None
        if provider is None:
            self._notify_result(fetch_id, False, None, FETCH_TIMEOUT)
            return
        try:
            payload = provider()
        except Exception:
            payload = None
        # A file provider hands back its Path; the real transport streams it
        # and the receiving side reads the bytes out of the response handle.
        if isinstance(payload, Path):
            try:
                payload = payload.read_bytes()
            except OSError:
                payload = None
        if not isinstance(payload, bytes):
            self._notify_result(fetch_id, False, None, FETCH_BAD_RESPONSE)
            return
        if len(payload) > max_size:
            self._notify_result(fetch_id, False, None, FETCH_TOO_LARGE)
            return
        self._notify_progress(fetch_id, 1.0)
        self._notify_result(fetch_id, True, payload, None)

    def cancel(self, fetch_id: str) -> None:
        pass

    # --- hosting ---

    def start_hosting(self, display_name: str, providers: dict) -> None:
        self.hosting_name = display_name
        self.providers = {p: fn for p, fn in providers.items()
                          if is_valid_request_path(p)}
        with self.registry.lock:
            self.registry.hosts[self.node_hex] = self
        self.announce()

    def update_hosting(self, display_name: str, providers: dict) -> None:
        if self.hosting_name is None:
            return
        self.hosting_name = display_name
        self.providers = {p: fn for p, fn in providers.items()
                          if is_valid_request_path(p)}

    def stop_hosting(self) -> None:
        self.hosting_name = None
        self.providers = {}
        with self.registry.lock:
            if self.registry.hosts.get(self.node_hex) is self:
                del self.registry.hosts[self.node_hex]

    def announce(self) -> None:
        if self.hosting_name is not None:
            self.announce_count += 1

    def tick(self) -> None:
        pass

    def join_threads(self, timeout: float = 2.0) -> None:
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads = [t for t in self._threads if t.is_alive()]
