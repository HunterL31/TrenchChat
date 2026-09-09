"""
Hosting an RRC hub: sessions, rooms, and forwarding.

A hub welcomes clients, keeps track of who is in which room, forwards what
they say to the others, and then forgets it. That is the whole job. It holds
no history, buffers nothing for an absent client, and claims no authority:
there is no moderation, no operator command and no ban list here, because a
hub that could do those things is a centre with power over the people using
it rather than a relay anyone can replace.

Running one is off by default and is a deliberate act. It is also the answer
to the awkward part of using RRC at all: a protocol with a hub in the middle
only avoids being a centre if anyone can be that hub, so every TrenchChat
node can be, and a client can move to another one at any time without losing
anything it would not have lost anyway.

All link work is delegated to an injected RRCTransportBase
(network/rrc_transport.py), so tests run against a fake with no mesh. This
manager never touches Storage: a hub stores nothing, which is the point.
"""

import threading
import time

import RNS

from trenchchat.core.rrc_wire import (
    B_CAPS, B_LIMITS, B_NAME, B_VERSION, CAP_ACTION, DEFAULT_LIMITS,
    K_BODY, K_DST, K_ID, K_NICK, K_ROOM, K_T, LIMIT_MSGS_PER_MINUTE,
    LIMIT_MSG_BODY_BYTES, LIMIT_ROOMS_PER_SESSION, T_ACTION, T_ERROR,
    T_HELLO, T_JOIN, T_JOINED, T_MSG, T_NOTICE, T_PART, T_PARTED, T_PING,
    T_PONG, T_WELCOME, is_valid_nick, pack_envelope,
)
from trenchchat.version import app_version

HUB_SOFTWARE = "TrenchChat"

# What this hub can do, advertised in WELCOME. Only what is implemented is
# named, for the same reason the client only names what it implements.
HUB_CAPABILITIES = {
    CAP_ACTION: True,
}

# The limits this hub enforces, which are exactly what its WELCOME promises.
# A client that respects them never trips one.
HUB_LIMITS = dict(DEFAULT_LIMITS)

RATE_WINDOW_SECS = 60.0

# Error text sent to a client that broke a rule. Short: it travels the same
# constrained links everything else does.
ERR_NOT_WELCOMED = "send HELLO first"
ERR_BAD_ROOM = "bad room name"
ERR_TOO_MANY_ROOMS = "too many rooms"
ERR_NOT_IN_ROOM = "not in that room"
ERR_TOO_LONG = "message too long"
ERR_RATE = "slow down"
ERR_ROOM_AND_DST = "room and direct destination are exclusive"
ERR_NO_DIRECT = "direct notices are not supported here"


class _HubSession:
    """One connected client, from its link opening to its link closing."""

    def __init__(self, session_id: str, identity_hex: str):
        self.session_id = session_id
        self.identity_hex = identity_hex
        self.welcomed = False
        self.nickname = ""
        self.rooms: set[str] = set()
        self.message_times: list[float] = []


