"""
Nomad Network page browsing and hosting.

Owns the discovered-node registry, the fetch state machine, the page/file
caches, bookmarks, and the hosted pages directory. All link work is
delegated to an injected NodeTransportBase (network/node_transport.py), so
tests run against a fake with no mesh.

Hosting serves DATA_DIR/nomad_pages/pages/ as "/page/..." and
DATA_DIR/nomad_pages/files/ as "/file/...", mirroring nomadnet's node
directory layout so an existing node dir can be copied straight in. Unlike
nomadnet, pages starting with "#!" are served as plain bytes — no CGI
execution.
"""

import os
import threading
import time
from pathlib import Path

import RNS

from trenchchat.network.node_transport import (
    NODE_FETCH_TIMEOUT_SECS, NOMAD_APP_NAME, NOMAD_ASPECT_NODE,
    NodeTransportBase, is_valid_request_path,
)

MAX_PAGE_BYTES = 512 * 1024
MAX_FILE_BYTES = 5 * 1024 * 1024
PAGE_CACHE_MAX_ROWS = 500
FILE_CACHE_MAX_BYTES = 50 * 1024 * 1024
NODE_LIST_MAX_ROWS = 200
NODE_NAME_MAX_LEN = 64
CACHE_PRUNE_INTERVAL_SECS = 300.0

# How long a #!c=0 ("do not cache") page stays retrievable. The UI reads
# fetched content back through the cache endpoint, so the row must outlive
# the delivery; after the grace it is gone, honouring the author's intent.
NO_CACHE_GRACE_SECS = 60.0

FETCH_QUEUED = "queued"
FETCH_FETCHING = "fetching"
FETCH_DONE = "done"
FETCH_FAILED = "failed"

DEFAULT_INDEX_MU = """>TrenchChat Node

Served by a TrenchChat peer over Reticulum.

Edit the files under nomad_pages/pages/ in your TrenchChat data directory
to publish your own pages.
"""


def nomad_node_hash_for_identity(identity_hash_hex: str) -> str:
    """The nomadnetwork.node destination hash a peer's node would announce
    under, derived purely from their identity hash."""
    return RNS.Destination.hash(
        bytes.fromhex(identity_hash_hex), NOMAD_APP_NAME, NOMAD_ASPECT_NODE
    ).hex()


def parse_nomad_url(url: str) -> tuple[str | None, str]:
    """Split a nomad URL into (node_hash_hex | None, request_path). Strict.

    Accepts "<hash>:/page/x.mu", ":/page/x.mu" (current-node relative),
    "/page/x.mu", bare "<hash>" or "<hash>:" (meaning /page/index.mu).
    Raises ValueError on anything else.
    """
    if not isinstance(url, str):
        raise ValueError("url must be a string")
    url = url.strip()
    if not url:
        raise ValueError("empty url")

    node_hex: str | None = None
    path = url
    if ":" in url:
        node_part, path = url.split(":", 1)
        if node_part:
            node_hex = _validate_node_hex(node_part)
    elif not url.startswith("/"):
        node_hex = _validate_node_hex(url)
        path = ""

    if not path:
        path = "/page/index.mu"
    if not is_valid_request_path(path):
        raise ValueError(f"invalid request path: {path!r}")
    return node_hex, path


def page_cache_expiry(payload: bytes, now: float) -> float | None:
    """The cache deadline a page declares via NomadNet's #!c= header.

    "#!c=N" on the first line is the cache lifetime in seconds; "#!c=0"
    means do not cache (kept only for NO_CACHE_GRACE_SECS so the client can
    read the delivery back). None means no header — the default LRU regime
    applies. Total: malformed headers read as no header.
    """
    if not payload.startswith(b"#!c="):
        return None
    first_line = payload.split(b"\n", 1)[0]
    try:
        seconds = int(first_line[4:])
    except ValueError:
        return None
    if seconds < 0:
        return None
    if seconds == 0:
        return now + NO_CACHE_GRACE_SECS
    return now + seconds


def _validate_node_hex(value: str) -> str:
    value = value.strip().lower()
    if len(value) != 32:
        raise ValueError("node hash must be 32 hex characters")
    try:
        bytes.fromhex(value)
    except ValueError:
        raise ValueError("node hash is not valid hex") from None
    return value


class _FetchState:
    def __init__(self, fetch_id: str, node_hex: str, path: str, kind: str):
        self.fetch_id = fetch_id
        self.node_hex = node_hex
        self.path = path
        self.kind = kind


