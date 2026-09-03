"""
LXMF field key constants and message type strings for the TrenchChat protocol.

This module has no local imports so it can be safely imported by any layer
(core, network, gui) without creating circular dependencies.

Field key registry
------------------
0x01–0x0F  Common / messaging / avatar / emoji fields
0x10       Control: msg_type discriminator
0x11–0x1F  Invite fields
0x20–0x2F  Member-list fields
0x30–0x3F  Subscription fields
0x40–0x4F  Reaction fields
0x50–0x5F  Sync status fields
0x60–0x6F  Voice fields
0x70–0x7F  Message integrity fields
0x80–0x8F  Friends / direct message fields

These numbers are TrenchChat's own and never appear as LXMF field keys on the
wire: LXMF reserves 0x00-0x80 for its standard registry (0x01 is
FIELD_EMBEDDED_LXMS there, 0x02 FIELD_TELEMETRY, and so on), so the whole
dict travels msgpack-packed inside LXMF's custom-payload fields instead
(pack_fields / unpack_fields below). To any client that is not TrenchChat a
channel or control message is one unknown envelope, not fourteen misparsed
fields, and upstream can allocate in its reserved range without breaking us.

A direct message uses the same custom-payload fields under its own envelope
type: it is the one message type that legitimately arrives at a client that
is not TrenchChat, so its text rides in the ordinary content, its attachment
in LXMF's standard image field, and only the TrenchChat extras in the
envelope (pack_dm_envelope below).

A mention is the one piece of TrenchChat's own structure that claims no key
here at all: it is written into the message text, so the wire carries no ping
to tamper with. See the mentions section below.
"""

import re

# --- Common / messaging fields ---
F_CHANNEL_HASH      = 0x01   # bytes[16]: which channel
F_DISPLAY_NAME      = 0x02   # str: sender display name
F_TIMESTAMP         = 0x03   # float: sender wall-clock Unix epoch
F_MESSAGE_ID        = 0x04   # bytes[32]: SHA-256 of content+sender+timestamp (hex text
#                              from older peers is still read)
F_REPLY_TO          = 0x05   # bytes[32]: message_id of the message being replied to, or None
F_LAST_SEEN_ID      = 0x06   # bytes[32]: message_id of the latest msg sender had seen, or None
F_SYNC_WINDOW_START = 0x07   # float: start of the sync window, unix timestamp (sync_request)
F_SYNC_MESSAGES     = 0x08   # bytes: msgpack list[dict] of full message records (sync_response)
F_MISSED_FOR        = 0x09   # str: identity hex of peer who missed a message
F_MISSED_MSG_ID     = 0x0A   # bytes[32]: message_id that was not delivered
F_AVATAR_DATA       = 0x0B   # bytes: JPEG avatar payload (max 4 KB)
F_AVATAR_VERSION    = 0x0C   # int: monotonic counter; receiver uses to detect stale updates
F_IMAGE_DATA        = 0x0D   # bytes: JPEG image attachment payload (max 320 KB)
F_EMOJI_HASH        = 0x0E   # bytes[32]: SHA-256 of the emoji image data
F_EMOJI_DATA        = 0x0F   # bytes: raw emoji image (PNG/GIF, max 64 KB)

# --- Reaction fields ---
F_REACTION_MSG_ID   = 0x40   # bytes[32]: message_id being reacted to
F_REACTION_REMOVE   = 0x41   # bool: True if this is a reaction removal
F_EMOJI_NAME        = 0x42   # str: human-readable emoji name; sent with request and response
#                              so the receiver can store the emoji under the correct name
F_REACTION_UNICODE  = 0x43   # str: reaction key for a standard unicode emoji. Mutually
#                              exclusive with F_EMOJI_HASH, which only ever carries a
#                              custom emoji's SHA-256.

# --- Control discriminator ---
F_MSG_TYPE          = 0x10   # str: present on all control messages; absent on chat messages

# --- Invite fields ---
F_INVITE_TOKEN      = 0x11   # bytes: Ed25519 signature token
F_INVITEE_HASH      = 0x12   # bytes: identity hash of the invitee
F_EXPIRY_TS         = 0x13   # float: Unix timestamp when the token expires
F_ADMIN_HASH        = 0x14   # bytes: identity hash of the issuing admin
F_INVITE_ISSUED_TS  = 0x15   # float: when the token was issued, bound into its signature