class RRCHubManager:
    """An RRC hub: welcome, join, forward, discard."""

    def __init__(self, config, transport, *, hub_name: str | None = None):
        self._config = config
        self._transport = transport
        self._lock = threading.RLock()
        self._sessions: dict[str, _HubSession] = {}
        self._rooms: dict[str, set[str]] = {}
        self._name = hub_name if hub_name is not None else config.rrc_hub_name

        transport.set_client_envelope_callback(self._on_client_envelope)
        transport.set_client_gone_callback(self._on_client_gone)

    # --- hosting ---

    def start(self) -> str | None:
        """Begin serving, and remember that this node does."""
        name = self._name or f"{HUB_SOFTWARE} hub"
        hub_hash = self._transport.start_hosting(name)
        self._config.rrc_hosting_enabled = True
        self._config.rrc_hub_name = name
        self._name = name
        return hub_hash

    def stop(self) -> None:
        self._transport.stop_hosting()
        with self._lock:
            self._sessions.clear()
            self._rooms.clear()
        self._config.rrc_hosting_enabled = False

    def restore(self) -> str | None:
        """Start hosting again if this node was hosting when it last ran."""
        if not self._config.rrc_hosting_enabled:
            return None
        return self.start()

    def set_hosting(self, *, enabled: bool | None = None,
                    hub_name: str | None = None) -> dict:
        """Apply a partial hosting change and report the new status."""
        if hub_name is not None:
            self._name = hub_name.strip()
            self._config.rrc_hub_name = self._name
            if self.is_hosting():
                self._transport.start_hosting(self._name or
                                              f"{HUB_SOFTWARE} hub")
        if enabled is True:
            self.start()
        elif enabled is False:
            self.stop()
        return self.status()

    def is_hosting(self) -> bool:
        return self._transport.hosted_hash() is not None

    def status(self) -> dict:
        with self._lock:
            rooms = {room: len(members) for room, members in self._rooms.items()}
            clients = len(self._sessions)
        return {
            "enabled": self.is_hosting(),
            "hub_hash": self._transport.hosted_hash(),
            "name": self._name,
            "clients": clients,
            "rooms": rooms,
            "limits": dict(HUB_LIMITS),
        }

    # --- inbound ---

    def _on_client_envelope(self, session_id: str, identity_hex: str,
                            envelope: dict) -> None:
        msg_type = envelope.get(K_T)
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                session = _HubSession(session_id, identity_hex)
                self._sessions[session_id] = session
            elif session.identity_hex != identity_hex:
                # One link, one identity. A link whose identity changed
                # underneath us is not a session this hub can attribute.
                self._transport.drop_client(session_id, "identity changed")
                return
            welcomed = session.welcomed

        if msg_type == T_HELLO:
            self._welcome(session, envelope)
            return
        if not welcomed:
            # Before WELCOME the hub processes nothing, which is the rule the
            # specification puts on both ends.
            self._error(session_id, ERR_NOT_WELCOMED)
            return

        self._remember_nick(session, envelope)
        if msg_type == T_JOIN:
            self._join(session, envelope)
        elif msg_type == T_PART:
            self._part(session, envelope)
        elif msg_type in (T_MSG, T_NOTICE, T_ACTION):
            self._forward(session, envelope, msg_type)
        elif msg_type == T_PING:
            self._send(session_id, T_PONG, msg_id=envelope.get(K_ID))
        # Anything else, including a type this hub does not know, is ignored
        # rather than refused: that is what lets the protocol grow.

    def _on_client_gone(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(session_id, None)
            if session is None:
                return
            emptied = []
            for room in session.rooms:
                members = self._rooms.get(room)
                if members is None:
                    continue
                members.discard(session_id)
                if not members:
                    emptied.append(room)
            for room in emptied:
                # A room with no members does not exist.
                self._rooms.pop(room, None)

    # --- handlers ---

    def _welcome(self, session: _HubSession, envelope: dict) -> None:
        with self._lock:
            if session.welcomed:
                # A second HELLO on a live session would re-announce limits
                # and cost a packet for something the client already has.
                return
            session.welcomed = True
        self._remember_nick(session, envelope)
        self._send(session.session_id, T_WELCOME, body={
            B_NAME: self._name or f"{HUB_SOFTWARE} hub",
            B_VERSION: app_version(),
            B_CAPS: dict(HUB_CAPABILITIES),
            B_LIMITS: dict(HUB_LIMITS),
        })
        RNS.log(f"TrenchChat [rrc-hub]: welcomed "
                f"{session.identity_hex[:12]}…", RNS.LOG_NOTICE)

    def _join(self, session: _HubSession, envelope: dict) -> None:
        # unpack_envelope drops an envelope whose room name is not one, so
        # what is left to check here is that a room was named at all.
        room = envelope.get(K_ROOM)
        if room is None:
            self._error(session.session_id, ERR_BAD_ROOM)
            return
        with self._lock:
            if room not in session.rooms and \
                    len(session.rooms) >= HUB_LIMITS[LIMIT_ROOMS_PER_SESSION]:
                self._error(session.session_id, ERR_TOO_MANY_ROOMS, room=room)
                return
            session.rooms.add(room)
            members = self._rooms.setdefault(room, set())
            members.add(session.session_id)
            roster = self._roster(members)
        self._send(session.session_id, T_JOINED, room=room, body=roster)

    def _part(self, session: _HubSession, envelope: dict) -> None:
        room = envelope.get(K_ROOM)
        if room is None:
            self._error(session.session_id, ERR_BAD_ROOM)
            return
        with self._lock:
            if room not in session.rooms:
                self._error(session.session_id, ERR_NOT_IN_ROOM, room=room)
                return
            session.rooms.discard(room)
            members = self._rooms.get(room, set())
            members.discard(session.session_id)
            if not members:
                self._rooms.pop(room, None)
        self._send(session.session_id, T_PARTED, room=room)

    def _forward(self, session: _HubSession, envelope: dict,
                 msg_type: int) -> None:
        if K_DST in envelope:
            # CAP_DIRECT_NOTICE is not advertised, so a client that sends one
            # is told rather than left waiting for a delivery that will not
            # happen.
            self._error(session.session_id,
                        ERR_ROOM_AND_DST if K_ROOM in envelope else ERR_NO_DIRECT)
            return

        room = envelope.get(K_ROOM)
        body = envelope.get(K_BODY)
        if room is None:
            self._error(session.session_id, ERR_BAD_ROOM)
            return
        if not self._allow_message(session):
            self._error(session.session_id, ERR_RATE, room=room)
            return
        if isinstance(body, str) and \
                len(body.encode("utf-8")) > HUB_LIMITS[LIMIT_MSG_BODY_BYTES]:
            self._error(session.session_id, ERR_TOO_LONG, room=room)
            return

        with self._lock:
            if room not in session.rooms:
                self._error(session.session_id, ERR_NOT_IN_ROOM, room=room)
                return
            targets = [sid for sid in self._rooms.get(room, set())
                       if sid != session.session_id]
            nick = session.nickname

        RNS.log(f"TrenchChat [rrc-hub]: forwarding type {msg_type} in {room} "
                f"from {session.identity_hex[:12]}… to {len(targets)}",
                RNS.LOG_DEBUG)
        for target in targets:
            # K_SRC is the authenticated sender, never the value the client
            # put in the envelope: a client may claim anything, and the link
            # is the only thing that proves who it is.
            self._send(target, msg_type, room=room, body=body,
                       nick=nick or None, msg_id=envelope.get(K_ID),
                       src_hex=session.identity_hex)

    # --- helpers ---

    def _remember_nick(self, session: _HubSession, envelope: dict) -> None:
        nick = envelope.get(K_NICK)
        if is_valid_nick(nick):
            with self._lock:
                session.nickname = nick

    def _allow_message(self, session: _HubSession) -> bool:
        now = time.time()
        cutoff = now - RATE_WINDOW_SECS
        with self._lock:
            session.message_times = [t for t in session.message_times
                                     if t > cutoff]
            if len(session.message_times) >= HUB_LIMITS[LIMIT_MSGS_PER_MINUTE]:
                return False
            session.message_times.append(now)
        return True

    def _roster(self, member_ids: set[str]) -> list[bytes]:
        """Caller holds the lock. Who is in a room, as identity hashes."""
        roster = []
        for sid in sorted(member_ids):
            member = self._sessions.get(sid)
            if member is None:
                continue
            try:
                roster.append(bytes.fromhex(member.identity_hex))
            except ValueError:
                continue
        return roster

    def _error(self, session_id: str, text: str, room: str | None = None) -> None:
        RNS.log(f"TrenchChat [rrc-hub]: refusing {session_id[:8]}…"
                f"{' in ' + room if room else ''}: {text}", RNS.LOG_WARNING)
        self._send(session_id, T_ERROR, body=text, room=room)

    def _send(self, session_id: str, msg_type: int, *, src_hex: str | None = None,
              **fields) -> bool:
        src = self._hub_src() if src_hex is None else _hash_bytes(src_hex)
        try:
            payload = pack_envelope(msg_type, src=src, **fields)
        except ValueError as e:
            RNS.log(f"TrenchChat [rrc-hub]: refusing to send type "
                    f"{msg_type}: {e}", RNS.LOG_WARNING)
            return False
        return self._transport.send_to_client(session_id, payload)

    def _hub_src(self) -> bytes:
        return _hash_bytes(self._transport.hosted_hash() or "")


def _hash_bytes(value: str) -> bytes:
    try:
        return bytes.fromhex(value)
    except ValueError:
        return b""
