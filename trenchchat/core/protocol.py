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
0x50–0x5F  Voice fields
0x60–0x6F  Relay fields
"""

# --- Common / messaging fields ---
F_CHANNEL_HASH      = 0x01   # bytes[16] — which channel
F_DISPLAY_NAME      = 0x02   # str       — sender display name
F_TIMESTAMP         = 0x03   # float     — sender wall-clock Unix epoch
F_MESSAGE_ID        = 0x04   # str       — hex SHA-256 of content+sender+timestamp
F_REPLY_TO          = 0x05   # str|None  — message_id of the message being replied to
F_LAST_SEEN_ID      = 0x06   # str|None  — message_id of the most recent msg sender had seen
F_SYNC_WINDOW_START = 0x07   # float     — unix timestamp: start of sync window (sync_request)
F_SYNC_MESSAGES     = 0x08   # bytes     — msgpack list[dict] of full message records (sync_response)
F_MISSED_FOR        = 0x09   # str       — identity hex of peer who missed a message
F_MISSED_MSG_ID     = 0x0A   # str       — message_id that was not delivered
F_AVATAR_DATA       = 0x0B   # bytes     — JPEG avatar payload (max 4 KB)
F_AVATAR_VERSION    = 0x0C   # int       — monotonic counter; receiver uses to detect stale updates
F_IMAGE_DATA        = 0x0D   # bytes     — JPEG image attachment payload (max 320 KB)
F_EMOJI_HASH        = 0x0E   # bytes[32] — SHA-256 of the emoji image data
F_EMOJI_DATA        = 0x0F   # bytes     — raw emoji image (PNG/GIF, max 64 KB)

# --- Reaction fields ---
F_REACTION_MSG_ID   = 0x40   # str  — message_id being reacted to
F_REACTION_REMOVE   = 0x41   # bool — True if this is a reaction removal
F_EMOJI_NAME        = 0x42   # str  — human-readable emoji name; sent with request and response
#                              so the receiver can store the emoji under the correct name

# --- Control discriminator ---
F_MSG_TYPE          = 0x10   # str — present on all control messages; absent on chat messages

# --- Invite fields ---
F_INVITE_TOKEN      = 0x11   # bytes — Ed25519 signature token
F_INVITEE_HASH      = 0x12   # bytes — identity hash of the invitee
F_EXPIRY_TS         = 0x13   # float — Unix timestamp when the token expires
F_ADMIN_HASH        = 0x14   # bytes — identity hash of the issuing admin

# --- Member-list fields ---
F_MEMBER_LIST_DOC   = 0x21   # bytes — msgpack-encoded member list document
F_CHANNEL_NAME      = 0x22   # str   — channel display name
F_CHANNEL_DESC      = 0x23   # str   — channel description
F_CHANNEL_CREATOR   = 0x24   # str   — creator identity hash hex
F_CHANNEL_ACCESS    = 0x25   # str   — (legacy) access mode ("public" | "invite")
F_CHANNEL_CREATED_AT = 0x26  # float — Unix timestamp of channel creation
F_CHANNEL_PERMISSIONS = 0x27 # str   — JSON permissions dict (replaces F_CHANNEL_ACCESS)
F_CHANNEL_TYPE        = 0x28 # str   — channel type ("text" | "voice")

# --- Subscription fields ---
F_SUBSCRIBER_LIST   = 0x30   # bytes — msgpack list of hex identity hashes

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

# --- Voice fields ---
F_VOICE_DEST_HASH    = 0x50   # bytes — RNS destination hash of the voice endpoint (owner or relay)
F_VOICE_PARTICIPANTS = 0x51   # bytes — msgpack list of participant identity hash hex strings
F_VOICE_CODEC_PROFILE = 0x52  # int   — LXST Opus profile constant
F_VOICE_SIGNAL       = 0x53   # int   — in-band signal type (see VS_* constants below)

# --- Relay fields ---
F_RELAY_TOKEN        = 0x60   # bytes — Ed25519 signature authorizing relay assignment
F_RELAY_DEST_HASH    = 0x61   # bytes — relay's LXMF delivery destination hash

# --- Voice message types ---
MT_VOICE_JOIN        = "voice_join"    # participant requests to join voice
MT_VOICE_LEAVE       = "voice_leave"   # participant announces departure
MT_VOICE_STATE       = "voice_state"   # host broadcasts current participant list + voice dest hash

# --- Relay message types ---
MT_RELAY_ASSIGN        = "relay_assign"         # owner assigns a channel to a relay
MT_RELAY_ACCEPT        = "relay_accept"          # relay confirms assignment, provides voice dest hash
MT_RELAY_REVOKE        = "relay_revoke"          # owner revokes relay assignment
MT_RELAY_MEMBER_UPDATE = "relay_member_update"  # owner pushes updated member list to relay

# --- In-band voice signal constants (sent over LXST SignallingReceiver, not LXMF) ---
VS_MUTE    = 0x01   # participant muted their mic
VS_UNMUTE  = 0x02   # participant unmuted their mic
VS_SPEAKING = 0x03  # VAD detected speech start
VS_SILENT  = 0x04   # VAD detected speech end
