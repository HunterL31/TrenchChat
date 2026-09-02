# TrenchChat: offline message sync

TrenchChat delivers messages by sending an individual LXMF packet directly to each channel subscriber. If a subscriber is offline at delivery time, the Reticulum path to them is either unknown or the direct delivery attempt eventually times out, and without intervention the message is lost.

This document describes the three-layer mechanism that ensures an offline peer receives all missed messages when they reconnect.

---

## What is lost when a subscriber is offline

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

## Three complementary mechanisms

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

## Mechanism 1: sender-side pending retry queue

**File**: `trenchchat/core/messaging.py`, `Messaging`

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

## Mechanism 2: missed-delivery hints

**File**: `trenchchat/core/sync.py`, `SyncManager._on_missed_delivery_event`  
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

When B later sends a `MT_SYNC_REQUEST`, any responding peer checks `missed_deliveries` for B's identity hash first. If hints exist, it fetches exactly those messages and sends them, no full-table diff required.

After B confirms receipt (via `MT_SYNC_RESPONSE` processing), the hints for B are cleared with `storage.clear_missed_deliveries(channel_hash, B_hash)`.

**Hint TTL**: Hints older than the sync window (default 7 days) are pruned at startup via `storage.purge_old_missed_deliveries(before_ts)`.

**Retry**: If a subscriber's identity can't be recalled at broadcast time, the hint isn't dropped, `SyncManager` requests the subscriber's path and queues the hint (capped at `MAX_QUEUED_HINTS_PER_PEER` per peer), mirroring `Messaging`'s pending-retry queue. `on_peer_appeared` flushes queued hints alongside `flush_pending`, so a hint aimed at a momentarily unreachable peer still reaches them once they're back.

**Limitation**: Hints only reach peers who are online at the exact moment A detects failure. If all peers except A are offline, no hints are stored anywhere. Mechanism 3 covers this.

---

## Mechanism 3: timestamp-fallback sync

**File**: `trenchchat/core/sync.py`, `SyncManager`

When B reconnects (detected via `PeerAnnounceHandler`), or on startup, B sends `MT_SYNC_REQUEST` to all known peers for every subscribed channel:

```
Fields:
  F_MSG_TYPE          → "sync_request"
  F_CHANNEL_HASH      → channel hash bytes
  F_SYNC_WINDOW_START → last_sync_at timestamp from subscriptions table
```

`last_sync_at` is updated whenever B successfully receives a sync response, so subsequent syncs only request the incremental gap.

Any online peer that is subscribed (or is a member of an invite-only channel) responds. The responder's logic:

1. **Resolve hints**: `storage.get_missed_message_ids(channel_hash, B_hash)`, then look those ids up directly (`storage.get_messages_by_ids`). These name exact messages B missed, including ones older than B's window.
2. **Sweep**: `_collect_permitted_rows(...)` from `window_start`, capped at 50.
3. **Merge and send** as `MT_SYNC_RESPONSE`, deduplicated by message id and ordered oldest first.

Hints **supplement** the sweep rather than replacing it. Letting a hint short-circuit the sweep breaks two ways, both of which strand B silently while the empty response reports the channel as synced:

- Hints are broadcast to every reachable subscriber, so most holders of a hint never have the message it names. That lookup resolves to nothing, and if it stood as the whole answer B would get nothing from that peer until the hint aged out.
- A responder that answers only the hinted message never serves the newer history B also lacks, and the hint is never cleared on the responder side, so it would keep answering with the same one message on every future request.

A hinted message **newer than where the sweep reached** is held back for the sweep to reach in order. B advances its watermark to the newest message in a response, so serving one out of band would strand everything between it and the sweep frontier.

Every authorised request is answered, **including with an empty message list**. Silence is ambiguous ("nothing for you", "never received it", and "not allowed" all look identical), so a requester could never tell that it is actually up to date. Requests the responder refuses (unauthorised peer, or a throttled deep sweep) stay silent, so neither leaks a signal.

The 50-message chunk limit keeps responses within LXMF message size constraints. A response that hits the cap carries `F_SYNC_TRUNCATED`, and the requester immediately asks the same peer for the next batch. Without that, everything past the cap waits for an unrelated announce to drive the next request. The chain is bounded by `MAX_SYNC_CONTINUATIONS` per (channel, peer) and only continues while the watermark actually advances, so a peer that flags every batch truncated can't induce unbounded requests.

