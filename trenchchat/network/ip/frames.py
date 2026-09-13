"""
The direct session's wire format: a type byte, a length, and a msgpack body.

Seven frame types carry everything. HI opens the control stream, CHALLENGE
carries the listener's nonce and HELLO the proof of identity over it; those
three are the handshake, and nothing else is read until it passes. MSG carries
one message envelope with the author's signature over it, and ACK names an
envelope the receiver has taken, which is what lets "delivered" mean
acknowledged on this path. REQ and RESP carry the file plane's exchanges on
their own streams, so a chat message never waits behind a chunk.

Every decode states its limits rather than trusting the payload, the way
protocol.unpack_wire does: the peer on the other end is assumed hostile, and a
frame over the ceiling ends the session instead of being parsed.
"""

import hashlib
import struct

import msgpack

# kind byte, then a four-byte big-endian body length.
FRAME_HEADER = "!BI"
FRAME_HEADER_BYTES = struct.calcsize(FRAME_HEADER)

KIND_HI = 0x01
KIND_CHALLENGE = 0x02
KIND_HELLO = 0x03
KIND_MSG = 0x10
KIND_ACK = 0x11
KIND_REQ = 0x20
KIND_RESP = 0x21

HANDSHAKE_KINDS = (KIND_HI, KIND_CHALLENGE, KIND_HELLO)

# Ceiling on one frame. The largest is a sync response at the direct path's
# batch budget; the handshake ceiling is what applies until a HELLO has passed,
# so an unauthenticated peer can never make this node hold megabytes.
MAX_FRAME_BYTES = 16 * 1024 * 1024
MAX_HANDSHAKE_FRAME_BYTES = 8 * 1024

# Bounds for anything unpacked off this wire.
MAX_WIRE_ARRAY = 4096
MAX_WIRE_MAP = 4096
MAX_WIRE_STR = 1 * 1024 * 1024
MAX_WIRE_BIN = MAX_FRAME_BYTES

# Domain tag, so an envelope signature can never be replayed as one of the
# other structures the same Ed25519 key signs.
ENVELOPE_DOMAIN = b"trenchchat-session-v1"

# The longest address text a HELLO may name: an IPv6 literal with a zone.
MAX_HOST_CHARS = 64

IDENTITY_HASH_BYTES = 16
ENVELOPE_HASH_BYTES = 32

_ENVELOPE_KEYS = ("src", "dst", "ts", "content", "fields", "proto")


class FrameError(ValueError):
    """A frame that cannot be read as one. The session closes rather than guess."""


def encode_frame(kind: int, payload: dict) -> bytes:
    """One frame, ready to write to a stream."""
    body = msgpack.packb(payload, use_bin_type=True)
    if len(body) > MAX_FRAME_BYTES:
        raise FrameError(f"frame body is {len(body)} bytes, over "
                         f"{MAX_FRAME_BYTES}")
    return struct.pack(FRAME_HEADER, kind, len(body)) + body


def decode_body(body: bytes) -> dict:
    """The payload of one frame, with every limit stated."""
    try:
        payload = msgpack.unpackb(
            body,
            raw=False,
            strict_map_key=False,
            max_array_len=MAX_WIRE_ARRAY,
            max_map_len=MAX_WIRE_MAP,
            max_str_len=MAX_WIRE_STR,
            max_bin_len=MAX_WIRE_BIN,
        )
    except Exception as e:
        raise FrameError(f"frame body does not parse: {e}") from e
    if not isinstance(payload, dict):
        raise FrameError("frame body is not a map")
    return payload


class FrameDecoder:
    """Turns a stream's bytes into frames, refusing anything over the limit.

    limit is raised from the handshake ceiling to the full one once a session
    has authenticated, so an unproven peer is held to the smaller budget.
    """

    def __init__(self, limit: int = MAX_HANDSHAKE_FRAME_BYTES):
        self.limit = limit
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[tuple[int, dict]]:
        """Every complete frame in what has arrived so far."""
        self._buffer += data
        out: list[tuple[int, dict]] = []
        while len(self._buffer) >= FRAME_HEADER_BYTES:
            kind, size = struct.unpack(
                FRAME_HEADER, bytes(self._buffer[:FRAME_HEADER_BYTES]))
            if size > self.limit:
                raise FrameError(f"inbound frame is {size} bytes, over "
                                 f"{self.limit}")
            if len(self._buffer) < FRAME_HEADER_BYTES + size:
                break
            body = bytes(self._buffer[FRAME_HEADER_BYTES:FRAME_HEADER_BYTES + size])
            del self._buffer[:FRAME_HEADER_BYTES + size]
            out.append((kind, decode_body(body)))
        return out

    def buffered(self) -> int:
        """Bytes held for a frame that has not finished arriving."""
        return len(self._buffer)


# --- the message envelope ---


def pack_envelope(*, src: bytes, dst: bytes, timestamp: float, content: str,
                  fields: dict, protocol: bool) -> bytes:
    """One message as it travels: the bytes a signature covers.

    fields is exactly the dict that would have gone to LXMF, and protocol says
    whether it is TrenchChat's own field registry or another client's keys, so
    the receiver hands it to Router marked the same way either path.
    """
    return msgpack.packb({
        "src": src,
        "dst": dst,
        "ts": float(timestamp),
        "content": content,
        "fields": fields,
        "proto": bool(protocol),
    }, use_bin_type=True)


