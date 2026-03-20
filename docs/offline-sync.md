# TrenchChat — Offline Message Sync

TrenchChat delivers messages by sending an individual LXMF packet directly to each channel subscriber. If a subscriber is offline at delivery time, the Reticulum path to them is either unknown or the direct delivery attempt eventually times out — and without intervention the message is lost.

This document describes the three-layer mechanism that ensures an offline peer receives all missed messages when they reconnect.

---

## The Problem

```
A sends message M to channel with subscribers [B, C]
  → B is offline: path unknown, or DIRECT delivery times out
  → C receives M immediately
  → A has no built-in retry for B
  → B comes back online hours later: no mechanism to pull M
```

The root causes are:
1. No application-level retry once the path resolves
2. No mechanism for a reconnecting peer to pull messages they missed
3. No way for other peers to know what B is missing

---

## Three Complementary Mechanisms

Each mechanism covers a failure scenario the others cannot.

```
┌─────────────────────────────────────────────────────────────────┐
│ Mechanism 1: Pending retry                                       │
│   Covers: B is briefly offline; A is still online when B returns │
├─────────────────────────────────────────────────────────────────┤
│ Mechanism 2: Missed-delivery hints                               │
│   Covers: A went offline, but C/D received the hint while online │
├─────────────────────────────────────────────────────────────────┤
│ Mechanism 3: Timestamp-fallback sync                             │
│   Covers: No hints exist; any peer with the messages can respond │
└─────────────────────────────────────────────────────────────────┘
```

### Full sequence

```
A offline-sends M          B offline             C online
─────────────────────────────────────────────────────────────────
A → C   deliver M          (missed)
A → C   MT_MISSED_DELIVERY {B missed M}
                           C stores hint in missed_deliveries

                    [ time passes ]

                           B comes back online, announces
A ← B announce detected by PeerAnnounceHandler
A → B   flush_pending: retry M directly          (Mechanism 1)

B → C   MT_SYNC_REQUEST {channel, since_ts}
C → B   MT_SYNC_RESPONSE {M, ...}               (Mechanism 2 — hint used)

                           C clears hints for B
                           B updates last_sync_at
```

---

## Mechanism 1: Sender-side Pending Retry Queue

**File**: `trenchchat/core/messaging.py` — `Messaging`

When `send_message` cannot reach a subscriber (path unknown in the RNS routing table), instead of silently skipping that peer the message parameters are serialized and stored in an in-memory pending queue:

```python
self._pending.setdefault(dest_hex, []).append({
    "channel_hash_hex": ..., "content": ..., "timestamp": ...,
    "msg_id": ..., "reply_to": ..., "last_seen_id": ...,
})
```

`Messaging.flush_pending(peer_hex)` reconstructs and resends all queued messages the moment a peer's RNS path becomes resolvable. It is triggered by `SyncManager.on_peer_appeared()`.

For the case where the path was initially known but the LXMF DIRECT delivery times out, an LXMF failure callback is attached to every outbound message:

```python
lxm.register_failed_callback(
    lambda m: self._on_delivery_failed(dest_hex, channel_hash_hex, msg_id)
)
```

Both cases (path-unknown and delivery-timeout) then call `_notify_missed`, which invokes the missed-delivery callback registered by `SyncManager`.

**Limitation**: The queue is in-memory only. If A restarts before B comes back, queued messages are lost. Mechanisms 2 and 3 cover this.

---

## Mechanism 2: Missed-Delivery Hints

**File**: `trenchchat/core/sync.py` — `SyncManager._on_missed_delivery_event`  
**Storage**: `missed_deliveries` table in `trenchchat/core/storage.py`

When delivery to B fails, A broadcasts a `MT_MISSED_DELIVERY` control message to every currently-reachable subscriber:

```
Fields:
  F_MSG_TYPE      → "missed_delivery"
  F_CHANNEL_HASH  → channel hash bytes
  F_MISSED_FOR    → B's identity hash hex
  F_MISSED_MSG_ID → the message_id that was not delivered
```

Each online peer that receives this stores a row in the `missed_deliveries` table:

```sql
CREATE TABLE IF NOT EXISTS missed_deliveries (
    channel_hash   TEXT NOT NULL,
    recipient_hash TEXT NOT NULL,
    message_id     TEXT NOT NULL,
    recorded_at    REAL NOT NULL,
    PRIMARY KEY (channel_hash, recipient_hash, message_id)
);
```