class NodeBrowserManager:
    """Discovers nomad nodes, fetches their pages, and hosts our own."""

    def __init__(self, identity, storage, config, *,
                 transport: NodeTransportBase | None = None):
        self._identity = identity
        self._storage = storage
        self._config = config
        self._transport = transport if transport is not None \
            else NodeTransportBase()
        self._lock = threading.RLock()
        self._fetches: dict[str, _FetchState] = {}
        self._node_callbacks: list = []
        self._fetch_callbacks: list = []
        self._last_prune = time.time()

        self._transport.set_fetch_result_callback(self._on_fetch_result)
        self._transport.set_fetch_progress_callback(self._on_fetch_progress)

        if config.nomad_hosting_enabled:
            try:
                self._start_hosting_from_config()
            except Exception as e:
                RNS.log(f"TrenchChat [nomad]: could not restore hosting: {e}",
                        RNS.LOG_ERROR)

    # --- callbacks ---

    def add_node_callback(self, cb) -> None:
        """cb(node_hash_hex, display_name)"""
        self._node_callbacks.append(cb)

    def add_fetch_callback(self, cb) -> None:
        """cb(fetch_id, node_hash_hex, path, status, progress, reason)"""
        self._fetch_callbacks.append(cb)

    def _notify_node(self, node_hex: str, display_name: str) -> None:
        for cb in self._node_callbacks:
            try:
                cb(node_hex, display_name)
            except Exception as e:
                RNS.log(f"TrenchChat [nomad]: node callback error: {e}",
                        RNS.LOG_ERROR)

    def _notify_fetch(self, fetch_id: str, node_hex: str, path: str,
                      status: str, progress: float,
                      reason: str | None) -> None:
        for cb in self._fetch_callbacks:
            try:
                cb(fetch_id, node_hex, path, status, progress, reason)
            except Exception as e:
                RNS.log(f"TrenchChat [nomad]: fetch callback error: {e}",
                        RNS.LOG_ERROR)

    # --- announce ingestion ---

    def record_node_announce(self, node_hash_hex: str, display_name: str,
                             iface=None) -> None:
        """Record a nomadnetwork.node announce. Presentation fields only."""
        try:
            node_hex = _validate_node_hex(node_hash_hex)
        except ValueError:
            return
        self._storage.upsert_nomad_node(node_hex, display_name)
        self._notify_node(node_hex, display_name)

    # --- browsing ---

    def fetch_page(self, node_hash_hex: str, path: str) -> str:
        return self._start_fetch(node_hash_hex, path, "page", MAX_PAGE_BYTES)

    def fetch_file(self, node_hash_hex: str, path: str) -> str:
        return self._start_fetch(node_hash_hex, path, "file", MAX_FILE_BYTES)

    def _start_fetch(self, node_hash_hex: str, path: str, kind: str,
                     max_size: int) -> str:
        node_hex = _validate_node_hex(node_hash_hex)
        if not is_valid_request_path(path):
            raise ValueError(f"invalid request path: {path!r}")
        if not path.startswith(f"/{kind}/"):
            raise ValueError(f"path {path!r} is not a /{kind}/ request")
        with self._lock:
            for state in self._fetches.values():
                if state.node_hex == node_hex and state.path == path:
                    return state.fetch_id
            fetch_id = os.urandom(8).hex()
            self._fetches[fetch_id] = _FetchState(fetch_id, node_hex, path,
                                                  kind)
        self._notify_fetch(fetch_id, node_hex, path, FETCH_QUEUED, 0.0, None)
        self._transport.fetch(fetch_id, node_hex, path, max_size=max_size,
                              timeout=NODE_FETCH_TIMEOUT_SECS)
        return fetch_id

    def _on_fetch_result(self, fetch_id: str, ok: bool,
                         payload: bytes | None, reason: str | None) -> None:
        with self._lock:
            state = self._fetches.pop(fetch_id, None)
        if state is None:
            return
        if ok and payload is not None:
            if state.kind == "page":
                self._storage.put_nomad_page(
                    state.node_hex, state.path, payload,
                    expires_at=page_cache_expiry(payload, time.time()))
            else:
                self._storage.put_nomad_file(state.node_hex, state.path,
                                             payload)
            self._notify_fetch(fetch_id, state.node_hex, state.path,
                               FETCH_DONE, 1.0, None)
        else:
            self._notify_fetch(fetch_id, state.node_hex, state.path,
                               FETCH_FAILED, 0.0, reason)

    def _on_fetch_progress(self, fetch_id: str, progress: float) -> None:
        with self._lock:
            state = self._fetches.get(fetch_id)
        if state is None:
            return
        self._notify_fetch(fetch_id, state.node_hex, state.path,
                           FETCH_FETCHING, progress, None)

    def get_cached_page(self, node_hash_hex: str, path: str):
        return self._storage.get_nomad_page(node_hash_hex, path)

    def get_cached_file(self, node_hash_hex: str, path: str):
        return self._storage.get_nomad_file(node_hash_hex, path)

    def known_nodes(self) -> list:
        return self._storage.get_nomad_nodes()

    def node_for_identity(self, identity_hash_hex: str):
        """The discovered node a peer hosts, or None when never heard."""
        try:
            node_hex = nomad_node_hash_for_identity(identity_hash_hex)
        except ValueError:
            return None
        return self._storage.get_nomad_node(node_hex)

    # --- bookmarks ---

    def bookmarks(self) -> list:
        return self._storage.get_nomad_bookmarks()

    def add_bookmark(self, node_hash_hex: str, path: str, label: str) -> None:
        node_hex = _validate_node_hex(node_hash_hex)
        if not is_valid_request_path(path):
            raise ValueError(f"invalid request path: {path!r}")
        self._storage.add_nomad_bookmark(node_hex, path, label[:128])

    def remove_bookmark(self, node_hash_hex: str, path: str) -> bool:
        return self._storage.remove_nomad_bookmark(node_hash_hex, path)

    # --- hosting ---

    @property
    def pages_root(self) -> Path:
        return self._config.data_dir / "nomad_pages"

    def hosting_status(self) -> dict:
        pages, files = self._scan_pages_dir()
        return {
            "enabled": self._config.nomad_hosting_enabled,
            "node_name": self._config.nomad_node_name,
            "pages_dir": str(self.pages_root),
            "pages": [{"path": p, "size": size}
                      for p, (_, size) in sorted(pages.items())],
            "files": [{"path": p, "size": size}
                      for p, (_, size) in sorted(files.items())],
        }

    def set_hosting(self, *, enabled: bool | None = None,
                    node_name: str | None = None) -> dict:
        if node_name is not None:
            name = "".join(c for c in node_name if c.isprintable()).strip()
            name = name[:NODE_NAME_MAX_LEN]
            self._config.nomad_node_name = name
        if enabled is not None:
            if enabled:
                if not self._config.nomad_node_name:
                    self._config.nomad_node_name = \
                        f"{self._config.display_name} node"[:NODE_NAME_MAX_LEN]
                self._config.nomad_hosting_enabled = True
                self._start_hosting_from_config()
            else:
                self._config.nomad_hosting_enabled = False
                self._transport.stop_hosting()
        elif self._config.nomad_hosting_enabled and node_name is not None:
            self._transport.update_hosting(self._config.nomad_node_name,
                                           self._build_providers())
        return self.hosting_status()

    def refresh_hosted_pages(self) -> dict:
        """Rescan the pages directory and re-register served paths."""
        if self._config.nomad_hosting_enabled:
            self._transport.update_hosting(self._config.nomad_node_name,
                                           self._build_providers())
        return self.hosting_status()

    def _start_hosting_from_config(self) -> None:
        self._ensure_pages_dir()
        self._transport.start_hosting(self._config.nomad_node_name,
                                      self._build_providers())

    def _ensure_pages_dir(self) -> None:
        pages_dir = self.pages_root / "pages"
        files_dir = self.pages_root / "files"
        pages_dir.mkdir(parents=True, exist_ok=True)
        files_dir.mkdir(parents=True, exist_ok=True)
        index = pages_dir / "index.mu"
        if not index.exists():
            index.write_text(DEFAULT_INDEX_MU, encoding="utf-8")

    def _scan_pages_dir(self) -> tuple[dict, dict]:
        """Enumerate servable content. This is the traversal boundary:
        only regular files that resolve inside the pages root are listed."""
        pages = self._scan_subdir(self.pages_root / "pages", "/page")
        files = self._scan_subdir(self.pages_root / "files", "/file")
        return pages, files

    def _scan_subdir(self, base: Path, prefix: str) -> dict:
        result: dict = {}
        try:
            base_real = base.resolve()
        except OSError:
            return result
        if not base_real.is_dir():
            return result
        for dirpath, dirnames, filenames in os.walk(base_real):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for filename in filenames:
                if filename.startswith("."):
                    continue
                full = Path(dirpath) / filename
                try:
                    real = full.resolve()
                    if not real.is_relative_to(base_real) or not real.is_file():
                        continue
                    size = real.stat().st_size
                    rel = real.relative_to(base_real).as_posix()
                except OSError:
                    continue
                served_path = f"{prefix}/{rel}"
                if not is_valid_request_path(served_path):
                    RNS.log(f"TrenchChat [nomad]: not serving {rel!r} — "
                            f"unservable path", RNS.LOG_WARNING)
                    continue
                result[served_path] = (real, size)
        return result

    def _build_providers(self) -> dict:
        pages, files = self._scan_pages_dir()

        def make_provider(real_path: Path):
            def provider() -> bytes | None:
                try:
                    return real_path.read_bytes()
                except OSError:
                    return None
            return provider

        providers = {}
        for served_path, (real, _) in {**pages, **files}.items():
            providers[served_path] = make_provider(real)
        return providers

    # --- housekeeping ---

    def tick(self) -> None:
        self._transport.tick()
        now = time.time()
        if now - self._last_prune >= CACHE_PRUNE_INTERVAL_SECS:
            self._last_prune = now
            try:
                self._storage.prune_nomad_pages(PAGE_CACHE_MAX_ROWS)
                self._storage.prune_nomad_files(FILE_CACHE_MAX_BYTES)
                self._storage.prune_nomad_nodes(NODE_LIST_MAX_ROWS)
            except Exception as e:
                RNS.log(f"TrenchChat [nomad]: cache prune failed: {e}",
                        RNS.LOG_WARNING)