def unpack_envelope(blob: bytes) -> dict:
    """An inbound envelope, with every field's type checked before it is used."""
    if len(blob) > MAX_FRAME_BYTES:
        raise FrameError(f"envelope is {len(blob)} bytes, over {MAX_FRAME_BYTES}")
    envelope = decode_body(blob)
    missing = [key for key in _ENVELOPE_KEYS if key not in envelope]
    if missing:
        raise FrameError(f"envelope is missing {', '.join(missing)}")
    for key in ("src", "dst"):
        value = envelope[key]
        if not isinstance(value, bytes) or len(value) != IDENTITY_HASH_BYTES:
            raise FrameError(f"envelope {key} is not an identity hash")
    if not isinstance(envelope["ts"], (int, float)) or \
            isinstance(envelope["ts"], bool):
        raise FrameError("envelope ts is not a number")
    if not isinstance(envelope["content"], str):
        raise FrameError("envelope content is not text")
    if not isinstance(envelope["fields"], dict):
        raise FrameError("envelope fields is not a map")
    if not isinstance(envelope["proto"], bool):
        raise FrameError("envelope proto is not a flag")
    return envelope


def envelope_digest(blob: bytes) -> bytes:
    """The bytes an author signs for one envelope."""
    return ENVELOPE_DOMAIN + blob


def envelope_hash(blob: bytes) -> bytes:
    """The name an ACK calls an envelope by."""
    return hashlib.sha256(blob).digest()


# --- frame builders ---


def hi_frame() -> bytes:
    """Open the control stream. Carries nothing: the listener answers first."""
    return encode_frame(KIND_HI, {})


def challenge_frame(nonce: bytes) -> bytes:
    """The listener's nonce, fresh per connection."""
    return encode_frame(KIND_CHALLENGE, {"nonce": nonce})


def hello_frame(public_key: bytes, timestamp: int, signature: bytes,
                certificate: bytes | None = None,
                seen: tuple[str, int] | None = None) -> bytes:
    """This node's identity, bound to this connection by the signature.

    certificate is set by the connecting side only: the listener's own
    certificate is already pinned by whoever dialled it. seen is set by both
    sides: it is where this node saw the other's packets arrive from, which is
    that peer's own translated address and the one thing it cannot learn from
    inside its own network. A dialler knows it too, because the address it
    dialled is often one a punch found rather than one the peer could name.
    """
    payload = {"pub": public_key, "ts": int(timestamp), "sig": signature}
    if certificate is not None:
        payload["cert"] = certificate
    if seen is not None:
        payload["seen"] = [str(seen[0]), int(seen[1])]
    return encode_frame(KIND_HELLO, payload)


def msg_frame(envelope: bytes, signature: bytes) -> bytes:
    """One message and its author signature."""
    return encode_frame(KIND_MSG, {"env": envelope, "sig": signature})


def ack_frame(message_hash: bytes) -> bytes:
    """Acknowledge one envelope by its hash."""
    return encode_frame(KIND_ACK, {"hash": message_hash})


def req_frame(request_id: int, op: str, payload: dict) -> bytes:
    """A request on its own stream. Phase 4 carries file chunks in these."""
    return encode_frame(KIND_REQ, {"id": int(request_id), "op": op,
                                   "payload": payload})


def resp_frame(request_id: int, ok: bool, payload: dict) -> bytes:
    """The answer to one request, named by the same id."""
    return encode_frame(KIND_RESP, {"id": int(request_id), "ok": bool(ok),
                                    "payload": payload})


# --- frame readers ---


def read_observed(payload: dict) -> tuple[str, int] | None:
    """The address a HELLO says it saw us at, or None for anything else.

    Every field is bounded here: it is a claim by the peer on the other end,
    useful only as a candidate to try next time and never trusted for anything.
    """
    seen = payload.get("seen")
    if not isinstance(seen, (list, tuple)) or len(seen) != 2:
        return None
    host, port = seen
    if not isinstance(host, str) or not 1 <= len(host) <= MAX_HOST_CHARS:
        return None
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        return None
    return host, port


def read_msg(payload: dict) -> tuple[bytes, bytes]:
    """The envelope and signature out of a MSG frame."""
    envelope = payload.get("env")
    signature = payload.get("sig")
    if not isinstance(envelope, bytes) or not envelope:
        raise FrameError("MSG carries no envelope")
    if not isinstance(signature, bytes) or not signature:
        raise FrameError("MSG carries no signature")
    return envelope, signature


def read_ack(payload: dict) -> bytes:
    """The envelope hash out of an ACK frame."""
    message_hash = payload.get("hash")
    if not isinstance(message_hash, bytes) or \
            len(message_hash) != ENVELOPE_HASH_BYTES:
        raise FrameError("ACK does not name an envelope")
    return message_hash


def read_request(payload: dict) -> tuple[int, str, dict]:
    """The id, operation and body out of a REQ frame."""
    request_id = payload.get("id")
    op = payload.get("op")
    body = payload.get("payload")
    if not isinstance(request_id, int) or isinstance(request_id, bool):
        raise FrameError("REQ has no request id")
    if not isinstance(op, str) or not op:
        raise FrameError("REQ names no operation")
    if not isinstance(body, dict):
        raise FrameError("REQ carries no map")
    return request_id, op, body


def read_response(payload: dict) -> tuple[int, bool, dict]:
    """The id, outcome and body out of a RESP frame."""
    request_id = payload.get("id")
    ok = payload.get("ok")
    body = payload.get("payload")
    if not isinstance(request_id, int) or isinstance(request_id, bool):
        raise FrameError("RESP has no request id")
    if not isinstance(ok, bool):
        raise FrameError("RESP carries no outcome")
    if not isinstance(body, dict):
        raise FrameError("RESP carries no map")
    return request_id, ok, body
