"""
In-process RRC transport and hub for tests.

Mirrors the semantics of RNSRRCTransport without any RNS Links. A shared
FakeHubRegistry connects the transports in one test to the fake hubs in it,
and delivery happens on a short-lived thread after a small delay, matching
real link timing closely enough for the eventual-consistency helpers.

FakeHub is deliberately a *correct* hub and nothing more: it welcomes,
joins, forwards and discards, exactly as the specification describes, so a
test that wants a hostile one overrides a hook rather than finding
adversarial behaviour baked in. Session state, the HELLO/WELCOME order and
the room model are the real ones; only the transport underneath is fake.
"""

import os
import threading
import time

from trenchchat.core.rrc_wire import (
    B_CAPS, B_LIMITS, B_NAME, B_VERSION, CAP_ACTION, CAP_DIRECT_NOTICE,
    CAP_RESOURCE_ENVELOPE, DEFAULT_LIMITS, K_BODY, K_DST, K_ID, K_NICK,
    K_ROOM, K_T, T_ACTION, T_ERROR, T_HELLO, T_JOIN, T_JOINED, T_MSG,
    T_NOTICE, T_PART, T_PARTED, T_PING, T_PONG, T_WELCOME,
    capabilities_of, limits_of, pack_envelope, unpack_envelope,
)
from trenchchat.network.rrc_transport import (
    REASON_CLOSED, REASON_LOCAL, REASON_NO_PATH, SESSION_ACTIVE,
    SESSION_DIALING, SESSION_HANDSHAKING, SESSION_IDLE, SESSION_UNREACHABLE,
    RRCTransportBase,
)

FAKE_DELIVERY_DELAY = 0.02


def _new_session_id() -> str:
    return os.urandom(8).hex()


class FakeHubRegistry:
    """Shared lookup table connecting the fake transports in one test."""

    def __init__(self):
        self.hubs: dict[str, "FakeHub"] = {}
        self.lock = threading.RLock()

    def add(self, hub: "FakeHub") -> str:
        with self.lock:
            self.hubs[hub.hub_hex] = hub
        return hub.hub_hex


