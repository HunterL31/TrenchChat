"""
RRC hub plane: RNS Link lifecycle and the RRC session handshake.

Speaks Reticulum Relay Chat for full interop with rrcd and its other
clients. A hub is an RNS destination on the "rrc.hub" aspect, and a session
is one Link to it carrying CBOR envelopes as link packets in both
directions. Dialling goes straight to the hub destination from its announce
hash, the way the nomad node plane dials a node, rather than through
lxmf-delivery indirection.

This module owns the protocol's session layer: identify, HELLO, the wait for
WELCOME, PING answering, and the rule that nothing but HELLO may be sent
before WELCOME arrives. Rooms, transcripts and the hub registry are not
protocol, and stay in core/rrc.py.

A session is always identified. RRC has no accounts and the Link is the only
authentication, so an anonymous session cannot say anything; connecting is
therefore an explicit user action, unlike the nomad plane which browses
anonymously by default.

This module never touches Storage or core managers, matching
voice_transport.py's layering: callbacks up, tick() down.
"""

import os
import threading
import time

import RNS

from trenchchat.core.rrc_wire import (
    B_CAPS, B_NAME, B_VERSION, CAP_ACTION, DEFAULT_LIMITS,
    HUB_APP_NAME, HUB_ASPECT,
    K_BODY, K_ID, K_T, MAX_HUB_NAME_BYTES,
    T_HELLO, T_PING, T_PONG, T_WELCOME,
    capabilities_of, limits_of, pack_envelope, unpack_envelope,
)
from trenchchat.version import app_version

# Public session states, reported through the session callback.
SESSION_IDLE = "idle"
SESSION_DIALING = "dialing"
SESSION_HANDSHAKING = "handshaking"
SESSION_ACTIVE = "active"
SESSION_UNREACHABLE = "unreachable"

# Why a session ended, carried alongside the state.
REASON_CLOSED = "closed"
REASON_NO_PATH = "no_path"
REASON_IDENTITY_MISMATCH = "identity_mismatch"
REASON_WELCOME_TIMEOUT = "welcome_timeout"
REASON_LOCAL = "local"

RRC_DIAL_BACKOFF = (2.0, 5.0, 10.0, 30.0)
RRC_WELCOME_TIMEOUT_SECS = 20.0
# A hub that has not answered by here is not coming back on this link.
RRC_DIAL_TIMEOUT_SECS = 30.0
# Ceiling on inbound packets from one hub per second. A hub fans a busy room
# out to every member, so this is generous next to the voice plane's, but a
# hub is still a peer and an unbounded inbound rate is its memory allocator.
RRC_PACKET_RATE_LIMIT = 60
RRC_PACKET_RATE_WINDOW = 1.0

# Hosting. A hub is a service other people have to find, so it re-announces,
# but on a long interval: an announce is a broadcast on shared spectrum, and
# a hub that shouted every minute would cost every listener for nothing.
RRC_ANNOUNCE_INTERVAL_SECS = 900.0
# Links accepted but not yet welcomed. Nothing here has said who it is, so
# this cap is the only thing bounding them.
MAX_PENDING_CLIENT_LINKS = 32
# Clients one hub will carry at once.
MAX_CLIENT_SESSIONS = 64

CLIENT_NAME = "TrenchChat"

# What this client can do, advertised in HELLO. Only what is implemented is
# named: a hub takes an advertised capability as permission to use it, so
# claiming one this client would drop on the floor is worse than claiming
# none. CAP_RESOURCE_ENVELOPE and CAP_DIRECT_NOTICE join this when the
# client can act on them.
CLIENT_CAPABILITIES = {
    CAP_ACTION: True,
}