The sweep fills a batch with rows the requester may actually see, **scanning past withheld ones** (`_collect_permitted_rows`, bounded by `MAX_SWEEP_SCAN`). Tenure filtering can otherwise empty a batch while the responder still holds newer history the requester is entitled to, stranding them at that timestamp. A batch that hits `MAX_SWEEP_SCAN` while every scanned row was withheld carries `F_SYNC_SCAN_CURSOR` (how far the sweep actually reached) so the requester's *next* request resumes from there instead of asking the same withheld run over again; that field never touches the persisted watermark (below), only the next request's `F_SYNC_WINDOW_START`.

Rows are grouped by timestamp as the sweep scans, and a batch or scan cursor never splits a group: `F_SYNC_WINDOW_START` and `F_SYNC_SCAN_CURSOR` are bare floats with no row-id tie-breaker, so several messages sharing the exact same timestamp (plausible on a coarse clock) must be included, or withheld and resumed from, as a whole, or whichever half landed on the wrong side of a split would be silently skipped by every future sweep (`Storage.get_messages_after` filters on strict `timestamp >`). A single group larger than `MAX_RESPONSE_MESSAGES` still ships whole rather than stalling forever. Internally, `_collect_permitted_rows` sweeps by an (timestamp, row id) cursor into `Storage.get_messages_after` so a group spanning an internal page boundary is scanned as one run.

The requester's watermark only ever advances over messages it actually accepted, never past ones the responder withheld or it rejected itself, and never backwards, since a hint can serve a message older than everything already held. A permission decision is not permanent: a role or `full_sync` grant still propagating would otherwise leave history withheld for good, since the watermark would already be past it. The cost is that the responder re-scans that withheld run on each request, which is bounded and indexed.

On receiving `MT_SYNC_RESPONSE`, B inserts each message with `Storage.insert_message()`, which is idempotent, the `UNIQUE(message_id)` constraint silently discards duplicates. New messages fire the normal GUI message callbacks so the chat view updates live.

---

## Sync status

**File**: `trenchchat/core/sync_status.py`, `SyncStatusTracker`, owned by `SyncManager` and exposed as `sync_mgr.status`

Sync is otherwise invisible: a freshly joined channel shows an empty pane while a backfill is already in flight, and the messages then appear looking exactly like live traffic. The tracker records what was asked of whom and what came back, so a frontend can show it. It has no network side effects, it only observes calls `SyncManager` already makes.

| State | Meaning |
|-------|---------|
| `SYNCING` | at least one request outstanding |
| `SYNCED` | a peer answered and reported nothing further |
| `INCOMPLETE` | a known gap: a truncated batch, rows we refused, or a hint naming us |
| `WAITING` | no answer to go on: every peer unreachable, or asked and silent |
| `UNKNOWN` | never attempted |

`SYNCED` requires a peer to have actually answered, a silent peer never counts as up to date, which is what the empty response above exists to make possible.

`INCOMPLETE` is a claim about history, not about peers, so it needs evidence that something is missing. A peer that never answered is not evidence: a member being offline is ordinary, and a responder can refuse silently; its deep-sync cooldown does. Silence leaves the channel `WAITING`, and it settles as soon as any answer arrives. Reporting silence as a gap marked every channel with an absent member `INCOMPLETE` a few minutes into every session.

A hint is evidence only until the message it names turns up. The sender's own retry queue usually delivers it directly, which is not a sync response, so the gap clears on the message arriving by any route, not only through sync.

`SYNCED` is scoped to peers we know about. A peer whose announce never reached us is never asked and can't be accounted for; on a partition-tolerant mesh there's no way to enumerate everyone who might hold history. `SYNCED` means "every peer we know about answered and had nothing more," not "no history exists anywhere." `get_status()`'s `answered_peers` count says how many peers back that claim.

---

## Peer reconnect detection

**File**: `trenchchat/network/announce.py`, `PeerAnnounceHandler`

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

Step 3 is paced by `ANNOUNCE_SYNC_COOLDOWN_SECS` (120s) per (channel, peer):
announces repeat on a heartbeat, not just on reconnect, and without the
cooldown every repeat announce cost a full request/response round trip per
shared channel. A peer that was actually away longer than the cooldown always
gets a fresh request the moment it reappears; within the cooldown the other
two mechanisms (pending retry, hints) still fire on every announce.

---

## Sync window

Both hint TTL and the timestamp-fallback query are bounded by a configurable sync window:

```python
SYNC_WINDOW_DAYS = 7   # in trenchchat/core/sync.py
SYNC_WINDOW_SECS = SYNC_WINDOW_DAYS * 86400
```

Requests never look back further than `now - SYNC_WINDOW_SECS`, preventing unbounded data exchange on long-offline clients.

---

## New LXMF field constants

Defined in `trenchchat/core/protocol.py`:

| Field | Key | Type | Used in |
|-------|-----|------|---------|
| `F_SYNC_WINDOW_START` | `0x07` | `float` | `sync_request` |
| `F_SYNC_MESSAGES` | `0x08` | `bytes` (msgpack) | `sync_response` |
| `F_MISSED_FOR` | `0x09` | `str` | `missed_delivery` |
| `F_MISSED_MSG_ID` | `0x0A` | `str` | `missed_delivery` |
| `F_SYNC_TRUNCATED` | `0x50` | `bool` | `sync_response` |
| `F_SYNC_SCAN_CURSOR` | `0x51` | `float` | `sync_response` (only when truncated) |

---

## Sync on channel join

**File**: `trenchchat/core/sync.py`, `SyncManager._on_channel_joined`

Auto-joining a channel via an accepted invite fires an additional sync trigger, wired to `InviteManager`'s `channel_joined` callback. Without this, a channel joined mid-session (as opposed to one already subscribed at the moment `request_sync_all()` runs, 3s after startup) would never sync at all until the next app restart or peer-reconnect announce. A fresh join has no `last_sync_at` yet, so it requests the full `SYNC_WINDOW_SECS` window, same as an unsynced channel's fallback in `request_sync_all()`.

---

## Access control

- **Public channels**: sync requests are honored for any peer who is subscribed (`storage.is_subscribed()`). No tenure tracking applies, membership there is a simple subscribe/unsubscribe flag, not a timestamped interval.
- **Invite-only channels**: access control is timestamp-based, not just membership-based, via the `membership_tenure` table (`channel_hash, identity_hash, joined_at, left_at`) and `storage.was_member_at(channel_hash, identity_hash, timestamp)`. Two independent checks apply to each candidate message in a sync response:
  1. **Sender tenure**: was the message's claimed author actually a member of the channel *at the message's timestamp*? Rejects messages from someone who has since been kicked, or whose claimed authorship predates them ever joining.
  2. **Requester tenure**: was the peer *asking* for sync actually a member at that timestamp? Off by default (see `full_sync` below); this is what stops a newly-invited member from using the sync protocol to backfill history from before they joined, the same way an invite-only channel's `members` table stops them from reading a live channel dump.

  Both checks are applied on both sides of a sync exchange: the responder filters before sending (`_handle_sync_request`), and the requester filters again on what it receives (`_handle_sync_response`), defense in depth against a single compromised or bugged peer skipping the check on its side.

  If a channel has zero rows in `membership_tenure` (an open-join channel, or one bootstrapped before tenure tracking existed), tenure checks are skipped entirely (`storage.has_any_tenure()`) rather than incorrectly rejecting everything.

### The `full_sync` permission

By default, a member of an invite-only channel can only sync/backfill messages sent since they actually joined; the requester-tenure check above is active. `full_sync` is a per-role permission, the same shape as `send_message`/`invite`/`kick`/`manage_roles`/`manage_channel` (`ALL_PERMISSIONS` in `trenchchat/core/permissions.py`), not a channel-wide switch: an admin grants it to whichever role(s) should be able to request the channel's *entire* history via sync, e.g. the admin role but not the member role. Checked with the same `has_permission(perms, role, FULL_SYNC)` used for every other permission, `_handle_sync_request` looks up the *requester's* role, `_handle_sync_response` looks up the local peer's own role. Granting it disables the requester-side tenure check for that role while the sender-side check still applies regardless.

Each member's true original join time (not just the timestamp of whichever member-list document version they first happened to receive) is carried in the signed member-list document itself (the `joined_at` field, covered by the same signature as the rest of the document; see `invite.py`'s `_build_document`/`_validate_document`). Without this, the first document version a peer processes would make everyone in it, including the channel owner, look like they joined "now," hiding all of their prior history regardless of how long the channel had actually existed.


---

## Direct messages use none of this

A conversation between two friends is deliberately outside all three mechanisms
above, because all three depend on somebody else being there. A conversation
has exactly two participants: there is no member to broadcast a missed-delivery
hint to, and no third peer who could answer a sync request for it.

So a conversation is not synced at all. It has no `subscriptions` row, which is
what keeps it out of `request_sync_all()`, the announce-driven per-channel loop
in `on_peer_appeared()`, and the missed-delivery path, every one of those
enumerates `get_subscriptions()`. Nothing had to be excluded by name.

What replaces them is an LXMF **propagation node**: a message to a friend who
is away is left with a node, which holds it until they collect it. That is a
different trade, not the same one, a node learns which two identities
corresponded and how much, where a channel's sync responder was already a
member. `docs/direct-messages.md` covers what the node sees, and what happens
on a mesh with no node at all.