# --- Member-list fields ---
F_MEMBER_LIST_DOC   = 0x21   # bytes: msgpack-encoded member list document
F_CHANNEL_NAME      = 0x22   # str: channel display name
F_CHANNEL_DESC      = 0x23   # str: channel description
F_CHANNEL_CREATOR   = 0x24   # str: creator identity hash hex
F_CHANNEL_ACCESS    = 0x25   # str: (legacy) access mode ("public" | "invite")
F_CHANNEL_CREATED_AT = 0x26  # float: Unix timestamp of channel creation
F_CHANNEL_PERMISSIONS = 0x27 # str: JSON permissions dict (replaces F_CHANNEL_ACCESS)
F_SCOPE_KIND        = 0x28   # str: "server" when this control message targets a
                             #         server scope; absent/"channel" means a single
                             #         channel. The scope hash rides in F_CHANNEL_HASH.

# --- Subscription fields ---
F_SUBSCRIBER_LIST   = 0x30   # bytes: msgpack list of hex identity hashes
F_SUBSCRIBER_VERSION = 0x31  # int: monotonic counter per channel
F_SUBSCRIBER_SIG    = 0x32   # bytes: owner Ed25519 signature over the list

# --- Sync status fields ---
F_SYNC_TRUNCATED    = 0x50   # bool: responder capped this batch; it holds more history
F_SYNC_SCAN_CURSOR  = 0x51   # float: furthest timestamp the responder's sweep reached,
                             #         even if every row there was withheld from the
                             #         requester (sync_response, only set when truncated)

# --- Voice fields ---
F_VOICE_STATE       = 0x60   # str: "joined" | "left"
F_VOICE_MUTED       = 0x61   # bool: sender's current mute state
F_VOICE_JOINED_AT   = 0x62   # float: Unix timestamp when the sender joined the voice session
F_VOICE_CODEC       = 0x63   # str: codec the sender transmits ("opus")

# --- Message integrity fields ---
F_AUTHOR_SIG        = 0x70   # bytes[64]: author's Ed25519 signature over author_digest()
F_AUTHOR_KEYS       = 0x71   # dict: {author identity hex: public key} for a synced batch

# --- Friends / direct message fields ---
F_FRIEND_NOTE       = 0x80   # str: optional intro line on a friend request

# Longest intro line accepted on a friend request. Self-asserted text from an
# identity we have no relationship with yet, so it is capped on the way in.
MAX_FRIEND_NOTE_CHARS = 140


# --- Mentions ---
#
# A mention is not a field. It is the mentioned identity's hash written into
# the message text as "@<hex>", which is why it needs no protocol of its own:
# the author signature already covers the content, sync already relays it, and
# storage already holds it. Nothing on the wire describes a ping, so a relay
# cannot add one, drop one, or aim it at somebody else, and a client that has
# never heard of mentions still shows the message it was written into.
#
# The name is deliberately not carried alongside the hash. A display name is
# self-asserted and mutable (see author_digest below), so the reader renders a
# mention from the name it holds for that identity now; a rename never leaves a
# ping addressed to who the sender thought they were talking to.
#
# The obvious alternative, a field holding the list of mentioned hashes, costs
# fewer bytes and was rejected anyway: it sits outside the author signature, so
# a relay could add a ping to somebody else's words or take one out, and the
# reader would still have to find the mention in the text to draw it. Widening
# author_digest to cover such a field would fix the first half and break the
# digest for every peer already running. Writing the address where the words
# are costs a few bytes and needs none of that.
MENTION_PREFIX = "@"

# Identity hashes are RNS truncated hashes: 16 bytes, 32 hex characters.
MENTION_HEX_CHARS = 32

# Distinct identities read out of one message. A ping is one notification per
# message however many names it holds, so this bounds only the work a peer can
# ask a reader to do per message, not what the message may say.
MAX_MENTIONS_PER_MESSAGE = 32

# The trailing exclusion keeps a longer hex run from reading as a mention of
# whoever its first 32 characters happen to name.
_MENTION_RE = re.compile(
    r"@([0-9a-fA-F]{%d})(?![0-9a-fA-F])" % MENTION_HEX_CHARS
)