class FakeHub:
    """A minimal, correct RRC hub: welcome, join, forward, discard."""

    def __init__(self, hub_hex: str, *, name: str = "fakehub",
                 version: str = "0.1", limits: dict | None = None,
                 capabilities: dict | None = None):
        self.hub_hex = hub_hex
        self.name = name
        self.version = version
        self.limits = dict(limits or DEFAULT_LIMITS)
        self.capabilities = dict(capabilities if capabilities is not None else {
            CAP_RESOURCE_ENVELOPE: True,
            CAP_ACTION: True,
            CAP_DIRECT_NOTICE: True,
        })
        self.lock = threading.RLock()
        self.sessions: dict[str, "FakeSession"] = {}
        self.rooms: dict[str, set[str]] = {}
        self.received: list[dict] = []
        # Set to refuse the next JOIN with an ERROR carrying this text.
        self.refuse_join_with: str | None = None
        # Set to answer HELLO with nothing at all.
        self.withhold_welcome = False

    # --- session lifecycle ---

    def open_session(self, session: "FakeSession") -> None:
        with self.lock:
            self.sessions[session.client_hex] = session

    def close_session(self, client_hex: str) -> None:
        with self.lock:
            self.sessions.pop(client_hex, None)
            for members in self.rooms.values():
                members.discard(client_hex)
            self.rooms = {r: m for r, m in self.rooms.items() if m}

    # --- inbound ---

    def handle(self, session: "FakeSession", payload: bytes) -> None:
        envelope = unpack_envelope(payload)
        if envelope is None:
            return
        with self.lock:
            self.received.append(envelope)
        msg_type = envelope.get(K_T)

        if msg_type == T_HELLO:
            if not session.welcomed and not self.withhold_welcome:
                session.welcomed = True
                session.deliver(self._welcome())
            return
        if not session.welcomed:
            # Before WELCOME the hub processes nothing, which is the rule
            # the client is required to make unnecessary.
            return
        if msg_type == T_JOIN:
            self._join(session, envelope)
        elif msg_type == T_PART:
            self._part(session, envelope)
        elif msg_type in (T_MSG, T_NOTICE, T_ACTION):
            self._forward(session, envelope, msg_type)
        elif msg_type == T_PONG:
            session.pongs += 1

    def ping(self, client_hex: str) -> None:
        with self.lock:
            session = self.sessions.get(client_hex)
        if session is not None:
            session.deliver(pack_envelope(T_PING, src=bytes.fromhex(self.hub_hex)))

    # --- handlers ---

    def _welcome(self) -> bytes:
        return pack_envelope(T_WELCOME, src=bytes.fromhex(self.hub_hex), body={
            B_NAME: self.name,
            B_VERSION: self.version,
            B_CAPS: dict(self.capabilities),
            B_LIMITS: dict(self.limits),
        })

    def _join(self, session: "FakeSession", envelope: dict) -> None:
        room = envelope.get(K_ROOM)
        if room is None:
            return
        if self.refuse_join_with is not None:
            session.deliver(pack_envelope(
                T_ERROR, src=bytes.fromhex(self.hub_hex), room=room,
                body=self.refuse_join_with))
            return
        with self.lock:
            members = self.rooms.setdefault(room, set())
            members.add(session.client_hex)
            roster = [bytes.fromhex(h) for h in sorted(members)]
        session.deliver(pack_envelope(T_JOINED, src=bytes.fromhex(self.hub_hex),
                                      room=room, body=roster))

    def _part(self, session: "FakeSession", envelope: dict) -> None:
        room = envelope.get(K_ROOM)
        if room is None:
            return
        with self.lock:
            members = self.rooms.get(room, set())
            members.discard(session.client_hex)
            if not members:
                self.rooms.pop(room, None)
        session.deliver(pack_envelope(T_PARTED, src=bytes.fromhex(self.hub_hex),
                                      room=room))

    def _forward(self, session: "FakeSession", envelope: dict,
                 msg_type: int) -> None:
        text = envelope.get(K_BODY)
        nick = envelope.get(K_NICK)
        dst = envelope.get(K_DST)
        if dst is not None:
            with self.lock:
                target = self.sessions.get(dst.hex())
            if target is None:
                session.deliver(pack_envelope(
                    T_ERROR, src=bytes.fromhex(self.hub_hex),
                    body="no such client"))
                return
            # K_SRC is overwritten with the authenticated sender, never the
            # value the client put there.
            target.deliver(pack_envelope(
                msg_type, src=bytes.fromhex(session.client_hex),
                dst=dst, body=text, nick=nick, msg_id=envelope.get(K_ID)))
            return

        room = envelope.get(K_ROOM)
        with self.lock:
            members = set(self.rooms.get(room, set()))
            if session.client_hex not in members:
                members = set()
            targets = [self.sessions.get(h) for h in members
                       if h != session.client_hex]
        if room is None:
            return
        for target in targets:
            if target is None:
                continue
            target.deliver(pack_envelope(
                msg_type, src=bytes.fromhex(session.client_hex), room=room,
                body=text, nick=nick, msg_id=envelope.get(K_ID)))


class FakeSession:
    """One client's connection to a FakeHub."""

    def __init__(self, transport: "FakeRRCTransport", hub: FakeHub,
                 client_hex: str):
        self.transport = transport
        self.hub = hub
        self.client_hex = client_hex
        self.welcomed = False
        self.pongs = 0

    def deliver(self, payload: bytes) -> None:
        self.transport._deliver_later(self.hub.hub_hex, payload)