class RRCTransportBase:
    """The seam core/rrc.py talks to, so tests can drive a fake."""

    def __init__(self):
        self._envelope_cb = None
        self._session_cb = None
        self._client_envelope_cb = None
        self._client_gone_cb = None

    def set_envelope_callback(self, cb) -> None:
        """cb(hub_hex, envelope): one validated inbound envelope."""
        self._envelope_cb = cb

    def set_session_callback(self, cb) -> None:
        """cb(hub_hex, state, reason): a session changed state."""
        self._session_cb = cb

    def connect(self, hub_hash_hex: str) -> None:
        raise NotImplementedError

    def disconnect(self, hub_hash_hex: str, reason: str = REASON_LOCAL) -> None:
        raise NotImplementedError

    def send(self, hub_hash_hex: str, payload: bytes) -> bool:
        """Put one packed envelope on the session. False if it could not go."""
        raise NotImplementedError

    def session_state(self, hub_hash_hex: str) -> str:
        raise NotImplementedError

    def hub_info(self, hub_hash_hex: str) -> dict:
        """Name, version, capabilities and limits, as the hub's WELCOME gave them."""
        raise NotImplementedError

    def tick(self) -> None:
        raise NotImplementedError

    # --- hosting ---

    def set_client_envelope_callback(self, cb) -> None:
        """cb(session_id, identity_hex, envelope): one envelope from a client."""
        self._client_envelope_cb = cb

    def set_client_gone_callback(self, cb) -> None:
        """cb(session_id): a client's link closed."""
        self._client_gone_cb = cb

    def start_hosting(self, hub_name: str) -> str | None:
        raise NotImplementedError

    def stop_hosting(self) -> None:
        raise NotImplementedError

    def announce(self) -> None:
        raise NotImplementedError

    def hosted_hash(self) -> str | None:
        raise NotImplementedError

    def send_to_client(self, session_id: str, payload: bytes) -> bool:
        raise NotImplementedError

    def drop_client(self, session_id: str, reason: str = "") -> None:
        raise NotImplementedError

    def _notify_client_envelope(self, session_id: str, identity_hex: str,
                                envelope: dict) -> None:
        if self._client_envelope_cb is None:
            return
        try:
            self._client_envelope_cb(session_id, identity_hex, envelope)
        except Exception as e:
            RNS.log(f"TrenchChat [rrc]: client envelope callback error: {e}",
                    RNS.LOG_ERROR)

    def _notify_client_gone(self, session_id: str) -> None:
        if self._client_gone_cb is None:
            return
        try:
            self._client_gone_cb(session_id)
        except Exception as e:
            RNS.log(f"TrenchChat [rrc]: client gone callback error: {e}",
                    RNS.LOG_ERROR)

    def _notify_envelope(self, hub_hex: str, envelope: dict) -> None:
        if self._envelope_cb is None:
            return
        try:
            self._envelope_cb(hub_hex, envelope)
        except Exception as e:
            RNS.log(f"TrenchChat [rrc]: envelope callback error: {e}", RNS.LOG_ERROR)

    def _notify_session(self, hub_hex: str, state: str, reason: str = "") -> None:
        if self._session_cb is None:
            return
        try:
            self._session_cb(hub_hex, state, reason)
        except Exception as e:
            RNS.log(f"TrenchChat [rrc]: session callback error: {e}", RNS.LOG_ERROR)


class _Session:
    """One hub's connection across dial, handshake and teardown."""

    def __init__(self, hub_hex: str):
        self.hub_hex = hub_hex
        self.state = SESSION_IDLE
        self.link = None
        self.dial_attempts = 0
        self.next_dial_at = 0.0
        self.dialed_at = 0.0
        self.hello_sent_at = 0.0
        self.wanted = False
        self.hub_name = ""
        self.hub_version = ""
        self.capabilities: dict = {}
        self.limits: dict = dict(DEFAULT_LIMITS)
        self.packet_times: list[float] = []

    def backoff_delay(self) -> float:
        index = min(self.dial_attempts, len(RRC_DIAL_BACKOFF) - 1)
        return RRC_DIAL_BACKOFF[index]


class _ClientLink:
    """One inbound client link on a hosted hub."""

    def __init__(self, session_id: str, link):
        self.session_id = session_id
        self.link = link
        self.opened_at = time.time()
        self.identified = False
        self.packet_times: list[float] = []