def mention_token(identity_hash_hex: str) -> str:
    """The text a mention of this identity is written as."""
    return MENTION_PREFIX + identity_hash_hex


def mentions_in(content: str) -> list[str]:
    """Every identity a message names, in the order written, without repeats."""
    if not content or MENTION_PREFIX not in content:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for match in _MENTION_RE.finditer(content):
        hex_hash = match.group(1).lower()
        if hex_hash in seen:
            continue
        seen.add(hex_hash)
        found.append(hex_hash)
        if len(found) >= MAX_MENTIONS_PER_MESSAGE:
            break
    return found


def mentions_identity(content: str, identity_hash_hex: str) -> bool:
    """Whether a message pings that identity.

    Deliberately not a substring test: a hash that is only the start of a
    longer hex run is not the identity that run names.
    """
    if not identity_hash_hex:
        return False
    return identity_hash_hex.lower() in mentions_in(content)


# --- Direct message envelope ---
#
# LXMF sets aside FIELD_CUSTOM_TYPE/FIELD_CUSTOM_DATA for exactly this: an
# application's own structure, carried without claiming a number that means
# something else to somebody. Imported rather than restated so the numbers
# cannot drift from the library's.
from LXMF import (  # noqa: E402
    FIELD_CUSTOM_DATA as LXMF_FIELD_CUSTOM_DATA,
    FIELD_CUSTOM_TYPE as LXMF_FIELD_CUSTOM_TYPE,
    FIELD_IMAGE as LXMF_FIELD_IMAGE,
)

# Names the envelope's shape, so a future change is a new version rather than a
# silent reinterpretation of the same bytes.
DM_ENVELOPE_TYPE = "trenchchat/dm/1"

# The image structure is not specified by the LXMF library. Sideband and
# NomadNet use [extension, bytes], so that is what is sent; anything readable
# is accepted on the way in (see inbound_image()).
DM_IMAGE_EXTENSION = "jpg"


MESSAGE_ID_BYTES = 32


def message_id_to_wire(message_id: str | None):
    """A message id as sent: its 32 digest bytes, or unchanged if it is not a digest."""
    if not message_id:
        return None
    try:
        raw = bytes.fromhex(message_id)
    except (ValueError, TypeError):
        return message_id
    return raw if len(raw) == MESSAGE_ID_BYTES else message_id


def message_id_from_wire(value) -> str:
    """A message id as stored: hex, whether it arrived as digest bytes or as text."""
    if isinstance(value, bytes):
        if len(value) == MESSAGE_ID_BYTES:
            return value.hex()
        return value.decode(errors="replace")
    return value if isinstance(value, str) else ""


_DM_ENVELOPE_ID_KEYS = ("message_id", "reply_to", "last_seen_id")


def pack_dm_envelope(*, message_id: str, timestamp: float, display_name: str,
                     reply_to: str | None, last_seen_id: str | None,
                     author_sig: bytes | None) -> bytes:
    """The TrenchChat-specific half of a direct message.

    Deliberately does not carry the conversation's address: the receiver
    derives that from the sender it authenticated, so putting it on the wire
    would add a claim to check rather than a fact to use.
    """
    return msgpack.packb({
        "message_id":   message_id_to_wire(message_id),
        "timestamp":    timestamp,
        "display_name": display_name,
        "reply_to":     message_id_to_wire(reply_to),
        "last_seen_id": message_id_to_wire(last_seen_id),
        "author_sig":   author_sig,
    }, use_bin_type=True)


def unpack_dm_envelope(fields: dict) -> dict | None:
    """Read a TrenchChat envelope out of an inbound message's fields.

    None when there is none, or it is not ours, or it does not parse -- all of
    which mean the same thing to the caller: treat this as a plain message from
    a client that is not TrenchChat.
    """
    if _envelope_type(fields) != DM_ENVELOPE_TYPE:
        return None
    payload = fields.get(LXMF_FIELD_CUSTOM_DATA)
    if not isinstance(payload, bytes):
        return None
    try:
        unpacked = unpack_wire(payload)
    except Exception:
        return None
    if not isinstance(unpacked, dict):
        return None
    for key in _DM_ENVELOPE_ID_KEYS:
        if unpacked.get(key) is not None:
            unpacked[key] = message_id_from_wire(unpacked[key])
    return unpacked


