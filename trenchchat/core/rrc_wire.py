"""
Wire format for RRC (Reticulum Relay Chat), the protocol rrcd speaks.

This is the RRC counterpart of core/protocol.py: the single place RRC's
envelope keys, message types and limits are defined, deliberately
dependency-free. Nothing here is TrenchChat's to change. The numbers come
from the RRC specification (https://rrc.kc1awv.net/, documents 1 to 5) and
rrcd's EX1-RRCD extension document, and a change to any of them breaks
interop with every other RRC client and hub.

Every message is one CBOR map with unsigned integer keys:

    0 K_V     protocol version      uint
    1 K_T     message type          uint
    2 K_ID    message id            8 bytes
    3 K_TS    timestamp             uint64, milliseconds
    4 K_SRC   sender identity hash  bytes
    5 K_ROOM  room name             text, optional
    6 K_BODY  body                  varies, optional
    7 K_NICK  nickname              text, optional, advisory
    8 K_DST   direct recipient      bytes, optional, NOTICE only

Keys 0 to 4 are the fixed 43-byte overhead. At Reticulum's default 500-byte
MTU that leaves roughly 422 bytes for everything else, which is what the
byte limits below are sized against.

unpack_envelope() is the trust boundary. Everything it returns has been
bounded and type-checked; everything it cannot vouch for is dropped. It
never raises, and an unrecognised message type decodes normally so callers
can ignore it, which is what makes the protocol's forward compatibility
work.
"""

import os
import time

import cbor2

RRC_VERSION = 1

# Envelope keys.
K_V = 0
K_T = 1
K_ID = 2
K_TS = 3
K_SRC = 4
K_ROOM = 5
K_BODY = 6
K_NICK = 7
K_DST = 8

# Message types. 0 to 49 are reserved for the core protocol; 50 up are
# extensions, and T_RESOURCE_ENVELOPE is rrcd's.
T_HELLO = 1
T_WELCOME = 2
T_JOIN = 10
T_JOINED = 11
T_PART = 12
T_PARTED = 13
T_MSG = 20
T_NOTICE = 21
T_ACTION = 22
T_PING = 30
T_PONG = 31
T_ERROR = 40
T_RESOURCE_ENVELOPE = 50

CONTENT_TYPES = (T_MSG, T_NOTICE, T_ACTION)

# HELLO and WELCOME body keys.
B_NAME = 0
B_VERSION = 1
B_CAPS = 2
B_LIMITS = 3

# Capabilities, carried as a CBOR map in B_CAPS rather than a bitmask.
CAP_RESOURCE_ENVELOPE = 0
CAP_ACTION = 1
CAP_DIRECT_NOTICE = 2

# Hub limits, advertised in the WELCOME body under B_LIMITS. String keys,
# unlike everything else here, because the specification names them.
LIMIT_NICK_BYTES = "max_nick_bytes"
LIMIT_ROOMS_PER_SESSION = "max_rooms_per_session"
LIMIT_ROOM_NAME_BYTES = "max_room_name_bytes"
LIMIT_MSG_BODY_BYTES = "max_msg_body_bytes"
LIMIT_MSGS_PER_MINUTE = "rate_limit_msgs_per_minute"

# T_RESOURCE_ENVELOPE body keys.
B_RES_ID = 0
B_RES_KIND = 1
B_RES_SIZE = 2
B_RES_SHA256 = 3
B_RES_ENCODING = 4

RES_KINDS = ("notice", "motd", "blob")

MESSAGE_ID_BYTES = 8

MAX_NICK_BYTES = 32
MAX_ROOM_NAME_BYTES = 64
# A hub's announced name is unsigned text; this is all of it we keep.
MAX_HUB_NAME_BYTES = 64
# Chosen so a maximum-length body, a maximum-length room name and a
# maximum-length nickname together still fit the 465 bytes Reticulum leaves
# at its default MTU with 32-byte addresses, which keeps a full message one
# packet on the slowest link anyone runs.
MAX_MSG_BODY_BYTES = 312
MAX_SRC_BYTES = 32
MAX_ROOMS_PER_SESSION = 16
MAX_MSGS_PER_MINUTE = 60
MAX_RESOURCE_BYTES = 256 * 1024

# Ceiling on one decoded envelope. An envelope always travels as a link
# packet, so it cannot exceed the link MDU in practice; this is headroom
# above that rather than a protocol limit, and it bounds the decoder against
# a peer that is not sending over a link at all.
MAX_ENVELOPE_BYTES = 2048

