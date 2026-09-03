---
description: LXMF field key registry, message type constants, and msgpack conventions for TrenchChat
globs: trenchchat/**/*.py
alwaysApply: false
---

# Protocol Constants

## Single source of truth

All LXMF field keys and message type strings live in `trenchchat/core/protocol.py`.
This module has **no local imports**, so it can be imported by any layer without
creating circular dependencies. **Do not redefine** constants that already exist there.
Import them instead:

```python
# ✅ CORRECT
from trenchchat.core.protocol import (
    F_CHANNEL_HASH, F_MSG_TYPE,
    F_SYNC_WINDOW_START, F_SYNC_MESSAGES,
    F_MISSED_FOR, F_MISSED_MSG_ID,
)

# ❌ WRONG — duplicate definition
F_MSG_TYPE = 0x10
F_CHANNEL_HASH = 0x01
```

## Field key registry

These keys are internal to the protocol envelope: outbound senders wrap the whole
dict with `pack_fields()` (msgpack inside LXMF's `FIELD_CUSTOM_TYPE`/`FIELD_CUSTOM_DATA`,
type `ENVELOPE_TYPE`), and the `Router` unwraps inbound messages once with
`unpack_fields()` before dispatch. They never appear as LXMF field keys on the wire,
so overlapping LXMF's own registry is harmless.

| Range | Owner | Examples |
|-------|-------|---------|
| `0x01–0x0F` | Common / messaging | `F_CHANNEL_HASH=0x01`, `F_DISPLAY_NAME=0x02`, `F_TIMESTAMP=0x03`, `F_MESSAGE_ID=0x04`, `F_REPLY_TO=0x05`, `F_LAST_SEEN_ID=0x06`, `F_SYNC_WINDOW_START=0x07`, `F_SYNC_MESSAGES=0x08`, `F_MISSED_FOR=0x09`, `F_MISSED_MSG_ID=0x0A` |
| `0x10` | Control | `F_MSG_TYPE=0x10` (present on all control messages, absent on chat messages) |
| `0x11–0x1F` | Invite | `F_INVITE_TOKEN=0x11`, `F_INVITEE_HASH=0x12`, `F_EXPIRY_TS=0x13`, `F_ADMIN_HASH=0x14` |
| `0x20–0x2F` | Member list | `F_MEMBER_LIST_DOC=0x21`, `F_CHANNEL_NAME=0x22`, `F_CHANNEL_DESC=0x23`, `F_CHANNEL_CREATOR=0x24`, `F_CHANNEL_ACCESS=0x25`, `F_CHANNEL_CREATED_AT=0x26`, `F_CHANNEL_PERMISSIONS=0x27`, `F_SCOPE_KIND=0x28` |
| `0x30–0x3F` | Subscription | `F_SUBSCRIBER_LIST=0x30` |
| `0x50–0x5F` | Sync status | `F_SYNC_TRUNCATED=0x50` |
| `0x60–0x6F` | Voice | `F_VOICE_STATE=0x60`, `F_VOICE_MUTED=0x61`, `F_VOICE_JOINED_AT=0x62`, `F_VOICE_CODEC=0x63` |
| `0x70–0x7F` | Message integrity | `F_AUTHOR_SIG=0x70`, `F_AUTHOR_KEYS=0x71` |
| `0x80–0x8F` | Friends / direct messages | `F_FRIEND_NOTE=0x80` |
| `0x90–0x9F` | File manifest | `F_FILE_NAME=0x90`, `F_FILE_SIZE=0x91`, `F_FILE_HASH=0x92`, `F_FILE_CHUNK_ROOT=0x93` |

When adding a new field:
1. Pick the next unused key in the appropriate range.
2. Add it to the field layout docstring at the top of `trenchchat/core/messaging.py`.
3. Define the constant in the module that owns that range.

## Message type strings

Control messages are identified by `fields[F_MSG_TYPE]`. Defined values:

| Constant | Value | Module |
|----------|-------|--------|
| `MT_SUBSCRIBE` | `"subscribe"` | `subscription.py` |
| `MT_UNSUBSCRIBE` | `"unsubscribe"` | `subscription.py` |
| `MT_SUBSCRIBER_LIST` | `"subscriber_list"` | `subscription.py` |
| `MT_INVITE` | `"invite"` | `invite.py` |
| `MT_JOIN_REQUEST` | `"join_request"` | `invite.py` |
| `MT_MEMBER_LIST_UPDATE` | `"member_list_update"` | `invite.py` |
| `MT_MISSED_DELIVERY` | `"missed_delivery"` | `sync.py` |
| `MT_SYNC_REQUEST` | `"sync_request"` | `sync.py` |
| `MT_SYNC_RESPONSE` | `"sync_response"` | `sync.py` |
| `MT_VOICE_JOIN` | `"voice_join"` | `voice.py` |
| `MT_VOICE_LEAVE` | `"voice_leave"` | `voice.py` |
| `MT_VOICE_STATE` | `"voice_state"` | `voice.py` |
| `MT_FRIEND_REQUEST` | `"friend_request"` | `friends.py` |
| `MT_FRIEND_ACCEPT` | `"friend_accept"` | `friends.py` |
| `MT_FRIEND_DECLINE` | `"friend_decline"` | `friends.py` |

Chat messages have **no** `F_MSG_TYPE` field. Handlers should check `F_MSG_TYPE in fields`
to distinguish control messages from chat messages.

A **shared file is a chat message too**: the four `0x90` fields are a manifest
(name, size, hash, chunk root), never bytes. The bytes are pulled from a holder
over the file plane's own request path, which is not LXMF at all and carries no
field keys. LXMF's own `FIELD_FILE_ATTACHMENTS`, which Sideband and MeshChat use,
is deliberately never set on a channel message: it is a push, so every member
would receive every byte whether they asked or not. It stays available as the
interop route if direct messages to other clients ever carry files.

A **direct message is a chat message**, not a control one: it carries no `F_MSG_TYPE`, and its
`F_CHANNEL_HASH` is a conversation address (`naming.dm_hash_for`) rather than a channel hash. That
keeps it out of the router's per-sender control throttle, where a limit would drop conversation.
Nothing on the wire marks it as a direct message, the receiver recomputes the address from the
authenticated sender, so the address itself is the proof.

## msgpack conventions

- Always pass `use_bin_type=True` when packing: `msgpack.packb(data, use_bin_type=True)`
- Always pass `raw=False` when unpacking unless you specifically need bytes dict keys:
  `msgpack.unpackb(blob, raw=False)`
- When unpacking a document that uses bytes keys (e.g. stored member list blobs), pass
  `raw=True` and access keys as `b"members"`, `b"admins"`, etc.
- Channel announce `app_data` is always unpacked with `raw=False`.
