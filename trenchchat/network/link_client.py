"""
Shared RNS Link client: dial ladder, per-peer request queue, request plumbing.

Two planes here pull bytes from a peer with Link.request: the nomad node plane
(node_transport.py) and the file plane (file_transport.py). Both dial with a
backoff ladder, keep one link per peer, queue requests behind the handshake,
retry once on a link the remote already dropped, and tear down idle links.
That machinery lives here.

What differs stays in the module that owns it: how a peer's destination is
derived, whether a link identifies, what a response looks like, and what
counts as a stalled request. Those are the four hooks a subclass fills in.

This module never touches Storage or core managers, matching
voice_transport.py's layering: callbacks up, tick() down.
"""

import threading
import time

import RNS

LINK_REDIAL_BACKOFF = (2.0, 5.0, 10.0, 30.0)
LINK_IDLE_SECS = 300.0
MAX_QUEUED_FETCHES_PER_PEER = 8

SERVE_RATE_LIMIT = 8
SERVE_RATE_WINDOW = 1.0
MAX_INBOUND_LINKS = 32
MAX_SERVED_RESPONSE_BYTES = 5 * 1024 * 1024

# Fetch failure reasons surfaced through the result callback.
FETCH_BAD_PATH = "bad_path"           # the request itself is malformed
FETCH_BUSY = "busy"
FETCH_UNREACHABLE = "unreachable"
FETCH_TIMEOUT = "timeout"
FETCH_TOO_LARGE = "too_large"
FETCH_LINK_CLOSED = "link_closed"
FETCH_SEND_FAILED = "send_failed"
FETCH_BAD_RESPONSE = "bad_response"
FETCH_NOT_FOUND = "not_found"
FETCH_IDENTITY_MISMATCH = "identity_mismatch"

# Internal connection states.
_IDLE = "idle"
_DIALING = "dialing"
_LINKED = "linked"


def link_is_usable(link) -> bool:
    """Whether a cached link can still carry a request.

    A link object without a status (a test double) counts as usable.
    """
    status = getattr(link, "status", None)
    return status is None or status == RNS.Link.ACTIVE


class LinkFetch:
    """One queued or in-flight request on a peer's link."""

    def __init__(self, fetch_id: str, peer_hex: str, path: str,
                 max_size: int, timeout: float, data=None):
        self.fetch_id = fetch_id
        self.peer_hex = peer_hex
        self.path = path
        self.max_size = max_size
        self.timeout = timeout
        self.data = data
        self.created_at = time.time()
        self.last_progress_at = self.created_at
        self.receipt = None
        self.redialed = False
        self.transferred = False


class LinkConn:
    """Bookkeeping for one peer's link across dial/fetch/redial cycles."""

    backoff = LINK_REDIAL_BACKOFF

    def __init__(self, peer_hex: str):
        self.peer_hex = peer_hex
        self.state = _IDLE
        self.link = None
        self.dial_attempts = 0
        self.next_dial_at = 0.0
        self.last_used = time.time()
        self.queued: list[LinkFetch] = []
        # Per link, not per peer: a new link starts anonymous again.
        self.identified = False

    @property
    def exhausted(self) -> bool:
        return self.dial_attempts >= len(self.backoff)


class LinkClientBase:
    """Injectable base: the result and progress callbacks, and nothing else."""

    log_tag = "link"

    def __init__(self):
        self._result_cb = None
        self._progress_cb = None

    def _notify_result(self, fetch_id: str, ok: bool, payload: bytes | None,
                       reason: str | None, *extra) -> None:
        if self._result_cb is None:
            return
        try:
            self._result_cb(fetch_id, ok, payload, reason, *extra)
        except Exception as e:
            RNS.log(f"TrenchChat [{self.log_tag}]: result callback error: {e}",
                    RNS.LOG_ERROR)

    def _notify_progress(self, fetch_id: str, progress: float) -> None:
        if self._progress_cb is None:
            return
        try:
            self._progress_cb(fetch_id, float(progress))
        except Exception as e:
            RNS.log(f"TrenchChat [{self.log_tag}]: progress callback error: "
                    f"{e}", RNS.LOG_ERROR)


