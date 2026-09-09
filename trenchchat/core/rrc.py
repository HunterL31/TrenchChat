"""
RRC client: hub registry, room membership, and the session's transcript.

TrenchChat's public chat is RRC (Reticulum Relay Chat), the protocol rrcd
speaks, rather than anything this project defines. What lives here is
everything that is not wire protocol: which hubs have been heard, which one
is connected, which rooms are joined, and the lines seen while joined. All
link and envelope work is delegated to an injected RRCTransportBase
(network/rrc_transport.py), so tests run against a fake with no mesh.

Nothing here is durable and nothing here touches Storage. RRC hubs hold no
history and buffer nothing for an absent client, so a transcript is only
what this node was present for: it lives in memory, bounded per room, and is
dropped when the session ends. That is the protocol's own bargain, not a
limitation of this implementation, and the client says so rather than
implying a message might still arrive.

One session at a time. RRC allows a client to hold links to several hubs,
but a person reads one conversation at a time and every extra link is a
standing cost on a shared medium, so connecting to a second hub replaces the
first.
"""

import threading
import time

import RNS

from trenchchat.core.rrc_wire import (
    K_BODY, K_ID, K_NICK, K_ROOM, K_SRC, K_T, K_TS,
    LIMIT_MSG_BODY_BYTES, LIMIT_ROOMS_PER_SESSION, MAX_MSG_BODY_BYTES,
    MAX_HUB_NAME_BYTES, MAX_NICK_BYTES, MAX_ROOMS_PER_SESSION, T_ACTION, T_ERROR, T_JOIN,
    T_JOINED, T_MSG, T_NOTICE, T_PART, T_PARTED,
    body_text, is_valid_nick, normalise_room, pack_envelope,
)
from trenchchat.network.rrc_transport import (
    SESSION_ACTIVE, SESSION_IDLE, RRCTransportBase,
)

# Lines kept per room. A transcript is a scrollback, not a store: it exists
# so a person can read back over the conversation they were present for.
MAX_ROOM_LINES = 300
# Hubs heard on the mesh, oldest dropped first. Identities are free to mint,
# so an announce is not evidence of anything and this list is bounded.
MAX_KNOWN_HUBS = 200
MAX_BOOKMARKS = 32

# Room membership states, reported to the client.
ROOM_JOINING = "joining"
ROOM_JOINED = "joined"
ROOM_PARTING = "parting"