# --- protocol envelope ---
#
# Every channel and control message wraps its field dict in the same
# custom-payload fields, under its own type. The receiving Router unwraps it
# once, at the inbound choke point, so handlers only ever see the inner dict.
ENVELOPE_TYPE = "trenchchat/1"


def _envelope_type(fields: dict) -> str | None:
    value = fields.get(LXMF_FIELD_CUSTOM_TYPE) if fields else None
    if isinstance(value, bytes):
        value = value.decode(errors="replace")
    return value if isinstance(value, str) else None


def pack_fields(fields: dict) -> dict:
    """Wrap a TrenchChat field dict for the wire.

    The result is what an outbound LXMessage's fields must be set to: the
    registry keys above never appear as LXMF field keys themselves.
    """
    return {
        LXMF_FIELD_CUSTOM_TYPE: ENVELOPE_TYPE,
        LXMF_FIELD_CUSTOM_DATA: msgpack.packb(fields, use_bin_type=True),
    }


def is_protocol_envelope(fields: dict) -> bool:
    """True if these fields claim the channel/control envelope type."""
    return _envelope_type(fields) == ENVELOPE_TYPE


def unpack_fields(fields: dict) -> dict | None:
    """The TrenchChat field dict inside an inbound message's envelope.

    None when the message carries no envelope of ours or the payload does
    not parse; is_protocol_envelope() tells those two cases apart.
    """
    if not is_protocol_envelope(fields):
        return None
    payload = fields.get(LXMF_FIELD_CUSTOM_DATA)
    if not isinstance(payload, bytes):
        return None
    try:
        unpacked = unpack_wire(payload, int_keys=True)
    except Exception:
        return None
    return unpacked if isinstance(unpacked, dict) else None


def inbound_image(fields: dict):
    """The attachment bytes from a standard LXMF image field, or None.

    Accepts the [extension, bytes] pair other clients send, and a bare payload,
    since the structure is convention rather than specification.
    """
    if not fields:
        return None
    value = fields.get(LXMF_FIELD_IMAGE)
    if isinstance(value, (list, tuple)):
        value = next((part for part in value if isinstance(part, bytes)), None)
    return value if isinstance(value, bytes) and value else None


# --- Message type strings ---
MT_SUBSCRIBE        = "subscribe"
MT_UNSUBSCRIBE      = "unsubscribe"
MT_SUBSCRIBER_LIST  = "subscriber_list"
MT_INVITE           = "invite"
MT_JOIN_REQUEST     = "join_request"
MT_MEMBER_LIST_UPDATE = "member_list_update"
MT_MISSED_DELIVERY  = "missed_delivery"
MT_SYNC_REQUEST     = "sync_request"
MT_SYNC_RESPONSE    = "sync_response"
MT_AVATAR_UPDATE    = "avatar_update"
MT_REACTION         = "reaction"        # notify channel: reactor added/removed emoji on a message
MT_EMOJI_REQUEST    = "emoji_request"   # ask a peer for emoji image data by hash
MT_EMOJI_RESPONSE   = "emoji_response"  # respond with the emoji image bytes
MT_PRESENCE         = "presence"        # signed liveness beacon; empty content, no other fields
MT_GOODBYE          = "goodbye"         # graceful-shutdown notice; empty content, no other fields
MT_VOICE_JOIN       = "voice_join"      # sender entered the channel's voice session
MT_VOICE_LEAVE      = "voice_leave"     # sender left the channel's voice session
MT_VOICE_STATE      = "voice_state"     # periodic self-refresh, mute change, or reply to a join
MT_FRIEND_REQUEST   = "friend_request"  # ask a peer to add us to their friends list
MT_FRIEND_ACCEPT    = "friend_accept"   # peer accepted our request, or already had us
MT_FRIEND_DECLINE   = "friend_decline"  # peer refused our request


# --- sync window ---
# How far back sync requests, missed-delivery hints, and a published member
# list's departed-member tenure entries look. Shared so invite.py and sync.py
# stay bounded by the same horizon.
SYNC_WINDOW_DAYS = 7
SYNC_WINDOW_SECS = SYNC_WINDOW_DAYS * 86400


# --- bounded unpacking of wire payloads ---