# A timestamp this far ahead of our clock is not a clock difference.
MAX_CLOCK_SKEW_MS = 5 * 60 * 1000

HUB_APP_NAME = "rrc"
HUB_ASPECT = "hub"

DEFAULT_LIMITS = {
    LIMIT_NICK_BYTES: MAX_NICK_BYTES,
    LIMIT_ROOMS_PER_SESSION: MAX_ROOMS_PER_SESSION,
    LIMIT_ROOM_NAME_BYTES: MAX_ROOM_NAME_BYTES,
    LIMIT_MSG_BODY_BYTES: MAX_MSG_BODY_BYTES,
    LIMIT_MSGS_PER_MINUTE: MAX_MSGS_PER_MINUTE,
}

# A hub advertising an implausible limit does not get to size our buffers.
_LIMIT_CEILINGS = {
    LIMIT_NICK_BYTES: 256,
    LIMIT_ROOMS_PER_SESSION: 256,
    LIMIT_ROOM_NAME_BYTES: 256,
    LIMIT_MSG_BODY_BYTES: MAX_ENVELOPE_BYTES,
    LIMIT_MSGS_PER_MINUTE: 6000,
}


def now_ms() -> int:
    return int(time.time() * 1000)


def new_message_id() -> bytes:
    return os.urandom(MESSAGE_ID_BYTES)


def normalise_room(name: str) -> str:
    """A room name in the form two clients will agree on.

    Hubs treat room names case-insensitively and IRC-style, so a leading
    '#' is added when it is missing and the whole thing is lower-cased.
    """
    name = name.strip().lower()
    if name and not name.startswith("#"):
        name = "#" + name
    return name


def is_valid_room(name: object) -> bool:
    if not isinstance(name, str) or not name.startswith("#") or len(name) < 2:
        return False
    if len(name.encode("utf-8", errors="replace")) > MAX_ROOM_NAME_BYTES:
        return False
    return not _has_control_chars(name)


def is_valid_nick(nick: object) -> bool:
    if not isinstance(nick, str) or not nick:
        return False
    if len(nick.encode("utf-8", errors="replace")) > MAX_NICK_BYTES:
        return False
    return not _has_control_chars(nick)


def _has_control_chars(value: str) -> bool:
    return any(ord(c) < 0x20 or ord(c) == 0x7F for c in value)


def wire_timestamp_ms(value: object, now: int | None = None) -> int | None:
    """A peer-supplied millisecond timestamp, or None if it isn't plausible.

    K_TS is self-asserted. Unbounded, a far-future value pins a line to the
    top of a transcript for as long as the session lasts.
    """
    now = now_ms() if now is None else now
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or value > now + MAX_CLOCK_SKEW_MS:
        return None
    return value


def pack_envelope(msg_type: int, *, body=None, room: str | None = None,
                  src: bytes | None = None, nick: str | None = None,
                  dst: bytes | None = None, msg_id: bytes | None = None,
                  timestamp_ms: int | None = None) -> bytes:
    """Encode one RRC envelope.

    Raises ValueError on anything this node should not be putting on the
    wire, so a local bug fails here rather than at the far end.
    """
    if msg_id is None:
        msg_id = new_message_id()
    if len(msg_id) != MESSAGE_ID_BYTES:
        raise ValueError("message id must be 8 bytes")
    if room is not None and not is_valid_room(room):
        raise ValueError(f"invalid room name: {room!r}")
    if nick is not None and not is_valid_nick(nick):
        raise ValueError("invalid nickname")
    if room is not None and dst is not None:
        raise ValueError("K_ROOM and K_DST are mutually exclusive")

    envelope = {
        K_V: RRC_VERSION,
        K_T: msg_type,
        K_ID: msg_id,
        K_TS: now_ms() if timestamp_ms is None else timestamp_ms,
        K_SRC: b"" if src is None else src,
    }
    if room is not None:
        envelope[K_ROOM] = room
    if body is not None:
        envelope[K_BODY] = body
    if nick is not None:
        envelope[K_NICK] = nick
    if dst is not None:
        envelope[K_DST] = dst
    return cbor2.dumps(envelope)


