"""
NomadNet node plane: RNS Link lifecycle for page browsing and hosting.

Speaks Nomad Network's node protocol for full interop: nodes are RNS
destinations on the "nomadnetwork.node" aspect, pages and files are served
as request/response over links ("/page/index.mu", "/file/<name>"). Fetching
dials the node destination directly (announces carry the destination hash,
so no lxmf-delivery indirection) and is anonymous unless the caller asks
for a node by name: identify_policy names the nodes our identity may be
revealed to, and nothing else ever calls Link.identify. Hosting registers
one request handler per served file; RNS dispatches requests by exact path
hash, so unregistered paths simply have no handler.

The dial ladder, the per-node queue and the request plumbing are the shared
link client (network/link_client.py); what stays here is the nomad protocol
itself: its paths, its response shapes, and its identify policy.

This module never touches Storage or core managers, matching
voice_transport.py's layering: callbacks up, tick() down.
"""

import time
from pathlib import Path

import RNS

from trenchchat.core.fileutils import clean_filename as _clean_filename
# The fetch reasons, states and connection classes are the shared link
# client's; they are imported here so callers of the node plane keep one
# import site, as they had before the two planes shared an engine.
from trenchchat.network.link_client import (
    FETCH_BAD_PATH, FETCH_BAD_RESPONSE, FETCH_BUSY, FETCH_IDENTITY_MISMATCH,
    FETCH_LINK_CLOSED, FETCH_NOT_FOUND, FETCH_SEND_FAILED, FETCH_TIMEOUT,
    FETCH_TOO_LARGE, FETCH_UNREACHABLE, LINK_IDLE_SECS, LINK_REDIAL_BACKOFF,
    MAX_INBOUND_LINKS, MAX_QUEUED_FETCHES_PER_PEER, MAX_SERVED_RESPONSE_BYTES,
    SERVE_RATE_LIMIT, SERVE_RATE_WINDOW, LinkClient, LinkClientBase,
    LinkConn as _NodeConn, LinkFetch as _Fetch, _IDLE, _LINKED,
)

NOMAD_APP_NAME = "nomadnetwork"
NOMAD_ASPECT_NODE = "node"

NODE_REDIAL_BACKOFF = LINK_REDIAL_BACKOFF
# A link is kept for the whole time a node is being read, not just between
# back-to-back requests: nomadnet holds one open until you browse elsewhere,
# and a node operator sees link churn from a slow reader as a new handshake
# per page. Idle links are still dropped, just not mid-visit.
NODE_LINK_IDLE_SECS = LINK_IDLE_SECS
NODE_FETCH_TIMEOUT_SECS = 60.0
NODE_ANNOUNCE_INTERVAL_SECS = 900.0
MAX_QUEUED_FETCHES_PER_NODE = MAX_QUEUED_FETCHES_PER_PEER
MAX_REQUEST_PATH_LEN = 256

# nomadnet's own ceiling for compressing a file response. Above it the cost
# is not worth paying, and most large files are already compressed.
NODE_FILE_AUTO_COMPRESS = 32_000_000

NODE_SERVE_RATE_LIMIT = SERVE_RATE_LIMIT
NODE_SERVE_RATE_WINDOW = SERVE_RATE_WINDOW
MAX_INBOUND_NODE_LINKS = MAX_INBOUND_LINKS


def is_valid_request_path(path) -> bool:
    """Whether a nomad request path is safe to send or register. Total."""
    if not isinstance(path, str) or not path:
        return False
    if len(path) > MAX_REQUEST_PATH_LEN:
        return False
    if not (path.startswith("/page/") or path.startswith("/file/")):
        return False
    if ".." in path or "\\" in path or "\0" in path:
        return False
    if not path.isprintable():
        return False
    return True


def _response_payload(receipt) -> tuple[bytes | None, str | None]:
    """A request response as (bytes, name the node gave it).

    A page is plain bytes. A file is not: nomadnet serves it as
    [open(path), {"name": ...}], which RNS delivers as an open handle on a
    temp file it deletes as soon as this callback returns -- so it has to be
    read here, not later -- with the name alongside in the receipt metadata.
    Older nodes answer with [name, data] instead. (None, None) for anything
    else.
    """
    data = receipt.response
    name = _response_name(getattr(receipt, "metadata", None))
    if hasattr(data, "read"):
        try:
            data = data.read()
        except OSError as e:
            RNS.log(f"TrenchChat [nomad]: could not read file response: {e}",
                    RNS.LOG_WARNING)
            return None, None
    elif isinstance(data, (list, tuple)) and len(data) == 2:
        if name is None:
            name = _clean_filename(data[0])
        data = data[1]
    if isinstance(data, str):
        data = data.encode("utf-8")
    if not isinstance(data, bytes):
        return None, None
    return data, name


def _response_name(metadata) -> str | None:
    """The file name in a response's metadata, if it carries a usable one."""
    if not isinstance(metadata, dict):
        return None
    return _clean_filename(metadata.get("name"))


