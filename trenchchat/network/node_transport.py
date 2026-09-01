"""
NomadNet node plane: RNS Link lifecycle for page browsing and hosting.

Speaks Nomad Network's node protocol for full interop: nodes are RNS
destinations on the "nomadnetwork.node" aspect, pages and files are served
as request/response over links ("/page/index.mu", "/file/<name>"). Fetching
dials the node destination directly (announces carry the destination hash,
so no lxmf-delivery indirection) and never identifies — browsing is
anonymous. Hosting registers one request handler per served file; RNS
dispatches requests by exact path hash, so unregistered paths simply have
no handler.

This module never touches Storage or core managers, matching
voice_transport.py's layering: callbacks up, tick() down.
"""

import threading
import time

import RNS

NOMAD_APP_NAME = "nomadnetwork"
NOMAD_ASPECT_NODE = "node"

NODE_REDIAL_BACKOFF = (2.0, 5.0, 10.0, 30.0)
NODE_LINK_IDLE_SECS = 60.0
NODE_FETCH_TIMEOUT_SECS = 60.0
NODE_ANNOUNCE_INTERVAL_SECS = 900.0
MAX_QUEUED_FETCHES_PER_NODE = 8
MAX_REQUEST_PATH_LEN = 256

NODE_SERVE_RATE_LIMIT = 8
NODE_SERVE_RATE_WINDOW = 1.0
MAX_INBOUND_NODE_LINKS = 32
MAX_SERVED_RESPONSE_BYTES = 5 * 1024 * 1024

# Fetch failure reasons surfaced through the result callback.
FETCH_BAD_PATH = "bad_path"
FETCH_BUSY = "busy"
FETCH_UNREACHABLE = "unreachable"
FETCH_TIMEOUT = "timeout"
FETCH_TOO_LARGE = "too_large"
FETCH_LINK_CLOSED = "link_closed"
FETCH_SEND_FAILED = "send_failed"
FETCH_BAD_RESPONSE = "bad_response"
FETCH_IDENTITY_MISMATCH = "identity_mismatch"
FETCH_CANCELLED = "cancelled"

# Internal connection states.
_IDLE = "idle"
_DIALING = "dialing"
_LINKED = "linked"


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


class NodeTransportBase:
    """Injectable transport interface; see RNSNodeTransport for semantics."""

    def __init__(self):
        self._result_cb = None
        self._progress_cb = None

    def set_fetch_result_callback(self, cb) -> None:
        """cb(fetch_id, ok: bool, payload: bytes | None, reason: str | None)"""
        self._result_cb = cb

    def set_fetch_progress_callback(self, cb) -> None:
        """cb(fetch_id, progress: float) with progress in 0.0-1.0"""
        self._progress_cb = cb

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
                       reason: str | None) -> None:
        if self._result_cb is None:
            return
        try:
            self._result_cb(fetch_id, ok, payload, reason)
        except Exception as e:
            RNS.log(f"TrenchChat [nomad]: result callback error: {e}",
                    RNS.LOG_ERROR)

    def _notify_progress(self, fetch_id: str, progress: float) -> None:
        if self._progress_cb is None:
            return
        try:
            self._progress_cb(fetch_id, float(progress))
        except Exception as e:
            RNS.log(f"TrenchChat [nomad]: progress callback error: {e}",
                    RNS.LOG_ERROR)


class _Fetch:
    """One in-flight or queued page/file request."""

    def __init__(self, fetch_id: str, node_hex: str, path: str,
                 max_size: int, timeout: float, data: dict | None = None):
        self.fetch_id = fetch_id
        self.node_hex = node_hex
        self.path = path
        self.max_size = max_size
        self.timeout = timeout
        self.data = data
        self.created_at = time.time()
        self.receipt = None
        self.redialed = False


class _NodeConn:
    """Bookkeeping for one node's link across dial/fetch/redial cycles."""

    def __init__(self, node_hex: str):
        self.node_hex = node_hex
        self.state = _IDLE
        self.link = None
        self.dial_attempts = 0
        self.next_dial_at = 0.0
        self.last_used = time.time()
        self.queued: list[_Fetch] = []

    @property
    def exhausted(self) -> bool:
        return self.dial_attempts >= len(NODE_REDIAL_BACKOFF)