class FakeRRCTransport(RRCTransportBase):
    """In-process stand-in for RNSRRCTransport."""

    def __init__(self, self_hex: str, registry: FakeHubRegistry, *,
                 delivery_delay: float = FAKE_DELIVERY_DELAY,
                 unreachable: set[str] | None = None):
        super().__init__()
        self._self_hex = self_hex
        self._registry = registry
        self._delay = delivery_delay
        self._unreachable = set(unreachable or ())
        self._lock = threading.RLock()
        self._threads: list[threading.Thread] = []
        self._hub_hex: str | None = None
        self._state = SESSION_IDLE
        self._session: FakeSession | None = None
        self._info: dict = {}
        self.sent: list[bytes] = []

    # --- commands ---

    def connect(self, hub_hash_hex: str) -> None:
        if hub_hash_hex in self._unreachable:
            with self._lock:
                self._hub_hex = hub_hash_hex
                self._state = SESSION_UNREACHABLE
            self._notify_session(hub_hash_hex, SESSION_UNREACHABLE, REASON_NO_PATH)
            return
        with self._registry.lock:
            hub = self._registry.hubs.get(hub_hash_hex)
        if hub is None:
            with self._lock:
                self._hub_hex = hub_hash_hex
                self._state = SESSION_UNREACHABLE
            self._notify_session(hub_hash_hex, SESSION_UNREACHABLE, REASON_NO_PATH)
            return

        session = FakeSession(self, hub, self._self_hex)
        with self._lock:
            self._hub_hex = hub_hash_hex
            self._session = session
            self._state = SESSION_DIALING
        self._notify_session(hub_hash_hex, SESSION_DIALING)
        hub.open_session(session)

        with self._lock:
            self._state = SESSION_HANDSHAKING
        self._notify_session(hub_hash_hex, SESSION_HANDSHAKING)
        hello = pack_envelope(T_HELLO, src=bytes.fromhex(self._self_hex), body={
            B_NAME: "TrenchChat", B_VERSION: "test",
            B_CAPS: {CAP_ACTION: True, CAP_DIRECT_NOTICE: True},
        })
        self.sent.append(hello)
        hub.handle(session, hello)

    def disconnect(self, hub_hash_hex: str, reason: str = REASON_LOCAL) -> None:
        with self._lock:
            session = self._session
            self._session = None
            self._hub_hex = None
            self._state = SESSION_IDLE
            self._info = {}
        if session is not None:
            session.hub.close_session(self._self_hex)
        self._notify_session(hub_hash_hex, SESSION_IDLE, reason)

    def drop_link(self) -> None:
        """Simulate the hub or the link going away underneath the client."""
        with self._lock:
            hub_hex = self._hub_hex
            session = self._session
            self._session = None
            self._state = SESSION_IDLE
            self._info = {}
        if session is not None:
            session.hub.close_session(self._self_hex)
        if hub_hex is not None:
            self._notify_session(hub_hex, SESSION_IDLE, REASON_CLOSED)

    def send(self, hub_hash_hex: str, payload: bytes) -> bool:
        with self._lock:
            if self._state != SESSION_ACTIVE or self._hub_hex != hub_hash_hex:
                return False
            session = self._session
        if session is None:
            return False
        self.sent.append(payload)
        session.hub.handle(session, payload)
        return True

    def session_state(self, hub_hash_hex: str) -> str:
        with self._lock:
            if self._hub_hex != hub_hash_hex:
                return SESSION_IDLE
            return self._state

    def hub_info(self, hub_hash_hex: str) -> dict:
        with self._lock:
            return dict(self._info) if self._hub_hex == hub_hash_hex else {}

    def tick(self) -> None:
        pass

    # --- inbound ---

    def _deliver_later(self, hub_hex: str, payload: bytes) -> None:
        def run():
            time.sleep(self._delay)
            self._receive(hub_hex, payload)

        thread = threading.Thread(target=run, daemon=True)
        with self._lock:
            self._threads.append(thread)
        thread.start()

    def join_threads(self, timeout: float = 2.0) -> None:
        """Wait for in-flight deliveries, so a test leaves none running.

        Without this a test's leftover threads keep waking during later
        tests, which is enough to make a timing-sensitive one nearby fail.
        """
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            thread.join(timeout=timeout)
        with self._lock:
            self._threads = [t for t in self._threads if t.is_alive()]

    def _receive(self, hub_hex: str, payload: bytes) -> None:
        with self._lock:
            if self._hub_hex != hub_hex or self._session is None:
                return
            state = self._state
        envelope = unpack_envelope(payload)
        if envelope is None:
            return
        msg_type = envelope.get(K_T)

        if msg_type == T_WELCOME:
            with self._lock:
                if self._state != SESSION_HANDSHAKING:
                    return
                self._state = SESSION_ACTIVE
                body = envelope.get(K_BODY) if isinstance(
                    envelope.get(K_BODY), dict) else {}
                # Read off the WELCOME rather than out of the hub object, so
                # a real RRCHubManager and a FakeHub are indistinguishable
                # from here, which is what the real transport does too.
                self._info = {
                    "name": body.get(B_NAME, ""),
                    "version": body.get(B_VERSION, ""),
                    "capabilities": capabilities_of(envelope),
                    "limits": limits_of(envelope),
                }
            self._notify_session(hub_hex, SESSION_ACTIVE)
            self._notify_envelope(hub_hex, envelope)
            return

        if state != SESSION_ACTIVE:
            return
        if msg_type == T_PING:
            self.send(hub_hex, pack_envelope(
                T_PONG, src=bytes.fromhex(self._self_hex),
                msg_id=envelope.get(K_ID)))
            return
        self._notify_envelope(hub_hex, envelope)