class NodeTransportBase(LinkClientBase):
    """Injectable transport interface; see RNSNodeTransport for semantics."""

    log_tag = "nomad"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._identify_policy = None

    def _may_identify(self, node_hex: str) -> bool:
        if self._identify_policy is None:
            return False
        try:
            return bool(self._identify_policy(node_hex))
        except Exception as e:
            RNS.log(f"TrenchChat [nomad]: identify policy error: {e}",
                    RNS.LOG_ERROR)
            return False

    def set_fetch_result_callback(self, cb) -> None:
        """cb(fetch_id, ok, payload: bytes | None, reason, filename)"""
        self._result_cb = cb

    def set_fetch_progress_callback(self, cb) -> None:
        """cb(fetch_id, progress: float) with progress in 0.0-1.0"""
        self._progress_cb = cb

    def set_identify_policy(self, policy) -> None:
        """policy(node_hash_hex) -> bool: may we identify to this node?"""
        self._identify_policy = policy

    def identify(self, node_hash_hex: str) -> bool:
        raise NotImplementedError

    def is_identified(self, node_hash_hex: str) -> bool:
        raise NotImplementedError

    def drop_link(self, node_hash_hex: str) -> bool:
        raise NotImplementedError

    def fetch(self, fetch_id: str, node_hash_hex: str, path: str, *,
              max_size: int, timeout: float = NODE_FETCH_TIMEOUT_SECS,
              data: dict | None = None) -> None:
        raise NotImplementedError

    def cancel(self, fetch_id: str) -> None:
        raise NotImplementedError

    def start_hosting(self, display_name: str, providers: dict) -> None:
        raise NotImplementedError

    def update_hosting(self, display_name: str, providers: dict) -> None:
        raise NotImplementedError

    def stop_hosting(self) -> None:
        raise NotImplementedError

    def announce(self) -> None:
        raise NotImplementedError

    def tick(self) -> None:
        raise NotImplementedError

    # shared helpers for subclasses

    def _notify_result(self, fetch_id: str, ok: bool, payload: bytes | None,
                       reason: str | None,
                       filename: str | None = None) -> None:
        super()._notify_result(fetch_id, ok, payload, reason, filename)


