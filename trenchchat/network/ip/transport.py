"""
The direct path as a Transport: one authenticated QUIC session per peer.

A listener accepts sessions from peers an eligibility gate lets in, and
open_session dials one to a peer whose address and certificate arrived over
Reticulum. Everything runs on one asyncio loop on one background thread, and
manager callbacks are handed to a small worker pool, so the contract every
manager already has (callbacks arrive on a background thread; the API layer
marshals them through EventBus) is the same on this path as on the mesh.

A session only ever carries messages from the identity that proved itself on
it: an inbound envelope is checked against the session's authenticated peer
and against the author's own signature before a handler sees it.
"""

import asyncio
import socket
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import RNS

from trenchchat.config import Config
from trenchchat.network.base import (
    InboundMessage, PATH_DIRECT, PATH_RETICULUM, SendState, Transport,
    TransportLimits, direct_limits,
)
from trenchchat.network.ip import frames, session as session_mod
from trenchchat.network.ip.certificate import SessionCertificate
from trenchchat.network.ip.session import DirectSession, SessionHooks

DEFAULT_LISTEN_HOST = "0.0.0.0"

# The plan's session caps.
MAX_SESSIONS = 128
MAX_PENDING_HANDSHAKES = 16

# How long a message waits for its acknowledgement before the send is called
# failed, which is what sends it back over Reticulum once.
ACK_TIMEOUT_SECS = 30.0

# The sweep that expires acknowledgements and keeps idle sessions alive.
SWEEP_INTERVAL_SECS = 1.0

# Inbound messages held for one session while its handlers work. An
# authenticated member sending faster than this node can store is bounded
# here; how often they may send a control message is Router's business.
MAX_QUEUED_INBOUND = 256

# Requests one session may have being answered at once. A plane bounds its own
# work on top of this (the file plane counts concurrent serves); this is the
# floor under every plane, so a peer cannot make this node queue work simply by
# opening streams faster than it answers them.
MAX_INFLIGHT_REQUESTS = 8

CALLBACK_WORKERS = 4

OPEN_TIMEOUT_SECS = 20.0
STOP_TIMEOUT_SECS = 5.0
LOOP_START_TIMEOUT_SECS = 5.0


class _SerialQueue:
    """One session's inbound work, run in arrival order on a shared pool.

    A pool alone would let two messages from the same peer overtake each
    other, which no path should do; a thread each would not scale to the
    session cap.
    """

    def __init__(self, submit_to, limit: int = MAX_QUEUED_INBOUND):
        self._submit_to = submit_to
        self._limit = limit
        self._items: deque = deque()
        self._running = False
        self._lock = threading.Lock()

    def submit(self, fn, *args) -> bool:
        """Queue one item. False when the queue is full and it was dropped."""
        with self._lock:
            if len(self._items) >= self._limit:
                return False
            self._items.append((fn, args))
            if self._running:
                return True
            self._running = True
        if not self._submit_to(self._drain):
            with self._lock:
                self._running = False
            return False
        return True

    def _drain(self) -> None:
        while True:
            with self._lock:
                if not self._items:
                    self._running = False
                    return
                fn, args = self._items.popleft()
            try:
                fn(*args)
            except Exception as e:
                RNS.log(f"TrenchChat [ip]: inbound handler error: {e}", RNS.LOG_ERROR)