When B later sends a `MT_SYNC_REQUEST`, any responding peer checks `missed_deliveries` for B's identity hash first. If hints exist, it fetches exactly those messages and sends them — no full-table diff required.

After B confirms receipt (via `MT_SYNC_RESPONSE` processing), the hints for B are cleared with `storage.clear_missed_deliveries(channel_hash, B_hash)`.

**Hint TTL**: Hints older than the sync window (default 7 days) are pruned at startup via `storage.purge_old_missed_deliveries(before_ts)`.

**Limitation**: Hints only reach peers who are online at the exact moment A detects failure. If all peers except A are offline, no hints are stored anywhere. Mechanism 3 covers this.

---

## Mechanism 3: Timestamp-Fallback Sync

**File**: `trenchchat/core/sync.py` — `SyncManager`

When B reconnects (detected via `PeerAnnounceHandler`), or on startup, B sends `MT_SYNC_REQUEST` to all known peers for every subscribed channel:

```
Fields:
  F_MSG_TYPE          → "sync_request"
  F_CHANNEL_HASH      → channel hash bytes
  F_SYNC_WINDOW_START → last_sync_at timestamp from subscriptions table
```

`last_sync_at` is updated whenever B successfully receives a sync response, so subsequent syncs only request the incremental gap.

Any online peer that is subscribed (or is a member of an invite-only channel) responds. The responder's logic:

1. **Check hints first**: `storage.get_missed_message_ids(channel_hash, B_hash)` — if non-empty, fetch exactly those messages (targeted; avoids a full sweep).
2. **Timestamp fallback**: if no hints exist, `storage.get_messages_after(channel_hash, window_start, limit=50)` — returns the 50 oldest messages since `window_start`.
3. **Send** as `MT_SYNC_RESPONSE` with the full message records packed via msgpack.

The 50-message chunk limit keeps responses within LXMF message size constraints. Subsequent sync cycles fill any remaining gaps.

On receiving `MT_SYNC_RESPONSE`, B inserts each message with `Storage.insert_message()`, which is idempotent — the `UNIQUE(message_id)` constraint silently discards duplicates. New messages fire the normal GUI message callbacks so the chat view updates live.

---

## Peer Reconnect Detection

**File**: `trenchchat/network/announce.py` — `PeerAnnounceHandler`

```python
class PeerAnnounceHandler:
    aspect_filter = "lxmf.delivery"

    def received_announce(self, destination_hash, announced_identity, app_data):
        self._callback(announced_identity.hash.hex())
```

Registered with `RNS.Transport.register_announce_handler(...)` at startup. Every time any peer broadcasts their LXMF delivery destination, `SyncManager.on_peer_appeared` fires, which:

1. Calls `messaging.flush_pending(peer_hex)` (Mechanism 1)
2. Checks whether the peer is a known member/subscriber of any shared channel
3. Sends a targeted `MT_SYNC_REQUEST` to that peer for each shared channel

---

## Sync Window

Both hint TTL and the timestamp-fallback query are bounded by a configurable sync window:

```python
SYNC_WINDOW_DAYS = 7   # in trenchchat/core/sync.py
SYNC_WINDOW_SECS = SYNC_WINDOW_DAYS * 86400
```

Requests never look back further than `now - SYNC_WINDOW_SECS`, preventing unbounded data exchange on long-offline clients.

---

## New LXMF Field Constants

Defined in `trenchchat/core/messaging.py`:

| Field | Key | Type | Used in |
|-------|-----|------|---------|
| `F_SYNC_WINDOW_START` | `0x07` | `float` | `sync_request` |
| `F_SYNC_MESSAGES` | `0x08` | `bytes` (msgpack) | `sync_response` |
| `F_MISSED_FOR` | `0x09` | `str` | `missed_delivery` |
| `F_MISSED_MSG_ID` | `0x0A` | `str` | `missed_delivery` |

---

## Access Control

- **Public channels**: sync requests are honored for any peer who is subscribed (`storage.is_subscribed()`).
- **Invite-only channels**: sync requests are only honored if the requester's identity hash is present in the local `members` table (`storage.is_member()`). This preserves the existing invite-only access model — a non-member cannot use the sync protocol to read channel history.