class RNSNodeTransport(LinkClient, NodeTransportBase):
    """Real RNS Link implementation of the nomad node plane."""

    link_idle_secs = NODE_LINK_IDLE_SECS
    max_queued_per_peer = MAX_QUEUED_FETCHES_PER_NODE
    max_inbound_links = MAX_INBOUND_NODE_LINKS
    serve_rate_limit = NODE_SERVE_RATE_LIMIT
    serve_rate_window = NODE_SERVE_RATE_WINDOW

    def __init__(self, identity):
        super().__init__(identity=identity)
        self._in_dest = None
        self._hosting_name: str | None = None
        self._providers: dict = {}
        self._last_announce = 0.0

    # --- fetching ---

    def fetch(self, fetch_id: str, node_hash_hex: str, path: str, *,
              max_size: int, timeout: float = NODE_FETCH_TIMEOUT_SECS,
              data: dict | None = None) -> None:
        if not is_valid_request_path(path):
            self._notify_result(fetch_id, False, None, FETCH_BAD_PATH)
            return
        self._enqueue(_Fetch(fetch_id, node_hash_hex, path, max_size, timeout,
                             data))

    # --- link client hooks ---

    def _dial_destination(self, node_hex: str):
        dest_hash = bytes.fromhex(node_hex)
        node_identity = RNS.Identity.recall(dest_hash)
        if node_identity is None:
            RNS.Transport.request_path(dest_hash)
            return None, None
        dest = RNS.Destination(
            node_identity,
            RNS.Destination.OUT,
            RNS.Destination.SINGLE,
            NOMAD_APP_NAME,
            NOMAD_ASPECT_NODE,
        )
        if dest.hash != dest_hash:
            # A recalled identity that doesn't hash back to the dialed
            # destination would let responses be cached under the wrong
            # node. Hard-fail rather than retry.
            return None, FETCH_IDENTITY_MISMATCH
        if not RNS.Transport.has_path(dest.hash):
            RNS.Transport.request_path(dest.hash)
            return None, None
        return dest, None

    def _should_identify(self, node_hex: str) -> bool:
        return self._may_identify(node_hex)

    def _handle_response(self, fetch: _Fetch, receipt) -> None:
        data, filename = _response_payload(receipt)
        if data is None:
            self._notify_result(fetch.fetch_id, False, None,
                                FETCH_BAD_RESPONSE)
            return
        self._notify_result(fetch.fetch_id, True, data, None, filename)

    def _sweep_active(self, now: float) -> list[tuple[_Fetch, str]]:
        # Belt and braces over RNS's own request timeout.
        return [(fetch, FETCH_TIMEOUT) for fetch in self._active.values()
                if now - fetch.created_at >= fetch.timeout * 2]

    # --- identifying ---

    def identify(self, node_hash_hex: str) -> bool:
        """Reveal our identity to a node over the link already open to it.

        Gated by the same policy as an establishing link, so there is one
        answer to "may this node know us" and no way past it. Returns
        whether the proof went out; False when no link is up, in which case
        the stored choice is what identifies the next one.
        """
        if not self._may_identify(node_hash_hex):
            return False
        with self._lock:
            conn = self._conns.get(node_hash_hex)
            link = conn.link if conn is not None and conn.state == _LINKED \
                else None
        if link is None:
            return False
        return self._identify_on(node_hash_hex, link)

    # --- housekeeping ---

    def tick(self) -> None:
        self._tick_links()
        if (self._hosting_name is not None
                and time.time() - self._last_announce
                >= NODE_ANNOUNCE_INTERVAL_SECS):
            self.announce()

    # --- hosting ---

    def start_hosting(self, display_name: str, providers: dict) -> None:
        with self._lock:
            if self._in_dest is None:
                self._in_dest = RNS.Destination(
                    self._identity.rns_identity,
                    RNS.Destination.IN,
                    RNS.Destination.SINGLE,
                    NOMAD_APP_NAME,
                    NOMAD_ASPECT_NODE,
                )
                self._in_dest.set_link_established_callback(
                    self._on_inbound_link)
            self._hosting_name = display_name
            self._apply_providers(providers)
        self.announce()

    def update_hosting(self, display_name: str, providers: dict) -> None:
        with self._lock:
            if self._in_dest is None:
                return
            self._hosting_name = display_name
            self._apply_providers(providers)

    def _apply_providers(self, providers: dict) -> None:
        """Caller holds the lock and has ensured _in_dest exists."""
        for path in set(self._providers) - set(providers):
            try:
                self._in_dest.deregister_request_handler(path)
            except Exception:
                pass
        for path in set(providers) - set(self._providers):
            if not is_valid_request_path(path):
                RNS.log(f"TrenchChat [nomad]: refusing to serve invalid "
                        f"path {path!r}", RNS.LOG_WARNING)
                continue
            self._in_dest.register_request_handler(
                path,
                response_generator=self._serve,
                allow=RNS.Destination.ALLOW_ALL,
                auto_compress=(NODE_FILE_AUTO_COMPRESS
                               if path.startswith("/file/") else True),
            )
        self._providers = {p: fn for p, fn in providers.items()
                           if is_valid_request_path(p)}

    def stop_hosting(self) -> None:
        with self._lock:
            if self._in_dest is not None:
                for path in self._providers:
                    try:
                        self._in_dest.deregister_request_handler(path)
                    except Exception:
                        pass
            self._providers = {}
            self._hosting_name = None

    def announce(self) -> None:
        with self._lock:
            dest = self._in_dest
            name = self._hosting_name
        if dest is None or name is None:
            return
        try:
            dest.announce(app_data=name.encode("utf-8"))
            self._last_announce = time.time()
        except Exception as e:
            RNS.log(f"TrenchChat [nomad]: announce failed: {e}",
                    RNS.LOG_WARNING)

    def _serve(self, path, data, request_id, link_id, remote_identity,
               requested_at):
        with self._lock:
            key = bytes(link_id) if link_id is not None else b""
            if not self._allow_request(key, time.time()):
                return None
            provider = self._providers.get(path)
        if provider is None:
            return None
        try:
            payload = provider()
        except Exception as e:
            RNS.log(f"TrenchChat [nomad]: serve error for {path}: {e}",
                    RNS.LOG_WARNING)
            return None
        if isinstance(payload, Path):
            return self._serve_file(path, payload)
        if not isinstance(payload, bytes):
            return None
        if len(payload) > MAX_SERVED_RESPONSE_BYTES:
            RNS.log(f"TrenchChat [nomad]: refusing to serve oversized "
                    f"{path} ({len(payload)} bytes)", RNS.LOG_WARNING)
            return None
        return payload

    def _serve_file(self, path: str, real: Path):
        """A file response: an open handle plus the name it should be saved
        under.

        This is the only shape nomadnet's browser can save -- handed raw
        bytes it reads response[0] as a filename, gets an integer, and drops
        the download with an exception. RNS streams from the handle and
        closes it.
        """
        try:
            size = real.stat().st_size
        except OSError as e:
            RNS.log(f"TrenchChat [nomad]: cannot stat {path}: {e}",
                    RNS.LOG_WARNING)
            return None
        if size > MAX_SERVED_RESPONSE_BYTES:
            RNS.log(f"TrenchChat [nomad]: refusing to serve oversized "
                    f"{path} ({size} bytes)", RNS.LOG_WARNING)
            return None
        try:
            handle = real.open("rb")
        except OSError as e:
            RNS.log(f"TrenchChat [nomad]: cannot open {path}: {e}",
                    RNS.LOG_WARNING)
            return None
        return [handle, {"name": real.name.encode("utf-8")}]