class IPTransport(Transport):
    """TrenchChat over a direct QUIC session between two peers."""

    def __init__(self, config: Config, identity, *, authorize=None,
                 listen_host: str = DEFAULT_LISTEN_HOST,
                 listen_port: int | None = None):
        """
        identity: trenchchat.core.identity.Identity instance
        (passed in to avoid circular imports)
        authorize: authorize(peer_hex) -> bool, consulted right after an
        inbound HELLO and before any frame is read. Without one no inbound
        session is accepted at all: a gate that is not wired is a gate that
        is shut.
        listen_port: 0 asks the kernel for a free port, which is what a test
        wants; None takes the configured one.
        """
        self._config = config
        self._identity = identity
        self._authorize = authorize or self._refuse_all
        self._certificate = SessionCertificate.load_or_create(config.data_dir)
        self._listen_host = listen_host
        self._listen_port = (config.upgrade_listen_port if listen_port is None
                             else listen_port)

        self._inbound_callback = None
        self._peer_appeared = None
        self._path_changed = None
        self._observed_callback = None
        self._datagram_callback = None
        self._request_handlers: dict = {}
        # (session id, request id) -> what to call with the answer.
        self._requests: dict[tuple[int, int], object] = {}
        self._inflight_requests: dict[int, int] = {}
        self._request_lock = threading.Lock()
        self._next_request_id = 0

        self._sessions: dict[str, DirectSession] = {}
        self._queues: dict[int, _SerialQueue] = {}
        self._sessions_lock = threading.Lock()
        self._pending_handshakes: set = set()
        # One-shot listeners on punched sockets, one per peer, closed with the
        # session they accepted. Closing one closes its socket.
        self._accept_servers: dict[str, object] = {}
        self._probe_responder = None
        self._stopped = False
        self._server = None
        self._sweep_task = None
        self._last_keepalive = 0.0

        self._pool = ThreadPoolExecutor(max_workers=CALLBACK_WORKERS,
                                        thread_name_prefix="ip-callbacks")
        self._inflight = 0
        self._inflight_lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        self._loop_ready = threading.Event()
        self._thread = threading.Thread(target=self._run_loop, daemon=True,
                                        name="ip-transport")
        self._thread.start()
        self._loop_ready.wait(LOOP_START_TIMEOUT_SECS)
        self._start_listener()

    # --- lifecycle ---

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.call_soon(self._loop_ready.set)
        try:
            self._loop.run_forever()
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    def _on_loop(self, coroutine, timeout: float):
        """Run one coroutine on the transport's loop and wait for its result."""
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        return future.result(timeout=timeout)

    def _start_listener(self) -> None:
        """Bind the listening socket, or carry on able to dial only."""
        try:
            self._on_loop(self._listen(), LOOP_START_TIMEOUT_SECS)
        except Exception as e:
            RNS.log(f"TrenchChat [ip]: not listening on "
                    f"{self._listen_host}:{self._listen_port}: {e}",
                    RNS.LOG_WARNING)
            self._listen_port = 0

    async def _listen(self) -> None:
        sock = session_mod.bind_datagram_socket(self._listen_host,
                                                self._listen_port)
        self._listen_port = sock.getsockname()[1]
        self._server = await session_mod.create_listener(
            sock,
            session_mod.listener_configuration(self._certificate),
            self._accept,
            probe_handler=self._on_listen_datagram,
        )
        self._sweep_task = asyncio.ensure_future(self._sweep())
        RNS.log(f"TrenchChat [ip]: listening on {self._listen_host}:"
                f"{self._listen_port}", RNS.LOG_NOTICE)

    async def _sweep(self) -> None:
        """Expire unacknowledged sends and keep idle sessions from timing out."""
        while not self._stopped:
            await asyncio.sleep(SWEEP_INTERVAL_SECS)
            now = time.time()
            keepalive = now - self._last_keepalive >= session_mod.KEEPALIVE_SECS
            if keepalive:
                self._last_keepalive = now
            for peer_session in self.live_sessions():
                try:
                    peer_session.expire_pending(ACK_TIMEOUT_SECS)
                    if keepalive:
                        peer_session.keepalive()
                except Exception as e:
                    RNS.log(f"TrenchChat [ip]: session sweep error: {e}",
                            RNS.LOG_ERROR)

    def stop(self) -> None:
        """Close every session, stop listening, and join the loop thread."""
        if self._stopped:
            return
        self._stopped = True
        try:
            self._on_loop(self._shutdown(), STOP_TIMEOUT_SECS)
        except Exception as e:
            RNS.log(f"TrenchChat [ip]: shutdown error: {e}", RNS.LOG_DEBUG)
        self._wait_for_callbacks()
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except RuntimeError:
            pass
        self._thread.join(timeout=STOP_TIMEOUT_SECS)
        self._pool.shutdown(wait=False)

    def _wait_for_callbacks(self) -> None:
        """Let work already handed to the pool finish before anything is torn down.

        A handler holds a Storage, and sqlite does not raise when its
        connection closes under another thread; it faults the interpreter. So
        stopping waits, bounded, rather than pulling the floor out.
        """
        deadline = time.time() + STOP_TIMEOUT_SECS
        while time.time() < deadline:
            with self._inflight_lock:
                if self._inflight == 0:
                    return
            time.sleep(0.01)
        RNS.log("TrenchChat [ip]: callbacks still running at shutdown",
                RNS.LOG_WARNING)

    async def _shutdown(self) -> None:
        for peer_session in self.live_sessions():
            peer_session.shut_down("this node is stopping")
        for pending in list(self._pending_handshakes):
            pending.shut_down("this node is stopping")
        self._pending_handshakes.clear()
        for peer_hex in list(self._accept_servers):
            self._close_accept_server(peer_hex)
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            self._sweep_task = None
        if self._server is not None:
            self._server.close()
            self._server = None
        # One turn of the loop, so the close frames actually go out.
        await asyncio.sleep(0)

    # --- sessions ---

    @property
    def listen_port(self) -> int:
        """The port sessions arrive on, or 0 when this node is not listening."""
        return self._listen_port

    @property
    def certificate_der(self) -> bytes:
        """This node's session certificate, as a peer pins it."""
        return self._certificate.der

    @property
    def certificate_fingerprint(self) -> bytes:
        """SHA-256 over the certificate, which every HELLO signature covers."""
        return self._certificate.fingerprint

    def set_authorize(self, authorize) -> None:
        """Replace the gate an inbound HELLO is held to."""
        self._authorize = authorize or self._refuse_all

    @staticmethod
    def _refuse_all(peer_hex: str) -> bool:
        RNS.log(f"TrenchChat [ip]: refusing {peer_hex[:12]}…: no eligibility "
                f"gate is wired", RNS.LOG_WARNING)
        return False

    def live_sessions(self) -> list[DirectSession]:
        """Every session currently up, as a snapshot."""
        with self._sessions_lock:
            return list(self._sessions.values())

    def sessions(self) -> list[dict]:
        """What this node knows about each of its own sessions."""
        return [peer_session.stats() for peer_session in self.live_sessions()]

    def session_for(self, peer_hex: str) -> DirectSession | None:
        """The session with one peer, if there is one."""
        with self._sessions_lock:
            return self._sessions.get(peer_hex)

    def open_session(self, peer_hex: str, host: str, port: int,
                     peer_cert_der: bytes, *, sock: socket.socket | None = None,
                     timeout: float = OPEN_TIMEOUT_SECS) -> bool:
        """Dial one peer whose address and certificate are already known.

        Blocks the calling thread until the session is up or the attempt
        fails, so callers use a thread of their own rather than an RNS one.
        Phase 3 passes the socket its punch opened as sock.
        """
        if self._stopped:
            return False
        if peer_hex == self._identity.hash_hex:
            return False
        if self.can_reach(peer_hex):
            return True
        try:
            return bool(self._on_loop(
                self._dial(peer_hex, host, port, peer_cert_der, sock), timeout))
        except Exception as e:
            RNS.log(f"TrenchChat [ip]: could not open a session with "
                    f"{peer_hex[:12]}…: {e}", RNS.LOG_WARNING)
            return False

    async def _dial(self, peer_hex: str, host: str, port: int,
                    peer_cert_der: bytes, sock: socket.socket | None) -> bool:
        if self.session_count() >= MAX_SESSIONS:
            RNS.log(f"TrenchChat [ip]: refusing to dial {peer_hex[:12]}…: at "
                    f"the {MAX_SESSIONS} session cap", RNS.LOG_WARNING)
            return False
        configuration = session_mod.dialer_configuration(self._certificate,
                                                         peer_cert_der)
        opened = await session_mod.dial_session(
            host, port, configuration,
            lambda connection: DirectSession(
                connection, identity=self._identity,
                certificate=self._certificate, hooks=self._hooks(),
                is_client=True, expected_peer_hex=peer_hex,
                peer_cert_der=peer_cert_der),
            sock=sock,
        )
        return opened.authenticated

    def close_session(self, peer_hex: str, reason: str = "closed locally") -> bool:
        """Close the session with one peer. False when there was none."""
        peer_session = self.session_for(peer_hex)
        if peer_session is None:
            return False
        try:
            self._loop.call_soon_threadsafe(peer_session.shut_down, reason)
        except RuntimeError:
            return False
        return True

    def accept_on(self, sock: socket.socket, peer_hex: str,
                  peer_cert_der: bytes = b"",
                  timeout: float = OPEN_TIMEOUT_SECS) -> bool:
        """Take one inbound session from a peer on a socket that has been punched.

        The other half of open_session: the side that does not dial still has
        to be listening on the socket whose mapping the punch opened, because
        the peer's first QUIC packet is addressed to it and to nothing else.
        Only *peer_hex* is let in, on top of whatever gate this transport
        already holds an inbound HELLO to, and only asserting the certificate
        it offered over Reticulum when *peer_cert_der* names one.

        Takes ownership of the socket, which is closed with the session it
        carried or when this call gives up. Blocks the calling thread, so
        callers use a thread of their own rather than an RNS one. A session
        that comes up by some other route in the meantime counts: the point is
        that this peer is reachable, not which socket carried it.
        """
        if self._stopped or peer_hex == self._identity.hash_hex:
            sock.close()
            return False
        if self.can_reach(peer_hex):
            sock.close()
            return True
        try:
            self._on_loop(self._listen_once(sock, peer_hex, peer_cert_der),
                          LOOP_START_TIMEOUT_SECS)
        except Exception as e:
            RNS.log(f"TrenchChat [ip]: could not listen for {peer_hex[:12]}…: "
                    f"{e}", RNS.LOG_WARNING)
            sock.close()
            return False
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.can_reach(peer_hex):
                return True
            time.sleep(0.05)
        try:
            self._loop.call_soon_threadsafe(self._close_accept_server, peer_hex)
        except RuntimeError:
            pass
        return False

    async def _listen_once(self, sock: socket.socket, peer_hex: str,
                           peer_cert_der: bytes) -> None:
        """Put a listener on one punched socket, replacing any it already had."""
        self._close_accept_server(peer_hex)
        server = await session_mod.create_listener(
            sock,
            session_mod.listener_configuration(self._certificate),
            lambda connection, stream_handler=None: self._accept(
                connection, stream_handler, expected_peer_hex=peer_hex,
                peer_cert_der=peer_cert_der),
            probe_handler=self._on_listen_datagram,
        )
        self._accept_servers[peer_hex] = server

    def _close_accept_server(self, peer_hex: str) -> None:
        """Drop a one-shot listener and the socket under it."""
        server = self._accept_servers.pop(peer_hex, None)
        if server is None:
            return
        try:
            server.close()
        except Exception as e:
            RNS.log(f"TrenchChat [ip]: could not close a punched listener: {e}",
                    RNS.LOG_DEBUG)

    def set_probe_responder(self, responder) -> None:
        """Register what answers a punch probe arriving on the listening socket.

        responder(data, addr, send) -> bool, called on the transport's loop for
        every datagram before QUIC sees it, and returning whether it took it.
        """
        self._probe_responder = responder

    def _on_listen_datagram(self, data: bytes, addr, send) -> bool:
        """Hand one datagram to the probe responder, if there is one."""
        responder = self._probe_responder
        return bool(responder(data, addr, send)) if responder is not None else False

    def session_count(self) -> int:
        """How many sessions are up."""
        with self._sessions_lock:
            return len(self._sessions)

    def pending_handshake_count(self) -> int:
        """How many connections are mid-handshake and not yet proven."""
        return len(self._pending_handshakes)

    def _accept(self, connection, stream_handler=None, *,
                expected_peer_hex: str = "",
                peer_cert_der: bytes = b"") -> DirectSession:
        """Build the session for one inbound connection, within the caps.

        expected_peer_hex narrows a punched socket to the one peer it was
        punched with; the listening socket takes anyone the gate allows.
        """
        refuse = ""
        if len(self._pending_handshakes) >= MAX_PENDING_HANDSHAKES:
            refuse = (f"at the {MAX_PENDING_HANDSHAKES} pending handshake cap")
        elif self.session_count() >= MAX_SESSIONS:
            refuse = f"at the {MAX_SESSIONS} session cap"
        inbound = DirectSession(
            connection, stream_handler=stream_handler, identity=self._identity,
            certificate=self._certificate,
            hooks=self._hooks(expected_peer_hex), is_client=False,
            peer_cert_der=peer_cert_der, refuse=refuse,
        )
        if not refuse:
            self._pending_handshakes.add(inbound)
        return inbound

    def _hooks(self, expected_peer_hex: str = "") -> SessionHooks:
        return SessionHooks(
            authorize=(self._authorize if not expected_peer_hex
                       else lambda peer_hex: (peer_hex == expected_peer_hex
                                              and self._authorize(peer_hex))),
            on_ready=self._on_ready,
            on_message=self._on_message,
            on_closed=self._on_closed,
            dispatch=self._dispatch,
            on_datagram=self._on_datagram,
            on_request=self._on_request,
            on_response=self._on_response,
            on_observed=self._on_observed,
        )

    def _on_ready(self, peer_session: DirectSession) -> None:
        """A session authenticated: it is this peer's path from now on."""
        self._pending_handshakes.discard(peer_session)
        peer_hex = peer_session.peer_hex
        with self._sessions_lock:
            previous = self._sessions.get(peer_hex)
            self._sessions[peer_hex] = peer_session
            self._queues[id(peer_session)] = _SerialQueue(self._submit)
        if previous is not None and previous is not peer_session:
            previous.shut_down("replaced by a newer session")
            return
        self._dispatch(self._fire_path_changed, peer_hex, PATH_DIRECT)
        self._dispatch(self._fire_peer_appeared, peer_hex)

    def _on_closed(self, peer_session: DirectSession, reason: str) -> None:
        """A session ended: the peer is back on whatever path is left."""
        self._pending_handshakes.discard(peer_session)
        self._fail_requests(peer_session)
        peer_hex = peer_session.peer_hex
        with self._sessions_lock:
            self._queues.pop(id(peer_session), None)
            if peer_hex and self._sessions.get(peer_hex) is peer_session:
                del self._sessions[peer_hex]
            else:
                peer_hex = ""
        if peer_hex:
            RNS.log(f"TrenchChat [ip]: session with {peer_hex[:12]}… ended: "
                    f"{reason}", RNS.LOG_NOTICE)
            self._close_accept_server(peer_hex)
            self._dispatch(self._fire_path_changed, peer_hex, PATH_RETICULUM)

    def _dispatch(self, fn, *args) -> None:
        """Hand one callback to the worker pool, off the connection's loop."""
        if fn is not None:
            self._submit(self._call, fn, *args)

    def _submit(self, fn, *args) -> bool:
        """Run one piece of work on the pool, counted so stop() can wait for it."""
        with self._inflight_lock:
            self._inflight += 1
        try:
            self._pool.submit(self._run, fn, *args)
        except RuntimeError:
            with self._inflight_lock:
                self._inflight -= 1
            return False
        return True

    def _run(self, fn, *args) -> None:
        try:
            fn(*args)
        finally:
            with self._inflight_lock:
                self._inflight -= 1

    @staticmethod
    def _call(fn, *args) -> None:
        try:
            fn(*args)
        except Exception as e:
            RNS.log(f"TrenchChat [ip]: callback error: {e}", RNS.LOG_ERROR)

    # --- inbound ---

    def _on_message(self, peer_session: DirectSession, envelope: bytes,
                    signature: bytes) -> None:
        """Queue one inbound message for this session, in arrival order."""
        queue = self._queues.get(id(peer_session))
        if queue is None:
            RNS.log(f"TrenchChat [ip]: dropped a message from "
                    f"{peer_session.peer_hex[:12]}…: its session has no inbound "
                    f"queue", RNS.LOG_WARNING)
            return
        if not queue.submit(self._accept_message, peer_session, envelope,
                            signature):
            RNS.log(f"TrenchChat [ip]: dropped a message from "
                    f"{peer_session.peer_hex[:12]}…: {MAX_QUEUED_INBOUND} "
                    f"already waiting", RNS.LOG_WARNING)

    def _accept_message(self, peer_session: DirectSession, envelope: bytes,
                        signature: bytes) -> None:
        """Check one inbound envelope and hand it up. Runs on a worker thread."""
        peer_hex = peer_session.peer_hex
        try:
            parsed = frames.unpack_envelope(envelope)
        except frames.FrameError as e:
            RNS.log(f"TrenchChat [ip]: dropped an unreadable envelope from "
                    f"{peer_hex[:12]}…: {e}", RNS.LOG_WARNING)
            return
        source_hex = parsed["src"].hex()
        if source_hex != peer_hex:
            RNS.log(f"TrenchChat [ip]: dropped a message claiming "
                    f"{source_hex[:12]}… on {peer_hex[:12]}…'s session",
                    RNS.LOG_WARNING)
            return
        if parsed["dst"] != self._identity.hash:
            RNS.log(f"TrenchChat [ip]: dropped a message from {peer_hex[:12]}… "
                    f"addressed to someone else", RNS.LOG_WARNING)
            return
        if not peer_session.verify(signature, frames.envelope_digest(envelope)):
            RNS.log(f"TrenchChat [ip]: dropped a message from {peer_hex[:12]}… "
                    f"whose signature does not verify", RNS.LOG_WARNING)
            return
        message = InboundMessage(
            source_hex=source_hex,
            fields=parsed["fields"],
            content=parsed["content"],
            timestamp=float(parsed["ts"]),
            hash=frames.envelope_hash(envelope),
            trenchchat_protocol=bool(parsed["proto"]),
            path=PATH_DIRECT,
        )
        callback = self._inbound_callback
        if callback is not None:
            callback(message)
        try:
            self._loop.call_soon_threadsafe(peer_session.send_ack, message.hash)
        except RuntimeError:
            pass

    def set_inbound_callback(self, callback) -> None:
        """Register the single callback every authenticated message arrives on."""
        self._inbound_callback = callback

    # --- requests ---

    def set_request_handler(self, op: str, handler) -> None:
        """Register what answers one operation, or None to stop answering it.

        handler(peer_hex, payload) -> (ok, payload), called on a worker thread
        with the identity the session proved. A plane registers one of these
        rather than reaching into a session.
        """
        if handler is None:
            self._request_handlers.pop(op, None)
        else:
            self._request_handlers[op] = handler

    def send_request(self, peer_hex: str, op: str, payload: dict,
                     on_result) -> int | None:
        """Put one request on a peer's session. None when there is no session.

        on_result(ok, payload) is called on a worker thread with the answer, or
        with (False, {}) if the session ends before one arrives.
        """
        peer_session = self.session_for(peer_hex)
        if peer_session is None or not peer_session.authenticated:
            return None
        with self._request_lock:
            self._next_request_id += 1
            request_id = self._next_request_id
            self._requests[(id(peer_session), request_id)] = on_result
        try:
            self._loop.call_soon_threadsafe(peer_session.open_request,
                                            request_id, op, payload)
        except RuntimeError:
            with self._request_lock:
                self._requests.pop((id(peer_session), request_id), None)
            return None
        return request_id

    def _on_request(self, peer_session: DirectSession, stream_id: int,
                    request_id: int, op: str, payload: dict) -> None:
        """One inbound request, answered off the loop or refused outright."""
        handler = self._request_handlers.get(op)
        key = id(peer_session)
        if handler is None:
            peer_session.send_response(stream_id, request_id, False, {})
            return
        with self._request_lock:
            live = self._inflight_requests.get(key, 0)
            if live >= MAX_INFLIGHT_REQUESTS:
                RNS.log(f"TrenchChat [ip]: refusing a request from "
                        f"{peer_session.peer_hex[:12]}…: {MAX_INFLIGHT_REQUESTS} "
                        f"already in flight", RNS.LOG_WARNING)
                peer_session.send_response(stream_id, request_id, False, {})
                return
            self._inflight_requests[key] = live + 1
        if not self._submit(self._answer_request, peer_session, stream_id,
                            request_id, handler, payload):
            self._release_request(key)

    def _answer_request(self, peer_session: DirectSession, stream_id: int,
                        request_id: int, handler, payload: dict) -> None:
        """Run one request handler and write its answer back. On a worker thread."""
        ok, body = False, {}
        try:
            ok, body = handler(peer_session.peer_hex, payload)
        except Exception as e:
            RNS.log(f"TrenchChat [ip]: request handler error for "
                    f"{peer_session.peer_hex[:12]}…: {e}", RNS.LOG_ERROR)
        finally:
            self._release_request(id(peer_session))
        try:
            self._loop.call_soon_threadsafe(peer_session.send_response,
                                            stream_id, request_id, bool(ok),
                                            body or {})
        except RuntimeError:
            pass

    def _release_request(self, key: int) -> None:
        """Give one of a session's in-flight request slots back."""
        with self._request_lock:
            live = self._inflight_requests.get(key, 0) - 1
            if live > 0:
                self._inflight_requests[key] = live
            else:
                self._inflight_requests.pop(key, None)

    def _on_response(self, peer_session: DirectSession, request_id: int,
                     ok: bool, payload: dict) -> None:
        """One answer, handed to whoever asked."""
        with self._request_lock:
            on_result = self._requests.pop((id(peer_session), request_id), None)
        if on_result is not None:
            self._dispatch(on_result, ok, payload)

    def _fail_requests(self, peer_session: DirectSession) -> None:
        """Answer everything outstanding on a session that has ended."""
        key = id(peer_session)
        with self._request_lock:
            stranded = [(request_key, cb) for request_key, cb
                        in self._requests.items() if request_key[0] == key]
            for request_key, _cb in stranded:
                del self._requests[request_key]
            self._inflight_requests.pop(key, None)
        for _request_key, on_result in stranded:
            self._dispatch(on_result, False, {})

    # --- datagrams ---

    def set_datagram_callback(self, callback) -> None:
        """Register what receives a session's unreliable datagrams.

        callback(peer_hex, payload), called on the transport's loop: the voice
        plane's frames arrive here and a jitter buffer push must not wait on a
        worker.
        """
        self._datagram_callback = callback

    def send_datagram(self, peer_hex: str, payload: bytes) -> bool:
        """Send one unreliable datagram to a peer. False without a session."""
        peer_session = self.session_for(peer_hex)
        if peer_session is None:
            return False
        try:
            self._loop.call_soon_threadsafe(peer_session.send_datagram, payload)
        except RuntimeError:
            return False
        return True

    def _on_datagram(self, peer_session: DirectSession, payload: bytes) -> None:
        callback = self._datagram_callback
        if callback is not None:
            callback(peer_session.peer_hex, payload)

    # --- observed addresses ---

    def set_observed_callback(self, callback) -> None:
        """Register what learns this node's own translated address.

        callback(peer_hex, host, port): where that peer saw this node arrive
        from, which is the one thing a node behind a NAT cannot work out for
        itself.
        """
        self._observed_callback = callback

    def _on_observed(self, peer_session: DirectSession, host: str,
                     port: int) -> None:
        callback = self._observed_callback
        if callback is not None:
            callback(peer_session.peer_hex, host, port)

    # --- send ---

    def send(self, dest_hex: str, fields: dict, content: str = "", *,
             on_delivered=None, on_failed=None, propagated: bool = False,
             envelope: bool = True) -> SendState:
        """Write one message to this peer's session. NO_PATH if there is none.

        A propagated message is never carried here: leaving mail with a node
        for a peer who is not there is Reticulum's own, and a peer with a
        session up is here.
        """
        if propagated:
            return SendState.NO_PATH
        peer_session = self.session_for(dest_hex)
        if peer_session is None:
            return SendState.NO_PATH
        try:
            packed = frames.pack_envelope(
                src=self._identity.hash, dst=bytes.fromhex(dest_hex),
                timestamp=time.time(), content=content, fields=fields,
                protocol=envelope,
            )
            signature = self._identity.rns_identity.sign(
                frames.envelope_digest(packed))
        except (ValueError, TypeError) as e:
            RNS.log(f"TrenchChat [ip]: could not build a message for "
                    f"{dest_hex[:12]}…: {e}", RNS.LOG_WARNING)
            return SendState.NO_PATH
        try:
            self._loop.call_soon_threadsafe(
                peer_session.send_message, packed, signature, on_delivered,
                on_failed)
        except RuntimeError:
            return SendState.NO_PATH
        return SendState.SENT

    def can_reach(self, dest_hex: str) -> bool:
        """Whether a session with this peer is up."""
        peer_session = self.session_for(dest_hex)
        return peer_session is not None and peer_session.authenticated

    def request_path(self, dest_hex: str) -> None:
        """Nothing to ask: a direct session exists or it does not."""

    def limits_for(self, dest_hex: str) -> TransportLimits:
        """The direct path's budgets. The same for every peer on it."""
        return direct_limits()

    def drain(self, timeout: float) -> int:
        """Wait for outstanding acknowledgements. Returns how many settled."""
        outstanding = sum(s.pending_acks() for s in self.live_sessions())
        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = sum(s.pending_acks() for s in self.live_sessions())
            if remaining == 0:
                return outstanding
            time.sleep(0.05)
        remaining = sum(s.pending_acks() for s in self.live_sessions())
        return max(outstanding - remaining, 0)

    # --- peer keys and events ---

    def public_key_for(self, peer_hex: str) -> bytes | None:
        """The peer's public key as their HELLO proved it, or None."""
        peer_session = self.session_for(peer_hex)
        if peer_session is None or not peer_session.peer_public_key:
            return None
        return peer_session.peer_public_key

    def set_peer_event_callbacks(self, *, peer_appeared=None,
                                 identity_resolved=None, channel_discovered=None,
                                 user_discovered=None, node_discovered=None,
                                 propagation_node_heard=None,
                                 path_changed=None) -> None:
        """Register the two events this path has: a peer appearing, and its path."""
        self._peer_appeared = peer_appeared
        self._path_changed = path_changed

    def _fire_peer_appeared(self, peer_hex: str) -> None:
        if self._peer_appeared is not None:
            self._peer_appeared(peer_hex, None)

    def _fire_path_changed(self, peer_hex: str, path: str) -> None:
        if self._path_changed is not None:
            self._path_changed(peer_hex, path)