class FakeHostTransport(RRCTransportBase):
    """The hosting half of the transport, backed by the registry.

    Lets a real RRCHubManager be the hub a FakeRRCTransport connects to, so
    a test exercises the actual hub code rather than FakeHub's stand-in. It
    exposes the same three methods FakeHub does, which is what the client
    transport calls.
    """

    def __init__(self, hub_hex: str, registry: FakeHubRegistry):
        super().__init__()
        self.hub_hex = hub_hex
        self._registry = registry
        self._lock = threading.RLock()
        self._hosting = False
        self._name = ""
        self._sessions: dict[str, "FakeSession"] = {}
        self._by_client: dict[str, str] = {}
        self.dropped: list[tuple[str, str]] = []
        self.announces = 0
        # Every envelope the hub emitted, so a test can assert on the hub
        # directly instead of needing a client to receive it.
        self.sent: list[tuple[str, dict]] = []

    # --- hosting API ---

    def start_hosting(self, hub_name: str) -> str:
        with self._lock:
            self._hosting = True
            self._name = hub_name
        with self._registry.lock:
            self._registry.hubs[self.hub_hex] = self
        self.announce()
        return self.hub_hex

    def stop_hosting(self) -> None:
        with self._lock:
            self._hosting = False
            self._sessions.clear()
            self._by_client.clear()
        with self._registry.lock:
            self._registry.hubs.pop(self.hub_hex, None)

    def announce(self) -> None:
        self.announces += 1

    def hosted_hash(self) -> str | None:
        with self._lock:
            return self.hub_hex if self._hosting else None

    def send_to_client(self, session_id: str, payload: bytes) -> bool:
        envelope = unpack_envelope(payload)
        if envelope is not None:
            self.sent.append((session_id, envelope))
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            return False
        session.deliver(payload)
        return True

    def drop_client(self, session_id: str, reason: str = "") -> None:
        with self._lock:
            session = self._sessions.pop(session_id, None)
            if session is not None:
                self._by_client.pop(session.client_hex, None)
        self.dropped.append((session_id, reason))

    # --- what the client transport calls, matching FakeHub ---

    def open_session(self, session: "FakeSession") -> None:
        session_id = _new_session_id()
        with self._lock:
            self._sessions[session_id] = session
            self._by_client[session.client_hex] = session_id

    def handle(self, session: "FakeSession", payload: bytes) -> None:
        with self._lock:
            session_id = self._by_client.get(session.client_hex)
        if session_id is None:
            return
        envelope = unpack_envelope(payload)
        if envelope is None:
            return
        self._notify_client_envelope(session_id, session.client_hex, envelope)

    def close_session(self, client_hex: str) -> None:
        with self._lock:
            session_id = self._by_client.pop(client_hex, None)
            if session_id is not None:
                self._sessions.pop(session_id, None)
        if session_id is not None:
            self._notify_client_gone(session_id)

    # --- unused client half ---

    def connect(self, hub_hash_hex: str) -> None:
        raise NotImplementedError("this transport only hosts")

    def disconnect(self, hub_hash_hex: str, reason: str = REASON_LOCAL) -> None:
        raise NotImplementedError("this transport only hosts")

    def send(self, hub_hash_hex: str, payload: bytes) -> bool:
        return False

    def session_state(self, hub_hash_hex: str) -> str:
        return SESSION_IDLE

    def hub_info(self, hub_hash_hex: str) -> dict:
        return {}

    def tick(self) -> None:
        pass


def unwelcomed_session(host: FakeHostTransport, transport: "FakeRRCTransport",
                       client_hex: str) -> "FakeSession":
    """A session open on the hub that has not sent HELLO.

    No honest client reaches this state, which is exactly why a test needs
    to build one: it is the only way to exercise the hub's WELCOME gate.
    """
    session = FakeSession(transport, host, client_hex)
    host.open_session(session)
    return session