class RRCManager:
    """The RRC client's state: hubs heard, the session, rooms and lines."""

    def __init__(self, identity, config, transport: RRCTransportBase):
        self._identity = identity
        self._config = config
        self._transport = transport
        self._lock = threading.RLock()

        self._hubs: dict[str, dict] = {}
        self._hub_hex: str | None = None
        self._rooms: dict[str, str] = {}
        self._rosters: dict[str, set[str]] = {}
        self._lines: dict[str, list[dict]] = {}

        self._line_callbacks: list = []
        self._session_callbacks: list = []
        self._room_callbacks: list = []
        self._hub_callbacks: list = []

        transport.set_envelope_callback(self._on_envelope)
        transport.set_session_callback(self._on_session_state)

    # --- callbacks ---

    def add_line_callback(self, cb) -> None:
        """cb(room, line): one line arrived in a joined room."""
        if cb not in self._line_callbacks:
            self._line_callbacks.append(cb)

    def add_session_callback(self, cb) -> None:
        """cb(hub_hex, state, reason): the session changed state."""
        if cb not in self._session_callbacks:
            self._session_callbacks.append(cb)

    def add_room_callback(self, cb) -> None:
        """cb(room, state): a room was joined or left."""
        if cb not in self._room_callbacks:
            self._room_callbacks.append(cb)

    def add_hub_callback(self, cb) -> None:
        """cb(hub_hex, name): a hub was heard on the mesh."""
        if cb not in self._hub_callbacks:
            self._hub_callbacks.append(cb)

    # --- hubs heard on the mesh ---

    def note_hub(self, hub_hash_hex: str, name: str = "") -> None:
        """Record a hub heard from an rrc.hub announce.

        An announce carries whatever the hub chose to put in it, so the name
        is a label over a verified destination hash and never an identity.
        """
        name = _clean(name)[:MAX_HUB_NAME_BYTES]
        with self._lock:
            known = self._hubs.get(hub_hash_hex)
            if known is None and len(self._hubs) >= MAX_KNOWN_HUBS:
                oldest = min(self._hubs, key=lambda h: self._hubs[h]["heard_at"])
                if oldest not in self.bookmarks():
                    del self._hubs[oldest]
            entry = known or {"hash": hub_hash_hex, "name": "", "first_heard": time.time()}
            if name:
                entry["name"] = name
            entry["heard_at"] = time.time()
            self._hubs[hub_hash_hex] = entry
            is_new = known is None
        if is_new:
            RNS.log(f"TrenchChat [rrc]: heard hub {hub_hash_hex[:12]}… "
                    f"({name or 'unnamed'})", RNS.LOG_NOTICE)
            self._fire(self._hub_callbacks, hub_hash_hex, name)

    def known_hubs(self) -> list[dict]:
        """Hubs heard on the mesh, most recently heard first."""
        with self._lock:
            hubs = [dict(entry) for entry in self._hubs.values()]
        bookmarked = set(self.bookmarks())
        for entry in hubs:
            entry["bookmarked"] = entry["hash"] in bookmarked
            entry["connected"] = entry["hash"] == self._hub_hex
        return sorted(hubs, key=lambda e: e["heard_at"], reverse=True)

    # --- session ---

    def connect(self, hub_hash_hex: str) -> bool:
        """Open a session to a hub, replacing any session already open.

        Connecting reveals this node's identity to the hub, which then sees
        every room joined and every line typed. That is inherent to RRC, so
        it is always a deliberate act and never automatic.
        """
        if not _is_hash_hex(hub_hash_hex):
            return False
        with self._lock:
            current = self._hub_hex
        if current is not None and current != hub_hash_hex:
            self.disconnect()
        with self._lock:
            self._hub_hex = hub_hash_hex
        self.note_hub(hub_hash_hex)
        self._transport.connect(hub_hash_hex)
        return True

    def disconnect(self) -> None:
        with self._lock:
            hub_hex = self._hub_hex
            self._hub_hex = None
            self._clear_session()
        if hub_hex is not None:
            self._transport.disconnect(hub_hex)

    def session(self) -> dict:
        """The current session, as the client should show it."""
        with self._lock:
            hub_hex = self._hub_hex
            rooms = dict(self._rooms)
        if hub_hex is None:
            return {"hub": None, "state": SESSION_IDLE, "rooms": {}}
        info = self._transport.hub_info(hub_hex)
        return {
            "hub": hub_hex,
            "state": self._transport.session_state(hub_hex),
            "name": info.get("name", ""),
            "version": info.get("version", ""),
            "capabilities": info.get("capabilities", {}),
            "limits": info.get("limits", {}),
            "rooms": rooms,
        }

    def is_active(self) -> bool:
        with self._lock:
            hub_hex = self._hub_hex
        return hub_hex is not None and \
            self._transport.session_state(hub_hex) == SESSION_ACTIVE

    # --- rooms ---

    def join_room(self, room: str) -> bool:
        room = normalise_room(room)
        if not room or not self.is_active():
            return False
        with self._lock:
            if room in self._rooms:
                return True
            limit = self._room_limit()
            if len(self._rooms) >= limit:
                RNS.log(f"TrenchChat [rrc]: refusing to join {room}, at the "
                        f"hub's limit of {limit} rooms", RNS.LOG_WARNING)
                return False
            self._rooms[room] = ROOM_JOINING
        self._fire(self._room_callbacks, room, ROOM_JOINING)
        return self._send(T_JOIN, room=room)

    def part_room(self, room: str) -> bool:
        room = normalise_room(room)
        with self._lock:
            if room not in self._rooms:
                return False
            self._rooms[room] = ROOM_PARTING
        self._fire(self._room_callbacks, room, ROOM_PARTING)
        return self._send(T_PART, room=room)

    def rooms(self) -> dict:
        with self._lock:
            return dict(self._rooms)

    def roster(self, room: str) -> list[str]:
        """Who the hub last said was in a room, as identity hashes.

        A roster is optional in RRC and never authoritative: the hub sends it
        when it feels like it, and a room's membership changes without one.
        """
        with self._lock:
            return sorted(self._rosters.get(normalise_room(room), set()))

    def lines(self, room: str, limit: int = MAX_ROOM_LINES) -> list[dict]:
        with self._lock:
            return list(self._lines.get(normalise_room(room), []))[-limit:]

    # --- sending ---

    def send_message(self, room: str, text: str) -> bool:
        """Send one line to a joined room.

        A leading '/me ' becomes an ACTION, which is what every other RRC
        client renders as an emote.
        """
        room = normalise_room(room)
        text = _clean(text)
        if not text:
            return False
        msg_type = T_MSG
        if text.lower().startswith("/me "):
            msg_type = T_ACTION
            text = text[4:].strip()
        return self._send_content(room, msg_type, text)

    def send_notice(self, room: str, text: str) -> bool:
        return self._send_content(normalise_room(room), T_NOTICE, _clean(text))

    def _send_content(self, room: str, msg_type: int, text: str) -> bool:
        if not text:
            return False
        with self._lock:
            joined = self._rooms.get(room) == ROOM_JOINED
        if not joined:
            return False
        if not self._send(msg_type, room=room, body=self._fit(text)):
            return False
        # A hub forwards to the room's other members and does not echo, so
        # the sender's own line is recorded here or nowhere.
        self._record(room, msg_type, self._identity.hash, text,
                     self._nickname(), own=True)
        return True

    def _send(self, msg_type: int, **fields) -> bool:
        with self._lock:
            hub_hex = self._hub_hex
        if hub_hex is None:
            return False
        nick = self._nickname()
        try:
            payload = pack_envelope(
                msg_type, src=self._identity.hash,
                nick=nick if is_valid_nick(nick) else None, **fields,
            )
        except ValueError as e:
            RNS.log(f"TrenchChat [rrc]: refusing to send type {msg_type}: {e}",
                    RNS.LOG_WARNING)
            return False
        return self._transport.send(hub_hex, payload)

    def _fit(self, text: str) -> str:
        """Trim a line to what the connected hub said it will accept."""
        limit = self._body_limit()
        encoded = text.encode("utf-8")
        if len(encoded) <= limit:
            return text
        return encoded[:limit].decode("utf-8", errors="ignore")

    def _body_limit(self) -> int:
        with self._lock:
            hub_hex = self._hub_hex
        if hub_hex is None:
            return MAX_MSG_BODY_BYTES
        return self._transport.hub_info(hub_hex).get("limits", {}).get(
            LIMIT_MSG_BODY_BYTES, MAX_MSG_BODY_BYTES)

    def _room_limit(self) -> int:
        with self._lock:
            hub_hex = self._hub_hex
        if hub_hex is None:
            return MAX_ROOMS_PER_SESSION
        return self._transport.hub_info(hub_hex).get("limits", {}).get(
            LIMIT_ROOMS_PER_SESSION, MAX_ROOMS_PER_SESSION)

    # --- nickname ---

    def nickname(self) -> str:
        return self._nickname()

    def set_nickname(self, nick: str) -> bool:
        # Validated as typed rather than cleaned first: the specification
        # says reject a nickname with control characters, and quietly
        # rewriting one into something the person did not choose is worse
        # than telling them it will not do.
        nick = nick.strip() if isinstance(nick, str) else ""
        if nick and not is_valid_nick(nick):
            return False
        self._config.rrc_nickname = nick
        return True

    def _nickname(self) -> str:
        nick = _clean(self._config.rrc_nickname)
        if not nick:
            nick = _clean(self._identity.display_name)
        return nick[:MAX_NICK_BYTES]

    # --- bookmarks ---

    def bookmarks(self) -> list[str]:
        return [h for h in self._config.rrc_bookmarks
                if _is_hash_hex(h)][:MAX_BOOKMARKS]

    def add_bookmark(self, hub_hash_hex: str) -> bool:
        if not _is_hash_hex(hub_hash_hex):
            return False
        current = self.bookmarks()
        if hub_hash_hex in current:
            return True
        if len(current) >= MAX_BOOKMARKS:
            return False
        self._config.rrc_bookmarks = current + [hub_hash_hex]
        return True

    def remove_bookmark(self, hub_hash_hex: str) -> bool:
        current = self.bookmarks()
        if hub_hash_hex not in current:
            return False
        self._config.rrc_bookmarks = [h for h in current if h != hub_hash_hex]
        return True

    # --- inbound ---

    def _on_session_state(self, hub_hex: str, state: str, reason: str) -> None:
        with self._lock:
            if hub_hex != self._hub_hex:
                return
            if state != SESSION_ACTIVE:
                # A new link is a new session: the hub remembers nothing, so
                # every room has to be joined again rather than assumed.
                self._clear_session()
        self._fire(self._session_callbacks, hub_hex, state, reason)

    def _on_envelope(self, hub_hex: str, envelope: dict) -> None:
        with self._lock:
            if hub_hex != self._hub_hex:
                return
        msg_type = envelope.get(K_T)
        if msg_type == T_JOINED:
            self._on_joined(envelope)
        elif msg_type == T_PARTED:
            self._on_parted(envelope)
        elif msg_type in (T_MSG, T_NOTICE, T_ACTION):
            self._on_content(msg_type, envelope)
        elif msg_type == T_ERROR:
            self._on_error(envelope)

    def _on_joined(self, envelope: dict) -> None:
        room = envelope.get(K_ROOM)
        if room is None:
            return
        with self._lock:
            self._rooms[room] = ROOM_JOINED
            self._rosters[room] = _roster_from(envelope)
        RNS.log(f"TrenchChat [rrc]: joined {room}", RNS.LOG_NOTICE)
        self._fire(self._room_callbacks, room, ROOM_JOINED)

    def _on_parted(self, envelope: dict) -> None:
        room = envelope.get(K_ROOM)
        if room is None:
            return
        with self._lock:
            self._rooms.pop(room, None)
            self._rosters.pop(room, None)
            self._lines.pop(room, None)
        self._fire(self._room_callbacks, room, SESSION_IDLE)

    def _on_content(self, msg_type: int, envelope: dict) -> None:
        text = body_text(envelope)
        if not text:
            return
        room = envelope.get(K_ROOM)
        # A direct NOTICE carries K_DST and no room. Nothing here can show
        # one yet, so it is dropped rather than filed somewhere a person
        # will never look; the client does not advertise CAP_DIRECT_NOTICE,
        # so a well-behaved hub never sends one.
        if room is None:
            return
        with self._lock:
            joined = room in self._rooms
        if not joined:
            RNS.log(f"TrenchChat [rrc]: dropped a line for {room}, "
                    f"which this session is not in", RNS.LOG_DEBUG)
            return
        self._record(room, msg_type, envelope.get(K_SRC), text,
                     envelope.get(K_NICK, ""),
                     timestamp_ms=envelope.get(K_TS),
                     msg_id=envelope.get(K_ID))

    def _on_error(self, envelope: dict) -> None:
        text = body_text(envelope) or "the hub refused that"
        room = envelope.get(K_ROOM)
        RNS.log(f"TrenchChat [rrc]: hub error: {text}", RNS.LOG_WARNING)
        with self._lock:
            hub_hex = self._hub_hex
            if room is not None and self._rooms.get(room) == ROOM_JOINING:
                self._rooms.pop(room, None)
        if hub_hex is not None:
            self._fire(self._session_callbacks, hub_hex, "error", text)

    def _record(self, room: str, msg_type: int, src, text: str, nick: str, *,
                own: bool = False, timestamp_ms: int | None = None,
                msg_id: bytes | None = None) -> None:
        line = {
            "room": room,
            "type": msg_type,
            "source": src.hex() if isinstance(src, bytes) else "",
            "nick": _clean(nick),
            "text": text,
            "at": (timestamp_ms or 0) / 1000.0 or time.time(),
            "id": msg_id.hex() if isinstance(msg_id, bytes) else "",
            "own": own,
        }
        with self._lock:
            lines = self._lines.setdefault(room, [])
            lines.append(line)
            if len(lines) > MAX_ROOM_LINES:
                del lines[:len(lines) - MAX_ROOM_LINES]
        self._fire(self._line_callbacks, room, line)

    def _clear_session(self) -> None:
        self._rooms.clear()
        self._rosters.clear()
        self._lines.clear()

    def _fire(self, callbacks: list, *args) -> None:
        for cb in list(callbacks):
            try:
                cb(*args)
            except Exception as e:
                RNS.log(f"TrenchChat [rrc]: callback error: {e}", RNS.LOG_ERROR)

    def tick(self) -> None:
        self._transport.tick()



def _clean(value) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str):
        return ""
    return "".join(c for c in value if ord(c) >= 0x20 and ord(c) != 0x7F).strip()


def _is_hash_hex(value) -> bool:
    if not isinstance(value, str) or len(value) % 2 or not value:
        return False
    try:
        bytes.fromhex(value)
    except ValueError:
        return False
    return True


def _roster_from(envelope: dict) -> set[str]:
    """The member list a JOINED or PARTED body may carry.

    rrcd has shipped both a single hash and a full list here, so both are
    read and anything else is treated as no roster at all.
    """
    body = envelope.get(K_BODY)
    if isinstance(body, bytes):
        return {body.hex()}
    if isinstance(body, list):
        return {m.hex() for m in body if isinstance(m, bytes)}
    if isinstance(body, dict):
        for value in body.values():
            if isinstance(value, list):
                return {m.hex() for m in value if isinstance(m, bytes)}
    return set()