def unpack_envelope(data: bytes) -> dict | None:
    """Decode and validate one RRC envelope, or None if it is not one.

    The returned dict holds only recognised keys, each already checked for
    type and length. An advisory field that fails its check is dropped
    rather than failing the whole envelope, because losing a nickname is
    better than losing the line it was attached to.
    """
    if not isinstance(data, (bytes, bytearray)) or not data:
        return None
    if len(data) > MAX_ENVELOPE_BYTES:
        return None
    try:
        decoded = cbor2.loads(bytes(data))
    except Exception:
        return None
    if not isinstance(decoded, dict):
        return None

    version = decoded.get(K_V)
    msg_type = decoded.get(K_T)
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        return None
    if isinstance(msg_type, bool) or not isinstance(msg_type, int) or msg_type < 0:
        return None

    envelope: dict = {K_V: version, K_T: msg_type}

    msg_id = decoded.get(K_ID)
    if isinstance(msg_id, bytes) and len(msg_id) == MESSAGE_ID_BYTES:
        envelope[K_ID] = msg_id

    timestamp = wire_timestamp_ms(decoded.get(K_TS))
    if timestamp is not None:
        envelope[K_TS] = timestamp

    src = decoded.get(K_SRC)
    if isinstance(src, bytes) and 0 < len(src) <= MAX_SRC_BYTES:
        envelope[K_SRC] = src

    room = decoded.get(K_ROOM)
    if room is not None:
        if not is_valid_room(room):
            return None
        envelope[K_ROOM] = room.lower()

    nick = decoded.get(K_NICK)
    if is_valid_nick(nick):
        envelope[K_NICK] = nick

    dst = decoded.get(K_DST)
    if isinstance(dst, bytes) and 0 < len(dst) <= MAX_SRC_BYTES:
        envelope[K_DST] = dst

    if K_ROOM in envelope and K_DST in envelope:
        return None

    if K_BODY in decoded:
        body = decoded[K_BODY]
        if not _is_sane_body(body):
            return None
        envelope[K_BODY] = body

    return envelope


def _is_sane_body(body: object, depth: int = 0) -> bool:
    """Whether a decoded body holds only the shapes RRC bodies are made of.

    CBOR can carry tags that decode to arbitrary Python objects (dates,
    decimals, sets, and self-referencing structures). None of those appear
    in an RRC body, so anything that is not a string, number, bytes, list or
    map of those is refused rather than handed on to a caller that will not
    be expecting it.
    """
    if depth > 4:
        return False
    if body is None or isinstance(body, (str, bytes, bool, int, float)):
        return True
    if isinstance(body, list):
        return all(_is_sane_body(item, depth + 1) for item in body)
    if isinstance(body, dict):
        return all(
            isinstance(key, (int, str)) and _is_sane_body(value, depth + 1)
            for key, value in body.items()
        )
    return False


def body_text(envelope: dict) -> str:
    """The human-readable text of a content or error message.

    A body is text for MSG, NOTICE, ACTION and ERROR. Anything else there
    is not something to show a person, so it reads as empty.
    """
    body = envelope.get(K_BODY)
    if isinstance(body, str):
        return body
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="replace")
    return ""


def capabilities_of(envelope: dict) -> dict:
    """The capability map from a HELLO or WELCOME body.

    Capabilities are a map keyed by capability number. rrcd's own clients
    have shipped them as a list too, so both are read and the result is
    always a map.
    """
    body = envelope.get(K_BODY)
    if not isinstance(body, dict):
        return {}
    caps = body.get(B_CAPS)
    if isinstance(caps, dict):
        return {k: v for k, v in caps.items() if isinstance(k, int)}
    if isinstance(caps, list):
        return {c: True for c in caps if isinstance(c, int) and not isinstance(c, bool)}
    return {}


def limits_of(envelope: dict) -> dict:
    """The hub limits from a WELCOME body, filled in from ours where absent.

    A hub need not advertise any of these, so a missing one falls back to
    the value this node would enforce itself rather than to no limit.
    """
    limits = dict(DEFAULT_LIMITS)
    body = envelope.get(K_BODY)
    if not isinstance(body, dict):
        return limits
    advertised = body.get(B_LIMITS)
    if not isinstance(advertised, dict):
        return limits
    for key, value in advertised.items():
        if key in limits and isinstance(value, int) and not isinstance(value, bool):
            if 0 < value <= _LIMIT_CEILINGS[key]:
                limits[key] = value
    return limits

