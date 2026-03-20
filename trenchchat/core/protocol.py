"""
LXMF field key constants and message type strings for the TrenchChat protocol.

This module has no local imports so it can be safely imported by any layer
(core, network, gui) without creating circular dependencies.

Field key registry
------------------
0x01–0x0F  Common / messaging fields
0x10       Control: msg_type discriminator
0x11–0x1F  Invite fields
0x20–0x2F  Member-list fields
0x30–0x3F  Subscription fields
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
F_CHANNEL_ACCESS    = 0x25   # str   — access mode ("public" | "invite")
F_CHANNEL_CREATED_AT = 0x26  # float — Unix timestamp of channel creation

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