class RNSNodeTransport(NodeTransportBase):
    """Real RNS Link implementation of the nomad node plane."""

    def __init__(self, identity):
        super().__init__()
        self._identity = identity
        self._lock = threading.RLock()
        self._conns: dict[str, _NodeConn] = {}
        self._by_link: dict[int, str] = {}
        self._active: dict[int, _Fetch] = {}       # id(receipt) -> fetch

        self._in_dest = None
        self._hosting_name: str | None = None
        self._providers: dict = {}
        self._inbound_links: dict[int, tuple] = {}  # id(link) -> (link, ts)
        self._serve_times: dict[bytes, list[float]] = {}
        self._last_announce = 0.0

    # --- fetching ---

    def fetch(self, fetch_id: str, node_hash_hex: str, path: str, *,
              max_size: int, timeout: float = NODE_FETCH_TIMEOUT_SECS,
              data: dict | None = None) -> None:
        if not is_valid_request_path(path):
            self._notify_result(fetch_id, False, None, FETCH_BAD_PATH)
            return
        fetch = _Fetch(fetch_id, node_hash_hex, path, max_size, timeout, data)
        dial_now = False
        flush_now = False
        with self._lock:
            conn = self._conns.get(node_hash_hex)
            if conn is None:
                conn = _NodeConn(node_hash_hex)
                self._conns[node_hash_hex] = conn
            active_here = sum(1 for f in self._active.values()
                              if f.node_hex == node_hash_hex)
            if len(conn.queued) + active_here >= MAX_QUEUED_FETCHES_PER_NODE:
                busy = True
            else:
                busy = False
                conn.queued.append(fetch)
                conn.last_used = time.time()
                if conn.state == _LINKED:
                    flush_now = True
                elif conn.state == _IDLE and time.time() >= conn.next_dial_at:
                    dial_now = True
        if busy:
            self._notify_result(fetch_id, False, None, FETCH_BUSY)
            return
        if flush_now:
            self._flush_queued(node_hash_hex)
        elif dial_now:
            self._dial(node_hash_hex)

    def cancel(self, fetch_id: str) -> None:
        with self._lock:
            for conn in self._conns.values():
                conn.queued = [f for f in conn.queued if f.fetch_id != fetch_id]
            for key, fetch in list(self._active.items()):
                if fetch.fetch_id == fetch_id:
                    del self._active[key]

    # --- outbound dialing ---

    def _dial(self, node_hex: str) -> None:
        with self._lock:
            conn = self._conns.get(node_hex)
            if conn is None or conn.state != _IDLE:
                return
            conn.state = _DIALING

        try:
            dest_hash = bytes.fromhex(node_hex)
            node_identity = RNS.Identity.recall(dest_hash)
            if node_identity is None:
                RNS.Transport.request_path(dest_hash)
                self._defer_dial(node_hex)
                return
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
                self._fail_conn(node_hex, FETCH_IDENTITY_MISMATCH)
                return
            if not RNS.Transport.has_path(dest.hash):
                RNS.Transport.request_path(dest.hash)
                self._defer_dial(node_hex)
                return
            link = RNS.Link(
                dest,
                established_callback=self._on_outbound_established,
                closed_callback=self._on_link_closed,
            )
            with self._lock:
                conn = self._conns.get(node_hex)
                if conn is None:
                    self._teardown_link(link)
                    return
                conn.link = link
                self._by_link[id(link)] = node_hex
        except Exception as e:
            RNS.log(f"TrenchChat [nomad]: dial to {node_hex[:12]}… "
                    f"failed: {e}", RNS.LOG_WARNING)
            self._defer_dial(node_hex)

    def _redial_for(self, fetch: _Fetch) -> bool:
        """Requeue a fetch that burnt on a dead link and dial a fresh one.

        A link the remote has already dropped still looks established here
        until RNS notices, so the first request on it fails to send. Retry
        once per fetch; a second failure is a real one.
        """
        if fetch.redialed:
            return False
        fetch.redialed = True
        link = None
        with self._lock:
            conn = self._conns.get(fetch.node_hex)
            if conn is None:
                return False
            if conn.link is not None:
                self._by_link.pop(id(conn.link), None)
                link = conn.link
                conn.link = None
            conn.state = _IDLE
            conn.dial_attempts = 0
            conn.next_dial_at = 0.0
            conn.queued.append(fetch)
        self._teardown_link(link)
        RNS.log(f"TrenchChat [nomad]: link to {fetch.node_hex[:12]}… was "
                f"dead, redialing", RNS.LOG_DEBUG)
        self._dial(fetch.node_hex)
        return True

    def _defer_dial(self, node_hex: str) -> None:
        # Exhaustion never fails queued fetches here: on a cold path, RNS
        # resolution can outlast the whole backoff ladder while the node is
        # up. Each fetch's own timeout is the promise; dialing just settles
        # to the backoff tail until then.
        with self._lock:
            conn = self._conns.get(node_hex)
            if conn is None:
                return
            self._register_link_failure(conn)

    def _register_link_failure(self, conn: _NodeConn, *,
                               arm_backoff: bool = True) -> None:
        """Caller holds the lock. Schedules the next dial.

        The backoff ladder answers dials that never landed. A link that was
        established and later closed says nothing about reachability, so it
        clears the ladder instead of climbing it.
        """
        if conn.link is not None:
            self._by_link.pop(id(conn.link), None)
            conn.link = None
        conn.state = _IDLE
        if not arm_backoff:
            conn.dial_attempts = 0
            conn.next_dial_at = 0.0
            return
        backoff = NODE_REDIAL_BACKOFF[
            min(conn.dial_attempts, len(NODE_REDIAL_BACKOFF) - 1)]
        conn.dial_attempts += 1
        conn.next_dial_at = time.time() + backoff

    def _fail_conn(self, node_hex: str, reason: str) -> None:
        """Fail everything queued or active for a node and drop it."""
        failed: list[_Fetch] = []
        link = None
        with self._lock:
            conn = self._conns.pop(node_hex, None)
            if conn is None:
                return
            failed.extend(conn.queued)
            conn.queued = []
            for key, fetch in list(self._active.items()):
                if fetch.node_hex == node_hex:
                    failed.append(fetch)
                    del self._active[key]
            if conn.link is not None:
                self._by_link.pop(id(conn.link), None)
                link = conn.link
        self._teardown_link(link)
        for fetch in failed:
            self._notify_result(fetch.fetch_id, False, None, reason)

    def _on_outbound_established(self, link) -> None:
        with self._lock:
            node_hex = self._by_link.get(id(link))
            conn = self._conns.get(node_hex) if node_hex else None
            if conn is None:
                self._teardown_link(link)
                return
            conn.state = _LINKED
            conn.dial_attempts = 0
        self._flush_queued(node_hex)

    def _flush_queued(self, node_hex: str) -> None:
        with self._lock:
            conn = self._conns.get(node_hex)
            if conn is None or conn.state != _LINKED or conn.link is None:
                return
            to_send = conn.queued
            conn.queued = []
            link = conn.link
            conn.last_used = time.time()
        for fetch in to_send:
            self._issue_request(link, fetch)

    def _issue_request(self, link, fetch: _Fetch) -> None:
        try:
            receipt = link.request(
                fetch.path,
                data=fetch.data,
                response_callback=self._on_response,
                failed_callback=self._on_request_failed,
                progress_callback=self._on_request_progress,
                timeout=fetch.timeout,
                max_response_size=fetch.max_size,
            )
        except Exception as e:
            RNS.log(f"TrenchChat [nomad]: request send failed: {e}",
                    RNS.LOG_WARNING)
            receipt = False
        if receipt is False or receipt is None:
            if self._redial_for(fetch):
                return
            self._notify_result(fetch.fetch_id, False, None, FETCH_SEND_FAILED)
            return
        fetch.receipt = receipt
        with self._lock:
            self._active[id(receipt)] = fetch

    # --- request callbacks (RNS threads) ---

    def _pop_active(self, receipt) -> _Fetch | None:
        with self._lock:
            fetch = self._active.pop(id(receipt), None)
            if fetch is not None:
                conn = self._conns.get(fetch.node_hex)
                if conn is not None:
                    conn.last_used = time.time()
            return fetch

    def _on_response(self, receipt) -> None:
        fetch = self._pop_active(receipt)
        if fetch is None:
            return
        data = receipt.response
        if isinstance(data, str):
            data = data.encode("utf-8")
        if not isinstance(data, bytes):
            self._notify_result(fetch.fetch_id, False, None,
                                FETCH_BAD_RESPONSE)
            return
        self._notify_result(fetch.fetch_id, True, data, None)

    def _on_request_failed(self, receipt) -> None:
        fetch = self._pop_active(receipt)
        if fetch is None:
            return
        reason = FETCH_TIMEOUT
        try:
            if (receipt.response_size is not None
                    and receipt.max_response_size is not None
                    and receipt.response_size > receipt.max_response_size):
                reason = FETCH_TOO_LARGE
        except Exception:
            pass
        self._notify_result(fetch.fetch_id, False, None, reason)

    def _on_request_progress(self, receipt) -> None:
        with self._lock:
            fetch = self._active.get(id(receipt))
        if fetch is None:
            return
        self._notify_progress(fetch.fetch_id, receipt.progress)

    # --- link close ---

    def _on_link_closed(self, link) -> None:
        # Outbound and inbound links share this callback; inbound links are
        # only tracked in _inbound_links.
        with self._lock:
            self._inbound_links.pop(id(link), None)
            node_hex = self._by_link.pop(id(link), None)
            conn = self._conns.get(node_hex) if node_hex else None
            if conn is None or conn.link is not link:
                return
            failed = [f for f in self._active.values()
                      if f.node_hex == node_hex]
            for fetch in failed:
                self._active.pop(id(fetch.receipt), None)
            self._register_link_failure(conn, arm_backoff=False)
        for fetch in failed:
            self._notify_result(fetch.fetch_id, False, None, FETCH_LINK_CLOSED)

    def _teardown_link(self, link) -> None:
        if link is None:
            return
        try:
            link.teardown()
        except Exception as e:
            RNS.log(f"TrenchChat [nomad]: link teardown error: {e}",
                    RNS.LOG_DEBUG)

    # --- housekeeping ---

    def tick(self) -> None:
        now = time.time()
        dial_needed: list[str] = []
        timed_out: list[_Fetch] = []
        idle_links: list = []

        never_linked: list[_Fetch] = []
        with self._lock:
            for conn in list(self._conns.values()):
                if conn.queued and conn.state == _IDLE and now >= conn.next_dial_at:
                    dial_needed.append(conn.node_hex)
                for fetch in list(conn.queued):
                    # Still queued at its deadline means no link was ever
                    # established for it -- "unreachable" is the honest
                    # reason, "timeout" is kept for mid-transfer stalls.
                    if now - fetch.created_at >= fetch.timeout:
                        conn.queued.remove(fetch)
                        never_linked.append(fetch)
            for key, fetch in list(self._active.items()):
                # Belt and braces over RNS's own request timeout.
                if now - fetch.created_at >= fetch.timeout * 2:
                    del self._active[key]
                    timed_out.append(fetch)
            for conn in list(self._conns.values()):
                has_active = any(f.node_hex == conn.node_hex
                                 for f in self._active.values())
                if (conn.state == _LINKED and not conn.queued and not has_active
                        and now - conn.last_used >= NODE_LINK_IDLE_SECS):
                    if conn.link is not None:
                        self._by_link.pop(id(conn.link), None)
                        idle_links.append(conn.link)
                    del self._conns[conn.node_hex]
                elif (conn.state == _IDLE and not conn.queued
                        and now - conn.last_used >= NODE_LINK_IDLE_SECS):
                    del self._conns[conn.node_hex]
            for key, times in list(self._serve_times.items()):
                if not times or now - times[-1] > NODE_SERVE_RATE_WINDOW:
                    del self._serve_times[key]

        for node_hex in dial_needed:
            self._dial(node_hex)
        for fetch in never_linked:
            self._notify_result(fetch.fetch_id, False, None, FETCH_UNREACHABLE)
        for fetch in timed_out:
            self._notify_result(fetch.fetch_id, False, None, FETCH_TIMEOUT)
        for link in idle_links:
            self._teardown_link(link)

        if (self._hosting_name is not None
                and now - self._last_announce >= NODE_ANNOUNCE_INTERVAL_SECS):
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

    def _on_inbound_link(self, link) -> None:
        link.set_link_closed_callback(self._on_link_closed)
        evicted = None
        with self._lock:
            if len(self._inbound_links) >= MAX_INBOUND_NODE_LINKS:
                oldest = min(self._inbound_links,
                             key=lambda k: self._inbound_links[k][1])
                evicted, _ = self._inbound_links.pop(oldest)
            self._inbound_links[id(link)] = (link, time.time())
        if evicted is not None:
            self._teardown_link(evicted)

    def _allow_request(self, link_id: bytes, now: float) -> bool:
        """Caller holds the lock. Per-link ceiling on inbound requests.

        Request traffic bypasses LXMF, so the router's control throttle
        never sees it; each request costs a lock and a file read.
        """
        times = self._serve_times.setdefault(link_id, [])
        times[:] = [t for t in times if now - t < NODE_SERVE_RATE_WINDOW]
        if len(times) >= NODE_SERVE_RATE_LIMIT:
            return False
        times.append(now)
        return True

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
        if not isinstance(payload, bytes):
            return None
        if len(payload) > MAX_SERVED_RESPONSE_BYTES:
            RNS.log(f"TrenchChat [nomad]: refusing to serve oversized "
                    f"{path} ({len(payload)} bytes)", RNS.LOG_WARNING)
            return None
        return payload