class LinkClient(LinkClientBase):
    """The dial, queue and request engine both RNS link planes run on."""

    conn_class = LinkConn
    link_idle_secs = LINK_IDLE_SECS
    max_queued_per_peer = MAX_QUEUED_FETCHES_PER_PEER
    max_inbound_links = MAX_INBOUND_LINKS
    serve_rate_limit = SERVE_RATE_LIMIT
    serve_rate_window = SERVE_RATE_WINDOW

    def __init__(self, identity):
        super().__init__()
        self._identity = identity
        self._lock = threading.RLock()
        self._conns: dict[str, LinkConn] = {}
        self._by_link: dict[int, str] = {}
        self._active: dict[int, LinkFetch] = {}     # id(receipt) -> fetch
        self._inbound_links: dict[int, tuple] = {}  # id(link) -> (link, ts)
        self._serve_times: dict[bytes, list[float]] = {}

    # --- hooks ---

    def _dial_destination(self, peer_hex: str):
        """The peer's outbound destination for this plane.

        Returns (destination, None) to dial it, (None, None) to defer the
        dial until a requested path resolves, or (None, reason) to fail
        everything queued for the peer.
        """
        raise NotImplementedError

    def _should_identify(self, peer_hex: str) -> bool:
        """Whether a link to this peer carries our identity."""
        return False

    def _handle_response(self, fetch: LinkFetch, receipt) -> None:
        """Turn a completed request receipt into a result callback."""
        raise NotImplementedError

    def _sweep_active(self, now: float) -> list[tuple[LinkFetch, str]]:
        """Caller holds the lock. In-flight fetches to fail, and why."""
        return []

    # --- queueing ---

    def _enqueue(self, fetch: LinkFetch) -> None:
        """Queue a request, dialing or flushing the peer's link as needed."""
        dial_now = False
        flush_now = False
        with self._lock:
            conn = self._conns.get(fetch.peer_hex)
            if conn is None:
                conn = self.conn_class(fetch.peer_hex)
                self._conns[fetch.peer_hex] = conn
            active_here = sum(1 for f in self._active.values()
                              if f.peer_hex == fetch.peer_hex)
            if len(conn.queued) + active_here >= self.max_queued_per_peer:
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
            self._notify_result(fetch.fetch_id, False, None, FETCH_BUSY)
            return
        if flush_now:
            self._flush_queued(fetch.peer_hex)
        elif dial_now:
            self._dial(fetch.peer_hex)

    def cancel(self, fetch_id: str) -> None:
        with self._lock:
            for conn in self._conns.values():
                conn.queued = [f for f in conn.queued if f.fetch_id != fetch_id]
            for key, fetch in list(self._active.items()):
                if fetch.fetch_id == fetch_id:
                    del self._active[key]

    # --- outbound dialing ---

    def _dial(self, peer_hex: str) -> None:
        with self._lock:
            conn = self._conns.get(peer_hex)
            if conn is None or conn.state != _IDLE:
                return
            conn.state = _DIALING

        try:
            dest, reason = self._dial_destination(peer_hex)
            if dest is None:
                if reason is None:
                    self._defer_dial(peer_hex)
                else:
                    self._fail_conn(peer_hex, reason)
                return
            link = RNS.Link(
                dest,
                established_callback=self._on_outbound_established,
                closed_callback=self._on_link_closed,
            )
            with self._lock:
                conn = self._conns.get(peer_hex)
                if conn is None:
                    self._teardown_link(link)
                    return
                conn.link = link
                self._by_link[id(link)] = peer_hex
        except Exception as e:
            RNS.log(f"TrenchChat [{self.log_tag}]: dial to {peer_hex[:12]}… "
                    f"failed: {e}", RNS.LOG_WARNING)
            self._defer_dial(peer_hex)

    def _redial_for(self, fetch: LinkFetch) -> bool:
        """Requeue a fetch that burnt on a dead link and dial a fresh one.

        A link the remote has already dropped still looks established here
        until RNS notices, so the first request on it fails to send. Retry
        once per fetch; a second failure is a real one. When a batch burns
        together, the first fetch triggers the redial and the rest only
        requeue, so they cannot clobber the dial in progress.
        """
        if fetch.redialed:
            return False
        fetch.redialed = True
        link = None
        redial = False
        with self._lock:
            conn = self._conns.get(fetch.peer_hex)
            if conn is None:
                return False
            if conn.state != _DIALING:
                if conn.link is not None:
                    self._by_link.pop(id(conn.link), None)
                    link = conn.link
                    conn.link = None
                conn.state = _IDLE
                conn.identified = False
                conn.dial_attempts = 0
                conn.next_dial_at = 0.0
                redial = True
            conn.queued.append(fetch)
        self._teardown_link(link)
        if redial:
            RNS.log(f"TrenchChat [{self.log_tag}]: link to "
                    f"{fetch.peer_hex[:12]}… was dead, redialing",
                    RNS.LOG_DEBUG)
            self._dial(fetch.peer_hex)
        return True

    def _defer_dial(self, peer_hex: str) -> None:
        # Exhaustion never fails queued fetches here: on a cold path, RNS
        # resolution can outlast the whole backoff ladder while the peer is
        # up. Each fetch's own timeout is the promise; dialing just settles
        # to the backoff tail until then.
        with self._lock:
            conn = self._conns.get(peer_hex)
            if conn is None:
                return
            self._register_link_failure(conn)

    def _register_link_failure(self, conn: LinkConn, *,
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
        conn.identified = False
        if not arm_backoff:
            conn.dial_attempts = 0
            conn.next_dial_at = 0.0
            return
        backoff = conn.backoff[min(conn.dial_attempts, len(conn.backoff) - 1)]
        conn.dial_attempts += 1
        conn.next_dial_at = time.time() + backoff

    def _fail_conn(self, peer_hex: str, reason: str) -> None:
        """Fail everything queued or active for a peer and drop it."""
        failed: list[LinkFetch] = []
        link = None
        with self._lock:
            conn = self._conns.pop(peer_hex, None)
            if conn is None:
                return
            failed.extend(conn.queued)
            conn.queued = []
            for key, fetch in list(self._active.items()):
                if fetch.peer_hex == peer_hex:
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
            peer_hex = self._by_link.get(id(link))
            conn = self._conns.get(peer_hex) if peer_hex else None
            if conn is None:
                self._teardown_link(link)
                return
            conn.state = _LINKED
            conn.dial_attempts = 0
        # Before the queued requests, so the peer has our identity by the
        # time it answers the first one.
        if self._should_identify(peer_hex):
            self._identify_on(peer_hex, link)
        self._flush_queued(peer_hex)

    def _identify_on(self, peer_hex: str, link) -> bool:
        """Send the link-identify proof and record that this link carries it."""
        try:
            link.identify(self._identity.rns_identity)
        except Exception as e:
            RNS.log(f"TrenchChat [{self.log_tag}]: could not identify to "
                    f"{peer_hex[:12]}…: {e}", RNS.LOG_WARNING)
            return False
        with self._lock:
            conn = self._conns.get(peer_hex)
            if conn is not None and conn.link is link:
                conn.identified = True
        RNS.log(f"TrenchChat [{self.log_tag}]: identified to {peer_hex[:12]}…",
                RNS.LOG_NOTICE)
        return True

    def is_identified(self, peer_hash_hex: str) -> bool:
        """Whether the link currently open to a peer carries our identity."""
        with self._lock:
            conn = self._conns.get(peer_hash_hex)
            return bool(conn is not None and conn.state == _LINKED
                        and conn.identified)

    def drop_link(self, peer_hash_hex: str) -> bool:
        """Close the link to a peer, so the next request opens a fresh one.

        A link cannot un-identify: the proof is sent once and the peer reads
        it on every request the link carries. Dropping it is the only way to
        stop identifying without waiting for the idle timeout. Queued and
        in-flight fetches are left alone: they fail or finish on the closing
        link.
        """
        with self._lock:
            conn = self._conns.get(peer_hash_hex)
            if conn is None or conn.link is None:
                return False
            link = conn.link
            self._by_link.pop(id(link), None)
            conn.link = None
            conn.state = _IDLE
            conn.identified = False
        self._teardown_link(link)
        return True

    def _flush_queued(self, peer_hex: str) -> None:
        dead = None
        with self._lock:
            conn = self._conns.get(peer_hex)
            if conn is None or conn.state != _LINKED or conn.link is None:
                return
            if not link_is_usable(conn.link):
                # RNS noticed the link dying but the closed callback has not
                # landed yet; dial fresh instead of burning the queue on it.
                self._by_link.pop(id(conn.link), None)
                dead = conn.link
                conn.link = None
                conn.state = _IDLE
                conn.identified = False
                conn.next_dial_at = 0.0
            else:
                to_send = conn.queued
                conn.queued = []
                link = conn.link
                conn.last_used = time.time()
        if dead is not None:
            self._teardown_link(dead)
            self._dial(peer_hex)
            return
        for fetch in to_send:
            self._issue_request(link, fetch)

    def _issue_request(self, link, fetch: LinkFetch) -> None:
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
            RNS.log(f"TrenchChat [{self.log_tag}]: request send failed: {e}",
                    RNS.LOG_WARNING)
            receipt = False
        if receipt is False or receipt is None:
            if self._redial_for(fetch):
                return
            self._notify_result(fetch.fetch_id, False, None, FETCH_SEND_FAILED)
            return
        fetch.receipt = receipt
        fetch.last_progress_at = time.time()
        with self._lock:
            self._active[id(receipt)] = fetch

    # --- request callbacks (RNS threads) ---

    def _pop_active(self, receipt) -> LinkFetch | None:
        with self._lock:
            fetch = self._active.pop(id(receipt), None)
            if fetch is not None:
                conn = self._conns.get(fetch.peer_hex)
                if conn is not None:
                    conn.last_used = time.time()
            return fetch

    def _on_response(self, receipt) -> None:
        fetch = self._pop_active(receipt)
        if fetch is None:
            return
        self._handle_response(fetch, receipt)

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
            if fetch is not None:
                fetch.transferred = True
                fetch.last_progress_at = time.time()
        if fetch is None:
            return
        self._notify_progress(fetch.fetch_id, receipt.progress)

    # --- link close ---

    def _on_link_closed(self, link) -> None:
        # Outbound and inbound links share this callback; inbound links are
        # only tracked in _inbound_links.
        with self._lock:
            self._inbound_links.pop(id(link), None)
            peer_hex = self._by_link.pop(id(link), None)
            conn = self._conns.get(peer_hex) if peer_hex else None
            if conn is None or conn.link is not link:
                return
            failed = [f for f in self._active.values()
                      if f.peer_hex == peer_hex]
            for fetch in failed:
                self._active.pop(id(fetch.receipt), None)
            self._register_link_failure(conn, arm_backoff=False)
        for fetch in failed:
            # A request the link died under before any data moved gets the
            # same one redial a refused send does; a mid-transfer death is
            # a real failure.
            if not fetch.transferred and self._redial_for(fetch):
                continue
            self._notify_result(fetch.fetch_id, False, None, FETCH_LINK_CLOSED)

    def _teardown_link(self, link) -> None:
        if link is None:
            return
        try:
            link.teardown()
        except Exception as e:
            RNS.log(f"TrenchChat [{self.log_tag}]: link teardown error: {e}",
                    RNS.LOG_DEBUG)

    # --- housekeeping ---

    def _tick_links(self) -> None:
        """Dial what is due, fail what is past its deadline, drop idle links."""
        now = time.time()
        dial_needed: list[str] = []
        never_linked: list[LinkFetch] = []
        failed: list[tuple[LinkFetch, str]] = []
        idle_links: list = []

        with self._lock:
            for conn in list(self._conns.values()):
                if (conn.queued and conn.state == _IDLE
                        and now >= conn.next_dial_at):
                    dial_needed.append(conn.peer_hex)
                for fetch in list(conn.queued):
                    # Still queued at its deadline means no link was ever
                    # established for it: "unreachable" is the honest reason,
                    # "timeout" is kept for mid-transfer stalls.
                    if now - fetch.created_at >= fetch.timeout:
                        conn.queued.remove(fetch)
                        never_linked.append(fetch)
            for fetch, reason in self._sweep_active(now):
                self._active.pop(id(fetch.receipt), None)
                failed.append((fetch, reason))
            for conn in list(self._conns.values()):
                has_active = any(f.peer_hex == conn.peer_hex
                                 for f in self._active.values())
                if (conn.state == _LINKED and not conn.queued and not has_active
                        and now - conn.last_used >= self.link_idle_secs):
                    if conn.link is not None:
                        self._by_link.pop(id(conn.link), None)
                        idle_links.append(conn.link)
                    del self._conns[conn.peer_hex]
                elif (conn.state == _IDLE and not conn.queued
                        and now - conn.last_used >= self.link_idle_secs):
                    del self._conns[conn.peer_hex]
            for key, times in list(self._serve_times.items()):
                if not times or now - times[-1] > self.serve_rate_window:
                    del self._serve_times[key]

        for peer_hex in dial_needed:
            self._dial(peer_hex)
        for fetch in never_linked:
            self._notify_result(fetch.fetch_id, False, None, FETCH_UNREACHABLE)
        for fetch, reason in failed:
            self._notify_result(fetch.fetch_id, False, None, reason)
        for link in idle_links:
            self._teardown_link(link)

    # --- inbound links ---

    def _on_inbound_link(self, link) -> None:
        link.set_link_closed_callback(self._on_link_closed)
        evicted = None
        with self._lock:
            if len(self._inbound_links) >= self.max_inbound_links:
                oldest = min(self._inbound_links,
                             key=lambda k: self._inbound_links[k][1])
                evicted, _ = self._inbound_links.pop(oldest)
            self._inbound_links[id(link)] = (link, time.time())
        if evicted is not None:
            self._teardown_link(evicted)

    def _inbound_link(self, link_id):
        """Caller holds the lock. The inbound link RNS gave this link_id."""
        if link_id is None:
            return None
        for link, _ in self._inbound_links.values():
            if getattr(link, "link_id", None) == link_id:
                return link
        return None

    def _allow_request(self, link_id: bytes, now: float) -> bool:
        """Caller holds the lock. Per-link ceiling on inbound requests.

        Request traffic bypasses LXMF, so the router's control throttle never
        sees it; each request costs a lock and a read.
        """
        times = self._serve_times.setdefault(link_id, [])
        times[:] = [t for t in times if now - t < self.serve_rate_window]
        if len(times) >= self.serve_rate_limit:
            return False
        times.append(now)
        return True