class RNSRRCTransport(RRCTransportBase):
    """Real RNS Link implementation of the RRC hub plane."""

    def __init__(self, identity):
        super().__init__()
        self._identity = identity
        self._lock = threading.RLock()
        self._sessions: dict[str, _Session] = {}
        self._link_owner: dict[int, str] = {}

        self._hub_dest = None
        # None means not hosting. The destination outlives a stop, because
        # RNS refuses to register the same one twice and a stop/start cycle
        # would otherwise fail on the second start.
        self._hub_name: str | None = None
        self._last_announce = 0.0
        self._clients: dict[str, _ClientLink] = {}
        self._client_of_link: dict[int, str] = {}

    # --- commands ---

    def connect(self, hub_hash_hex: str) -> None:
        with self._lock:
            session = self._sessions.setdefault(hub_hash_hex, _Session(hub_hash_hex))
            if session.wanted and session.state != SESSION_UNREACHABLE:
                return
            session.wanted = True
            session.dial_attempts = 0
            session.next_dial_at = 0.0
        self._dial(hub_hash_hex)

    def disconnect(self, hub_hash_hex: str, reason: str = REASON_LOCAL) -> None:
        with self._lock:
            session = self._sessions.get(hub_hash_hex)
            if session is None:
                return
            session.wanted = False
            link = session.link
            self._reset(session)
        if link is not None:
            try:
                link.teardown()
            except Exception as e:
                RNS.log(f"TrenchChat [rrc]: link teardown failed: {e}", RNS.LOG_WARNING)
        self._notify_session(hub_hash_hex, SESSION_IDLE, reason)

    def send(self, hub_hash_hex: str, payload: bytes) -> bool:
        with self._lock:
            session = self._sessions.get(hub_hash_hex)
            if session is None or session.state != SESSION_ACTIVE:
                return False
            link = session.link
        return self._send_on(link, payload, hub_hash_hex)

    def session_state(self, hub_hash_hex: str) -> str:
        with self._lock:
            session = self._sessions.get(hub_hash_hex)
            return SESSION_IDLE if session is None else session.state

    def hub_info(self, hub_hash_hex: str) -> dict:
        with self._lock:
            session = self._sessions.get(hub_hash_hex)
            if session is None:
                return {}
            return {
                "name": session.hub_name,
                "version": session.hub_version,
                "capabilities": dict(session.capabilities),
                "limits": dict(session.limits),
            }

    def tick(self) -> None:
        now = time.time()
        if self._hub_name is not None and \
                now - self._last_announce >= RRC_ANNOUNCE_INTERVAL_SECS:
            self.announce()
        redial: list[str] = []
        expired: list[tuple[str, str]] = []
        with self._lock:
            for session in self._sessions.values():
                if session.state == SESSION_HANDSHAKING and \
                        now - session.hello_sent_at >= RRC_WELCOME_TIMEOUT_SECS:
                    expired.append((session.hub_hex, REASON_WELCOME_TIMEOUT))
                elif session.state == SESSION_DIALING and \
                        now - session.dialed_at >= RRC_DIAL_TIMEOUT_SECS:
                    expired.append((session.hub_hex, REASON_NO_PATH))
                elif session.wanted and session.state in (SESSION_IDLE,
                                                          SESSION_UNREACHABLE) \
                        and now >= session.next_dial_at:
                    redial.append(session.hub_hex)
        for hub_hex, reason in expired:
            self._fail(hub_hex, reason)
        for hub_hex in redial:
            self._dial(hub_hex)

    # --- dialling ---

    def _dial(self, hub_hex: str) -> None:
        try:
            dest_hash = bytes.fromhex(hub_hex)
        except ValueError:
            self._fail(hub_hex, REASON_IDENTITY_MISMATCH)
            return

        hub_identity = RNS.Identity.recall(dest_hash)
        if hub_identity is None:
            RNS.Transport.request_path(dest_hash)
            self._defer(hub_hex)
            return

        dest = RNS.Destination(
            hub_identity,
            RNS.Destination.OUT,
            RNS.Destination.SINGLE,
            HUB_APP_NAME,
            HUB_ASPECT,
        )
        if dest.hash != dest_hash:
            # A recalled identity that does not hash back to the dialled hub
            # would put this session's traffic somewhere else entirely.
            self._fail(hub_hex, REASON_IDENTITY_MISMATCH)
            return
        if not RNS.Transport.has_path(dest.hash):
            RNS.Transport.request_path(dest.hash)
            self._defer(hub_hex)
            return

        try:
            link = RNS.Link(
                dest,
                established_callback=self._on_established,
                closed_callback=self._on_closed,
            )
            link.set_packet_callback(self._on_packet)
        except Exception as e:
            RNS.log(f"TrenchChat [rrc]: dial to {hub_hex[:12]}… failed: {e}",
                    RNS.LOG_WARNING)
            self._defer(hub_hex)
            return

        with self._lock:
            session = self._sessions.setdefault(hub_hex, _Session(hub_hex))
            session.link = link
            session.state = SESSION_DIALING
            session.dialed_at = time.time()
            self._link_owner[id(link)] = hub_hex
        self._notify_session(hub_hex, SESSION_DIALING)

    def _defer(self, hub_hex: str) -> None:
        with self._lock:
            session = self._sessions.setdefault(hub_hex, _Session(hub_hex))
            session.dial_attempts += 1
            session.next_dial_at = time.time() + session.backoff_delay()
            session.state = SESSION_UNREACHABLE
        self._notify_session(hub_hex, SESSION_UNREACHABLE, REASON_NO_PATH)

    def _fail(self, hub_hex: str, reason: str) -> None:
        with self._lock:
            session = self._sessions.get(hub_hex)
            if session is None:
                return
            link = session.link
            self._reset(session)
            session.dial_attempts += 1
            session.next_dial_at = time.time() + session.backoff_delay()
            session.state = SESSION_UNREACHABLE
        if link is not None:
            try:
                link.teardown()
            except Exception:
                pass
        self._notify_session(hub_hex, SESSION_UNREACHABLE, reason)

    def _reset(self, session: _Session) -> None:
        if session.link is not None:
            self._link_owner.pop(id(session.link), None)
        session.link = None
        session.state = SESSION_IDLE
        session.hub_name = ""
        session.hub_version = ""
        session.capabilities = {}
        session.limits = dict(DEFAULT_LIMITS)
        session.packet_times.clear()

    # --- handshake ---

    def _on_established(self, link) -> None:
        hub_hex = self._owner_of(link)
        if hub_hex is None:
            return
        try:
            link.identify(self._identity.rns_identity)
        except Exception as e:
            RNS.log(f"TrenchChat [rrc]: could not identify to "
                    f"{hub_hex[:12]}…: {e}", RNS.LOG_WARNING)
            self._fail(hub_hex, REASON_CLOSED)
            return

        hello = pack_envelope(T_HELLO, src=self._identity.hash, body={
            B_NAME: CLIENT_NAME,
            B_VERSION: app_version(),
            B_CAPS: dict(CLIENT_CAPABILITIES),
        })
        with self._lock:
            session = self._sessions.get(hub_hex)
            if session is None:
                return
            session.state = SESSION_HANDSHAKING
            session.hello_sent_at = time.time()
            session.dial_attempts = 0
        self._notify_session(hub_hex, SESSION_HANDSHAKING)
        self._send_on(link, hello, hub_hex)

    def _on_packet(self, data, packet) -> None:
        hub_hex = self._owner_of(packet.link)
        if hub_hex is None:
            return
        now = time.time()
        with self._lock:
            session = self._sessions.get(hub_hex)
            if session is None or not self._allow_packet(session, now):
                return
            state = session.state

        envelope = unpack_envelope(data)
        if envelope is None:
            RNS.log(f"TrenchChat [rrc]: dropped a malformed envelope from "
                    f"{hub_hex[:12]}…", RNS.LOG_WARNING)
            return

        msg_type = envelope.get(K_T)
        if msg_type == T_WELCOME:
            self._accept_welcome(hub_hex, envelope)
            return
        if state != SESSION_ACTIVE:
            # The hub must not talk before it has welcomed us; anything it
            # sends first is not something this session can act on.
            RNS.log(f"TrenchChat [rrc]: {hub_hex[:12]}… sent type "
                    f"{msg_type} before WELCOME", RNS.LOG_WARNING)
            return
        if msg_type == T_PING:
            self._answer_ping(hub_hex, envelope)
            return
        self._notify_envelope(hub_hex, envelope)

    def _accept_welcome(self, hub_hex: str, envelope: dict) -> None:
        body = envelope.get(K_BODY)
        with self._lock:
            session = self._sessions.get(hub_hex)
            if session is None or session.state != SESSION_HANDSHAKING:
                return
            if isinstance(body, dict):
                session.hub_name = _text(body.get(B_NAME))
                session.hub_version = _text(body.get(B_VERSION))
            session.capabilities = capabilities_of(envelope)
            session.limits = limits_of(envelope)
            session.state = SESSION_ACTIVE
        RNS.log(f"TrenchChat [rrc]: welcomed by {hub_hex[:12]}…", RNS.LOG_NOTICE)
        self._notify_session(hub_hex, SESSION_ACTIVE)
        self._notify_envelope(hub_hex, envelope)

    def _answer_ping(self, hub_hex: str, envelope: dict) -> None:
        pong = pack_envelope(T_PONG, src=self._identity.hash,
                             msg_id=envelope.get(K_ID))
        self.send(hub_hex, pong)

    # --- link plumbing ---

    def _owner_of(self, link) -> str | None:
        if link is None:
            return None
        with self._lock:
            return self._link_owner.get(id(link))

    def _allow_packet(self, session: _Session, now: float) -> bool:
        cutoff = now - RRC_PACKET_RATE_WINDOW
        session.packet_times = [t for t in session.packet_times if t > cutoff]
        if len(session.packet_times) >= RRC_PACKET_RATE_LIMIT:
            return False
        session.packet_times.append(now)
        return True

    def _send_on(self, link, payload: bytes, hub_hex: str) -> bool:
        if link is None:
            return False
        try:
            # send() returns False when it could not go: a closed link, or no
            # interface that would carry it. Ignoring that reports a line as
            # sent that never left, which is the one lie a chat client must
            # not tell.
            sent = RNS.Packet(link, payload, create_receipt=False).send()
        except Exception as e:
            RNS.log(f"TrenchChat [rrc]: send to {hub_hex[:12]}… failed: {e}",
                    RNS.LOG_WARNING)
            return False
        if sent is False:
            RNS.log(f"TrenchChat [rrc]: {len(payload)}B for {hub_hex[:12]}… "
                    f"could not be sent", RNS.LOG_WARNING)
            return False
        return True

    def _on_closed(self, link) -> None:
        hub_hex = self._owner_of(link)
        if hub_hex is None:
            return
        with self._lock:
            session = self._sessions.get(hub_hex)
            if session is None:
                return
            wanted = session.wanted
            self._reset(session)
            if wanted:
                session.dial_attempts += 1
                session.next_dial_at = time.time() + session.backoff_delay()
        # A new link is a new session: RRC keeps no continuity across one, so
        # the caller has to rejoin rather than assume its rooms survived.
        self._notify_session(hub_hex, SESSION_IDLE, REASON_CLOSED)


    # --- hosting ---

    def start_hosting(self, hub_name: str) -> str | None:
        """Serve rrc.hub from this node, and announce that it is up."""
        with self._lock:
            if self._hub_dest is None:
                self._hub_dest = RNS.Destination(
                    self._identity.rns_identity,
                    RNS.Destination.IN,
                    RNS.Destination.SINGLE,
                    HUB_APP_NAME,
                    HUB_ASPECT,
                )
                self._hub_dest.set_link_established_callback(self._on_client_link)
            self._hub_name = hub_name
            dest_hash = self._hub_dest.hash.hex()
        self.announce()
        RNS.log(f"TrenchChat [rrc]: hosting hub {dest_hash[:12]}… "
                f"({hub_name})", RNS.LOG_NOTICE)
        return dest_hash

    def stop_hosting(self) -> None:
        with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
            self._client_of_link.clear()
            self._hub_name = None
        for client in clients:
            self._teardown(client.link)

    def announce(self) -> None:
        """Announce the hosted hub, carrying its name as plain UTF-8.

        The specification fixes the aspect but not the payload, so the
        simplest encoding any client can read is the one that goes out.
        """
        with self._lock:
            dest = self._hub_dest
            name = self._hub_name
        if dest is None or name is None:
            return
        try:
            dest.announce(app_data=name.encode("utf-8")[:MAX_HUB_NAME_BYTES]
                          or None)
            self._last_announce = time.time()
        except Exception as e:
            RNS.log(f"TrenchChat [rrc]: hub announce failed: {e}", RNS.LOG_WARNING)

    def hosted_hash(self) -> str | None:
        with self._lock:
            if self._hub_dest is None or self._hub_name is None:
                return None
            return self._hub_dest.hash.hex()

    def send_to_client(self, session_id: str, payload: bytes) -> bool:
        with self._lock:
            client = self._clients.get(session_id)
            link = client.link if client is not None else None
        if link is None:
            return False
        try:
            sent = RNS.Packet(link, payload, create_receipt=False).send()
        except Exception as e:
            RNS.log(f"TrenchChat [rrc]: send to client failed: {e}",
                    RNS.LOG_WARNING)
            return False
        if sent is False:
            RNS.log(f"TrenchChat [rrc]: {len(payload)}B for client "
                    f"{session_id[:8]}… could not be sent", RNS.LOG_WARNING)
            return False
        return True

    def drop_client(self, session_id: str, reason: str = "") -> None:
        with self._lock:
            client = self._clients.pop(session_id, None)
            if client is not None:
                self._client_of_link.pop(id(client.link), None)
        if client is None:
            return
        if reason:
            RNS.log(f"TrenchChat [rrc]: dropping client {session_id[:8]}…: "
                    f"{reason}", RNS.LOG_WARNING)
        self._teardown(client.link)

    def _on_client_link(self, link) -> None:
        link.set_packet_callback(self._on_client_packet)
        link.set_link_closed_callback(self._on_client_link_closed)
        evicted = None
        session_id = os.urandom(8).hex()
        with self._lock:
            if self._hub_dest is None or self._hub_name is None:
                accepted = False
            elif len(self._clients) >= MAX_CLIENT_SESSIONS:
                accepted = False
            else:
                # Nothing here has identified yet, so the pending cap is the
                # only bound on links held before a HELLO arrives; drop the
                # oldest rather than grow.
                pending = [c for c in self._clients.values() if not c.identified]
                if len(pending) >= MAX_PENDING_CLIENT_LINKS:
                    oldest = min(pending, key=lambda c: c.opened_at)
                    evicted = self._clients.pop(oldest.session_id, None)
                    if evicted is not None:
                        self._client_of_link.pop(id(evicted.link), None)
                self._clients[session_id] = _ClientLink(session_id, link)
                self._client_of_link[id(link)] = session_id
                accepted = True
        if evicted is not None:
            self._notify_client_gone(evicted.session_id)
            self._teardown(evicted.link)
        if not accepted:
            self._teardown(link)

    def _on_client_packet(self, data, packet) -> None:
        link = packet.link
        now = time.time()
        with self._lock:
            session_id = self._client_of_link.get(id(link))
            client = self._clients.get(session_id) if session_id else None
            if client is None or not self._allow_client_packet(client, now):
                return

        envelope = unpack_envelope(data)
        if envelope is None:
            return
        remote = link.get_remote_identity()
        if remote is None:
            # RRC has no accounts: the Link is the authentication, so an
            # unidentified client has said nothing this hub can attribute.
            # The identify packet can lose the race with a first HELLO, and
            # the client retries, so this waits rather than dropping the link.
            return
        with self._lock:
            client.identified = True
        self._notify_client_envelope(session_id, remote.hash.hex(), envelope)

    def _allow_client_packet(self, client: "_ClientLink", now: float) -> bool:
        cutoff = now - RRC_PACKET_RATE_WINDOW
        client.packet_times = [t for t in client.packet_times if t > cutoff]
        if len(client.packet_times) >= RRC_PACKET_RATE_LIMIT:
            return False
        client.packet_times.append(now)
        return True

    def _on_client_link_closed(self, link) -> None:
        with self._lock:
            session_id = self._client_of_link.pop(id(link), None)
            if session_id is not None:
                self._clients.pop(session_id, None)
        if session_id is not None:
            self._notify_client_gone(session_id)

    def _teardown(self, link) -> None:
        try:
            link.teardown()
        except Exception as e:
            RNS.log(f"TrenchChat [rrc]: link teardown error: {e}", RNS.LOG_DEBUG)


def _text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value if isinstance(value, str) else ""