import msgpack  # noqa: E402  (kept below the constants; still no local imports)

# Caps applied when unpacking anything that came off the network. msgpack
# >= 1.0 derives per-type limits from len(packed); these state them explicitly
# rather than relying on that default. unpackb() has no max_buffer_size, so
# the payload size is bounded separately.
MAX_WIRE_PAYLOAD  = 4 * 1024 * 1024
MAX_WIRE_ARRAY    = 4096
MAX_WIRE_MAP      = 4096
MAX_WIRE_STR      = 1 * 1024 * 1024
MAX_WIRE_BIN      = 2 * 1024 * 1024


import math  # noqa: E402
import time  # noqa: E402

# How far ahead of our own clock a peer's timestamp may sit before it stops
# being explainable as clock skew.
MAX_CLOCK_SKEW_SECS = 300.0


def wire_timestamp(value, now: float | None = None) -> float | None:
    """A peer-supplied timestamp, or None if it isn't plausible.

    F_TIMESTAMP is self-asserted and unverifiable. Unbounded, a far-future
    value pins a message to the top of the transcript permanently, and on the
    sync path it advances the requester's persisted watermark past history it
    never received -- after which that peer is never asked for anything older
    again. Callers decide the policy: substitute their own clock (direct
    delivery, where the message is still worth keeping) or drop the row
    (sync, where accepting it would move a watermark).
    """
    now = time.time() if now is None else now
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(ts) or ts < 0 or ts > now + MAX_CLOCK_SKEW_SECS:
        return None
    return ts


import hashlib  # noqa: E402
import struct  # noqa: E402

# Domain tag, so an author signature can never be replayed as one of the other
# structures the same Ed25519 key signs (invite tokens, member lists,
# subscriber lists).
AUTHOR_SIG_DOMAIN = b"trenchchat-author-v1"


def _length_prefixed(*parts: bytes) -> bytes:
    """Join byte strings so no field's contents can impersonate a boundary.

    content is arbitrary user text and may contain any byte, so a separator
    scheme would let a crafted message shift the field boundaries and produce
    a colliding digest.
    """
    return b"".join(struct.pack(">I", len(p)) + p for p in parts)


def author_digest(channel_hash_hex: str, message_id: str, timestamp: float,
                  content: str, reply_to: str | None,
                  last_seen_id: str | None,
                  image_data: bytes | None) -> bytes:
    """The bytes an author signs to bind a message to their identity.

    Covers everything a relay could otherwise alter while passing every other
    check: the text, the attachment, and the threading fields -- rewriting
    reply_to alone would graft real words onto a different conversation.
    message_id is covered too, which is what stops a tampered copy being
    stored under a genuine message's id.

    sender_name is deliberately absent: a display name is self-asserted and
    mutable, so signing it would freeze it at send time and fail on rename.
    The timestamp is formatted, not packed, so peers agree on it without
    depending on float encoding.
    """
    return hashlib.sha256(_length_prefixed(
        AUTHOR_SIG_DOMAIN,
        bytes.fromhex(channel_hash_hex),
        message_id.encode(),
        f"{timestamp:.6f}".encode(),
        (reply_to or "").encode(),
        (last_seen_id or "").encode(),
        hashlib.sha256(image_data or b"").digest(),
        content.encode("utf-8"),
    )).digest()


def unpack_wire(payload: bytes, *, raw: bool = False, int_keys: bool = False):
    """msgpack.unpackb with explicit limits, for data received from a peer.

    Use this for every payload that originated off the network; the plain
    msgpack.unpackb call is fine for blobs we wrote ourselves.
    int_keys permits integer map keys, which the protocol envelope's field
    dict uses. Raises ValueError if the payload itself is over the size cap.
    """
    if len(payload) > MAX_WIRE_PAYLOAD:
        raise ValueError(
            f"wire payload is {len(payload)} bytes, over the "
            f"{MAX_WIRE_PAYLOAD} limit"
        )
    return msgpack.unpackb(
        payload,
        raw=raw,
        strict_map_key=not int_keys,
        max_array_len=MAX_WIRE_ARRAY,
        max_map_len=MAX_WIRE_MAP,
        max_str_len=MAX_WIRE_STR,
        max_bin_len=MAX_WIRE_BIN,
    )
