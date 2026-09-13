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

A peer's path and the path_changed that announces it are one step in both
directions: a handler is called with the new state already in place, and no
other thread can see a session come up or go away before that call has
returned. Anything less and a manager can act on a path this node has not
told it about, which is a race it has no way to see.
"""

import asyncio
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
from trenchchat.network.ip import frames, punch, session as session_mod
from trenchchat.network.ip.certificate import SessionCertificate
from trenchchat.network.ip.endpoint import DatagramEndpoint, bind_listen_sockets
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

# How long a thread waits for a path change to be announced before it reads the
# new state anyway. A path_changed handler slower than this has a problem of
# its own; holding every send on this node behind it would be a worse one.
PATH_ANNOUNCE_TIMEOUT_SECS = 5.0

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
        # peer hex -> the event a path change is announced behind. Held under
        # the same lock as the sessions, so "what is this peer's path" and "is
        # a change to it still unannounced" are read as one answer.
        self._path_gates: dict[str, threading.Event] = {}
        self._announcing = threading.local()
        self._pending_handshakes: set = set()
        # nonce -> the attempt waiting on datagrams carrying it.
        self._probe_channels: dict[bytes, punch.ProbeChannel] = {}
        self._stopped = False
        self._endpoint: DatagramEndpoint | None = None
        self._sweep_task = None
        self._last_keepalive = 0.0

        self._pool = ThreadPoolExecutor(max_workers=CALLBACK_WORKERS,
                                        thread_name_prefix="ip-callbacks")
        # Path changes get a thread of their own, in arrival order: on the
        # shared pool two of them could be announced out of order, and a
        # thread waiting for one could be waiting behind the work that
        # announces it.
        self._announcer = ThreadPoolExecutor(max_workers=1,
                                             thread_name_prefix="ip-paths")
        self._inflight = 0
        self._inflight_lock = threading.Lock()
        self._loop_thread_id = 0
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
        self._loop_thread_id = threading.get_ident()
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
        """Bind the endpoint, falling back to a port the kernel picks.

        Every session and every probe runs on this one endpoint, so a node
        that cannot bind it holds no sessions at all, in or out. A configured
        port that is refused is therefore worth a kernel-assigned one rather
        than nothing: a peer is told whichever port this node actually has.
        """
        wanted = self._listen_port
        for port in ([0] if wanted == 0 else [wanted, 0]):
            try:
                self._on_loop(self._listen(port), LOOP_START_TIMEOUT_SECS)
                return
            except Exception as e:
                RNS.log(f"TrenchChat [ip]: not listening on "
                        f"{self._listen_host}:{port}: {e}", RNS.LOG_WARNING)
        self._listen_port = 0

    async def _listen(self, port: int) -> None:
        sockets = bind_listen_sockets(self._listen_host, port)
        endpoint = DatagramEndpoint(
            session_mod.listener_configuration(self._certificate),
            self._accept, probe_router=self._route_probe)
        await endpoint.bind(sockets)
        self._endpoint = endpoint
        self._listen_port = endpoint.port
        if self._sweep_task is None:
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
        self._announcer.shutdown(wait=False)

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
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            self._sweep_task = None
        if self._endpoint is not None:
            self._endpoint.close()
            self._endpoint = None
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
        """The session with one peer, if there is one.

        A path change and the callback that announces it are one step: a
        thread that would see a session come up or go away waits here until
        every path_changed handler for it has run, so nothing on this node
        can act on a path it has not been told about. Two threads never wait:
        the one doing the announcing, which would be waiting for itself, and
        the loop, which carries every session here.
        """
        deadline = time.time() + PATH_ANNOUNCE_TIMEOUT_SECS
        while True:
            with self._sessions_lock:
                gate = self._path_gates.get(peer_hex)
                if gate is None or self._announces_its_own_paths():
                    return self._sessions.get(peer_hex)
            if not gate.wait(max(deadline - time.time(), 0.0)):
                RNS.log(f"TrenchChat [ip]: the path change for "
                        f"{peer_hex[:12]}… was still unannounced after "
                        f"{PATH_ANNOUNCE_TIMEOUT_SECS:.0f}s", RNS.LOG_WARNING)
                with self._sessions_lock:
                    return self._sessions.get(peer_hex)

    def _announces_its_own_paths(self) -> bool:
        """Whether this thread is one that must never wait for an announcement."""
        return (bool(getattr(self._announcing, "active", False))
                or threading.get_ident() == self._loop_thread_id)

    def _open_path_gate(self, peer_hex: str) -> threading.Event:
        """Hold readers of this peer's path. Call under the sessions lock."""
        gate = threading.Event()
        previous = self._path_gates.get(peer_hex)
        self._path_gates[peer_hex] = gate
        if previous is not None:
            previous.set()
        return gate

    def _close_path_gate(self, peer_hex: str, gate: threading.Event) -> None:
        """Let readers of this peer's path see the change behind this gate."""
        with self._sessions_lock:
            if self._path_gates.get(peer_hex) is gate:
                del self._path_gates[peer_hex]
        gate.set()

    def _announce_path(self, gate: threading.Event, peer_hex: str, path: str,
                       appeared: bool) -> None:
        """Tell the managers where a peer is, then let the change be read.

        The gate opens again on the path itself, and a peer appearing follows
        on the pool: that one is the work the news leads to (a flush, a sync
        request) and has no business holding a send to somebody else.
        """
        self._announcing.active = True
        try:
            self._call(self._fire_path_changed, peer_hex, path)
        finally:
            self._announcing.active = False
            self._close_path_gate(peer_hex, gate)
        if appeared:
            self._dispatch(self._fire_peer_appeared, peer_hex)

    def _announce(self, gate: threading.Event, peer_hex: str, path: str, *,
                  appeared: bool) -> None:
        """Queue one path change, opening the gate again if it cannot be sent."""
        if not self._submit(self._announce_path, gate, peer_hex, path,
                            appeared, executor=self._announcer):
            self._close_path_gate(peer_hex, gate)

    def open_session(self, peer_hex: str, host: str, port: int,
                     peer_cert_der: bytes,
                     timeout: float = OPEN_TIMEOUT_SECS) -> bool:
        """Dial one peer whose address and certificate are already known.

        The dial goes out of the socket this node listens on, which is the
        socket its candidates named and the only one a punched mapping
        forwards. Blocks the calling thread until the session is up or the
        attempt fails, so callers use a thread of their own rather than an
        RNS one.
        """
        if self._stopped or self._endpoint is None:
            return False
        if peer_hex == self._identity.hash_hex:
            return False
        if self.can_reach(peer_hex):
            return True
        try:
            return bool(self._on_loop(
                self._dial(peer_hex, host, port, peer_cert_der), timeout))
        except Exception as e:
            RNS.log(f"TrenchChat [ip]: could not open a session with "
                    f"{peer_hex[:12]}…: {e}", RNS.LOG_WARNING)
            return False

    async def _dial(self, peer_hex: str, host: str, port: int,
                    peer_cert_der: bytes) -> bool:
        if self.session_count() >= MAX_SESSIONS:
            RNS.log(f"TrenchChat [ip]: refusing to dial {peer_hex[:12]}…: at "
                    f"the {MAX_SESSIONS} session cap", RNS.LOG_WARNING)
            return False
        if self._endpoint is None:
            return False
        configuration = session_mod.dialer_configuration(self._certificate,
                                                         peer_cert_der)
        opened = await self._endpoint.dial(
            host, port, configuration,
            lambda connection: DirectSession(
                connection, identity=self._identity,
                certificate=self._certificate, hooks=self._hooks(),
                is_client=True, expected_peer_hex=peer_hex,
                peer_cert_der=peer_cert_der),
        )
        return opened.authenticated

    def await_session(self, peer_hex: str,
                      timeout: float = OPEN_TIMEOUT_SECS) -> bool:
        """Wait for one peer to dial this node. The other half of open_session.

        The side that does not dial has nothing to do but listen, because the
        peer's first packet is addressed to the socket it was told about and
        the listener is already on it. Blocks the calling thread, so callers
        use a thread of their own rather than an RNS one.
        """
        deadline = time.time() + timeout
        while True:
            if self.can_reach(peer_hex):
                return True
            if self._stopped or time.time() >= deadline:
                return False
            time.sleep(0.05)

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

    def open_probe_channel(self, nonce: bytes) -> punch.ProbeChannel | None:
        """A channel for one attempt's probes, on the listening socket.

        Every datagram carrying this nonce is handed to it before QUIC sees
        it, and what it sends goes out of the socket this node's candidates
        named. None when this node is not listening, which is when it has no
        such socket and therefore nothing to punch with.
        """
        if self._endpoint is None or self._stopped:
            return None
        channel = punch.ProbeChannel(nonce, self._send_probe)
        with self._sessions_lock:
            self._probe_channels[nonce] = channel
        return channel

    def close_probe_channel(self, nonce: bytes) -> None:
        """Stop answering probes for one attempt."""
        with self._sessions_lock:
            channel = self._probe_channels.pop(nonce, None)
        if channel is not None:
            channel.close()

    def _route_probe(self, data: bytes, addr) -> bool:
        """Hand one datagram to the attempt whose nonce it carries.

        Called on the transport's loop for every datagram before QUIC sees it.
        No QUIC packet is short enough to be read as a probe, and a nonce that
        names no live attempt is nothing.
        """
        nonce = punch.nonce_of(data)
        if nonce is None:
            return False
        with self._sessions_lock:
            channel = self._probe_channels.get(nonce)
        return channel is not None and channel.deliver(data, addr)

    def _send_probe(self, data: bytes, addr) -> bool:
        """Put one probe datagram on the loop, from whatever thread asks.

        A punch runs on a worker thread and an asyncio transport is not its to
        write to, so every probe goes through the loop the endpoint lives on.
        True means the datagram was handed over, which is all a datagram ever
        promises.
        """
        endpoint = self._endpoint
        if endpoint is None or self._stopped:
            return False
        try:
            self._loop.call_soon_threadsafe(endpoint.send_to, data, addr)
        except RuntimeError:
            return False
        return True

    def session_count(self) -> int:
        """How many sessions are up."""
        with self._sessions_lock:
            return len(self._sessions)

    def pending_handshake_count(self) -> int:
        """How many connections are mid-handshake and not yet proven."""
        return len(self._pending_handshakes)

    def _accept(self, connection) -> DirectSession:
        """Build the session for one inbound connection, within the caps.

        Every inbound session arrives here, punched or not: the gate an
        inbound HELLO is held to is the eligibility check, and the identity it
        proves is the only thing that decides whether the session lives.
        """
        refuse = ""
        if len(self._pending_handshakes) >= MAX_PENDING_HANDSHAKES:
            refuse = (f"at the {MAX_PENDING_HANDSHAKES} pending handshake cap")
        elif self.session_count() >= MAX_SESSIONS:
            refuse = f"at the {MAX_SESSIONS} session cap"
        inbound = DirectSession(
            connection, identity=self._identity, certificate=self._certificate,
            hooks=self._hooks(), is_client=False, refuse=refuse,
        )
        if not refuse:
            self._pending_handshakes.add(inbound)
        return inbound

    def _hooks(self) -> SessionHooks:
        return SessionHooks(
            authorize=self._authorize,
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
            gate = self._open_path_gate(peer_hex)
            previous = self._sessions.get(peer_hex)
            self._sessions[peer_hex] = peer_session
            self._queues[id(peer_session)] = _SerialQueue(self._submit)
        if previous is not None and previous is not peer_session:
            self._close_path_gate(peer_hex, gate)
            previous.shut_down("replaced by a newer session")
            return
        self._announce(gate, peer_hex, PATH_DIRECT, appeared=True)

    def _on_closed(self, peer_session: DirectSession, reason: str) -> None:
        """A session ended: the peer is back on whatever path is left."""
        self._pending_handshakes.discard(peer_session)
        self._fail_requests(peer_session)
        if self._endpoint is not None:
            self._endpoint.forget(peer_session)
        peer_hex = peer_session.peer_hex
        gate = None
        with self._sessions_lock:
            self._queues.pop(id(peer_session), None)
            if peer_hex and self._sessions.get(peer_hex) is peer_session:
                gate = self._open_path_gate(peer_hex)
                del self._sessions[peer_hex]
            else:
                peer_hex = ""
        if peer_hex and gate is not None:
            RNS.log(f"TrenchChat [ip]: session with {peer_hex[:12]}… ended: "
                    f"{reason}", RNS.LOG_NOTICE)
            self._announce(gate, peer_hex, PATH_RETICULUM, appeared=False)

    def _dispatch(self, fn, *args) -> None:
        """Hand one callback to the worker pool, off the connection's loop."""
        if fn is not None:
            self._submit(self._call, fn, *args)

    def _submit(self, fn, *args, executor=None) -> bool:
        """Run one piece of work on a pool, counted so stop() can wait for it."""
        with self._inflight_lock:
            self._inflight += 1
        try:
            (executor or self._pool).submit(self._run, fn, *args)
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
            self._loop.call_soon_threadsafe(self._open_request, peer_session,
                                            request_id, op, payload)
        except RuntimeError:
            with self._request_lock:
                self._requests.pop((id(peer_session), request_id), None)
            return None
        return request_id

    def _open_request(self, peer_session: DirectSession, request_id: int,
                      op: str, payload: dict) -> None:
        """Write one request, answering it here if it could not go.

        A request nobody wrote would otherwise wait out the asking plane's own
        timeout for an answer that was never coming.
        """
        if not peer_session.open_request(request_id, op, payload):
            self._on_response(peer_session, request_id, False, {})

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
        worker. Anything that is not a frame is the plane's to hand back to
        dispatch, because the loop carries every session on this node.
        """
        self._datagram_callback = callback

    def dispatch(self, fn, *args) -> None:
        """Run one piece of a plane's work off the loop, on the worker pool.

        The loop carries every session, so anything that touches storage or
        waits on a lock belongs here rather than on it.
        """
        self._dispatch(fn, *args)

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
