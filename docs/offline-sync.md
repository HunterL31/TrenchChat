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
│ Mechanism 3: Set reconciliation                                  │
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

B → C   MT_SYNC_REQUEST {channel, what B holds}
C → B   MT_SYNC_RESPONSE {M, ...}               (Mechanism 2, hint used)

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

## Mechanism 3: reconciling what each side holds

**File**: `trenchchat/core/sync.py`, `SyncManager`; the set arithmetic is `trenchchat/core/sync_ranges.py`

When B reconnects (detected via `PeerAnnounceHandler`), or on startup, B sends `MT_SYNC_REQUEST` to all known peers for every subscribed channel:

```
Fields:
  F_MSG_TYPE          → "sync_request"
  F_CHANNEL_HASH      → channel hash bytes
  F_SYNC_WINDOW_START → where B would resume from, kept for peers that predate ranges
  F_SYNC_RANGES       → what B actually holds, as timestamp ranges
```

### Why a watermark is not enough

A watermark answers "since when". After a partition it is asked to answer "which", and it cannot: if A holds {1,3,4} and B holds {1,2,4,5}, each side's watermark already sits past the other's gap, so neither ever asks for 2, 3 or 5. That is the shape `sync11` in `docs/testenv-scenarios.md` reproduced with four peers, and the trust horizon, the scan cursor and the never-backwards watermark rule were all attempts to paper over it from the timestamp side.

Message ids are content hashes (`_compute_message_id`), so what the two sides actually disagree about is a *set of ids*. `F_SYNC_RANGES` describes that set:

- A range is `[lo, hi, mode, payload]`, covering rows with `lo <= timestamp < hi`. Ranges in one message are ascending and non-overlapping.
- `RANGE_FINGERPRINT` carries `[count, fp]`, where `fp` is 16 bytes of SHA-256 over the range's ids sorted and concatenated. Equal count and fingerprint means equal sets, and the range is not looked at again.
- `RANGE_IDLIST` carries every id in the range by its leading 8 bytes, ascending. Small enough to diff outright, which ends the exchange for that range.

### Two stages, because a re-check is almost always a no-op

Describing a set costs about ten bytes a row, and a routine re-check happens on every announce cooldown per (channel, peer) and almost always answers "nothing further". So a request has two shapes:

- **A fresh request summarises** (`sync_ranges.summarise`): the newest `SYNC_SUMMARY_LADDER[0]` (8) rows spelled out by id, then fingerprinted buckets that double in size with age (16, 32, 64, 128), then one fingerprint for everything older, so the whole window is covered in at most six ranges and a few hundred bytes whatever the channel holds. The resolution sits at the recent end because that is where a peer that was away has its gap, and the newest bucket goes by id rather than fingerprint because a responder that receives ids can send the missing rows in the same answer, where a fingerprint could only be described back. A peer holding no more than the newest bucket sends one id list, so a fresh join still gets rows in the first answer rather than spending a round trip proving it has none.
- **A narrowing request describes** (`sync_ranges.describe`): only once a peer has answered that the window differs is it worth saying where. `describe` splits the span into at most `SYNC_RANGE_FANOUT` (16) sub-ranges of roughly equal row count, spelling out any sub-range holding at most `SYNC_LEAF_IDS` (32) rows and fingerprinting the rest. Boundaries always land on a row timestamp and never inside a group of rows sharing one: a boundary is a bare float, so a split group could never be compared consistently by either side.

The trade is a few hundred bytes on every routine re-check, against a steady state that once cost five kilobytes and a recent gap that now closes in a single round trip: the first answer carries the rows. A gap older than the ladder reaches costs the extra round trip of describing the bucket it fell in. A single whole-window fingerprint (about 110 bytes) was measured as the alternative; it saves the bytes and spends the round trip on every difference, and the scenario numbers for both are in `docs/testenv-scenarios.md`.

Measured, as the whole packed field dict of one `MT_SYNC_REQUEST`, by how many signed rows the requester holds in the window:

| Rows held | Before | One fingerprint | Ladder (fresh request) | Narrowing step |
|---|---|---|---|---|
| 0 | 89 B | 89 B | 89 B | 89 B |
| 5 | 139 B | 108 B | 139 B | 139 B |
| 32 | 413 B | 108 B | 249 B | 413 B |
| 100 | 1385 B | 108 B | 290 B | 551 B |
| 500 | 5440 B | 110 B | 373 B | 510 B |
| 2000 | 712 B | 110 B | 374 B | 521 B |
| 10000 | 744 B | 110 B | 374 B | 532 B |

(A request with no ranges at all, which is what an older peer sends, is 64 B. The old hump at 100 to 500 rows is every one of 16 sub-ranges being spelled out; past that, sub-ranges grow beyond `SYNC_LEAF_IDS` and become fingerprints on their own.)

### The description budget

`SYNC_DESCRIPTION_BUDGET_BYTES` (512) caps what any one description costs packed. Id lists are what grow, so they are the first thing given up: the largest is summarised as a fingerprint, then the next, until the description fits; if every range is already a fingerprint and there are still too many, the same span is described again in fewer, coarser ranges. Narrowing gets slower, never wider, and coverage is never dropped: every row stays inside some range.

The budget clears one full leaf (32 ids pack to 344 bytes) on purpose. A leaf that could not be spelled out could never be resolved, and at 1 kbps 512 bytes is about four seconds of airtime, which is worth spending on a difference and not on a re-check.

One case is deliberately allowed past it: a range whose rows all share a single timestamp cannot be split, so a fingerprint there could never be narrowed and the two sides would disagree about that range forever. It is spelled out however long that makes it, up to `MAX_SYNC_LIST_IDS`, past which it is summarised and settles as a standing mismatch on one range rather than having the whole window refused. `MAX_SYNC_RANGES` and `MAX_SYNC_LIST_IDS` remain the hard inbound refusal bound; the budget is what a well-behaved sender spends.

A hash chain over the channel would be smaller still, and was rejected: a channel has many concurrent authors on a partitioning mesh, so its history is a DAG, and a chain needs somebody to sequence it. That is a center, which fails the first check in `.claude/rules/reticulum-zen.md`.

### The exchange

The responder compares each received range against its **serving view**: signed rows this requester's tenure entitles it to (`_filter_rows_by_tenure`). A row it will not relay is simply absent from every description it produces. What it asks for, by contrast, is measured against everything it holds, so a row it withholds is never requested back.

- **An id list** resolves outright. Rows the requester did not name are sent in `F_SYNC_MESSAGES`. Prefixes the requester named that the responder does not hold become `F_SYNC_NEED` triples `[lo, hi, prefix]` on the same response.
- **A fingerprint that matches** is skipped entirely.
- **A fingerprint that differs** is described back: the responder's own id list for that range if it is small enough, otherwise fingerprinted sub-ranges. The requester compares those against its own rows and sends a narrowed continuation request, which is the first message in the exchange to spell anything out. Traffic is proportional to the difference, not to the history.

Nothing is ever pushed unasked. A `F_SYNC_NEED` on a response is answered by a `MT_SYNC_RESPONSE` from the original requester, so the responder records a pending request of its own (`_record_pending_request`) before sending one; without that, the answer would arrive as unsolicited and be dropped. A need-only request is never classified as deep and is never throttled: it names rows outright, so it costs nothing a flood of them could turn into repeated full sweeps.

The worked example takes three round trips. A holds {1,3,4} and B holds {1,2,4,5}:

```
A → B  sync_request   ranges = fingerprint of the window
B → A  sync_response  no rows, ranges = id list [1,2,4,5]
A → B  sync_request   ranges = id list [1,3,4], need = prefixes of 2 and 5
B → A  sync_response  messages 2 and 5, need = prefix(3)
A → B  sync_response  message 3
```

The first two messages are the price of the cheap steady state: had A spelled out its three ids in the first request, this would have been two round trips and every no-op re-check would have cost the same. `tests/test_sync_reconcile.py::TestWorkedExample` pins the exact sequence.

### What the legacy sweep still does

A request carrying neither `F_SYNC_RANGES` nor `F_SYNC_NEED` gets the timestamp sweep exactly as before: `_collect_permitted_rows` from `min(window_start, trust_floor)`, capped at 50 rows, `F_SYNC_SCAN_CURSOR` when a truncated batch scanned past withheld rows, the `PEER_TRUST_HORIZON_SECS` widening for a peer never served before. That path is untouched, so a peer running an older build syncs with a new one exactly as it always did. Everything in the two paragraphs below applies to it and not to a reconciled request.

The sweep fills a batch with rows the requester may actually see, **scanning past withheld ones** (bounded by `MAX_SWEEP_SCAN`). Tenure filtering can otherwise empty a batch while the responder still holds newer history the requester is entitled to, stranding them at that timestamp. A batch that hits `MAX_SWEEP_SCAN` while every scanned row was withheld carries `F_SYNC_SCAN_CURSOR` (how far the sweep actually reached) so the requester's *next* request resumes from there instead of asking the same withheld run over again; that field never touches the persisted watermark, only the next request's `F_SYNC_WINDOW_START`.

Rows are grouped by timestamp as the sweep scans, and a batch or scan cursor never splits a group: `F_SYNC_WINDOW_START` and `F_SYNC_SCAN_CURSOR` are bare floats with no row-id tie-breaker, so several messages sharing the exact same timestamp (plausible on a coarse clock) must be included, or withheld and resumed from, as a whole, or whichever half landed on the wrong side of a split would be silently skipped by every future sweep (`Storage.get_messages_after` filters on strict `timestamp >`). A single group larger than `MAX_RESPONSE_MESSAGES` still ships whole rather than stalling forever. Internally, `_collect_permitted_rows` sweeps by a (timestamp, row id) cursor into `Storage.get_messages_after` so a group spanning an internal page boundary is scanned as one run.

### Hints, caps and bounds

Hints **supplement** the answer rather than replacing it. Letting a hint short-circuit it breaks two ways, both of which strand B silently while the empty response reports the channel as synced:

- Hints are broadcast to every reachable subscriber, so most holders of a hint never have the message it names. That lookup resolves to nothing, and if it stood as the whole answer B would get nothing from that peer until the hint aged out.
- A responder that answers only the hinted message never serves the newer history B also lacks, and the hint is never cleared on the responder side, so it would keep answering with the same one message on every future request.

On the sweep path a hinted message **newer than where the sweep reached** is held back for the sweep to reach in order, because B advances its watermark to the newest message in a response. A reconciled request has no such frontier: B asks by id, so a row served out of timestamp order strands nothing.

Every authorised request is answered, **including with an empty message list**. Silence is ambiguous ("nothing for you", "never received it", and "not allowed" all look identical), so a requester could never tell that it is actually up to date. Requests the responder refuses (unauthorised peer, a throttled deep request, or a malformed description) stay silent, so none of them leaks a signal.

The 50-message chunk limit keeps responses within LXMF message size constraints. A response that hits the cap carries `F_SYNC_TRUNCATED` and the requester immediately asks the same peer again, rebuilt from what it now holds. The chain is bounded by `MAX_SYNC_CONTINUATIONS` per (channel, peer), and only continues on actual progress: a row accepted, or a question that differs from the last one put to that peer (`_continue_reconcile` compares `sync_ranges.signature`). A responder that repeats one unmatchable range earns exactly one narrowing, not a request per claim.

Inbound descriptions are refused whole rather than in part, and the caps are what a well-formed description of one window can produce: at most `MAX_SYNC_RANGES` (32) ranges and `MAX_SYNC_LIST_IDS` (512, which is 16 sub-ranges of 32) prefixes per message, `MAX_SYNC_NEEDS` (50) need triples, exact prefix and fingerprint widths, ascending non-overlapping ranges, and sorted id lists. These are the refusal bound, not the budget: a peer may legitimately have built its description under different constants, so what is refused is only what no honest peer could produce. Acting on the half of a description that parsed would mean serving rows against a set the peer never actually described.

A request is classified **deep** by how far its ranges actually reach: older than `SYNC_WINDOW_SECS` plus a clock-skew allowance, so a peer whose clock trails ours is never throttled for asking about the window it believes it is in. A fresh deep ask is paced per (channel, peer) by `DEEP_SYNC_COOLDOWN_SECS` exactly as before, but its *narrowing steps* are not: a deep reconcile is no longer one message, and refusing the steps would strand the requester halfway through with nothing left to ask. An exchange already accepted runs to `DEEP_SYNC_BURST` messages inside that window (the requester's own continuation budget plus the ask that opened it) and is refused past that, so a flood still cannot force unbounded work. `sync_ranges.is_summary` is what tells the two apart: a fresh ask is exactly one range covering the window, a narrowing step is anything else.

The requester's watermarks (`last_sync_at`, `sync_progress`) still advance only over messages actually accepted, never past ones the responder withheld or it rejected itself, and never backwards. They no longer decide *what* is asked for, though, so a grant that arrives late, or a row that turns up behind them, is recoverable rather than lost.

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

Both the hint TTL and the span a request reconciles are bounded by a configurable sync window:

```python
SYNC_WINDOW_DAYS = 7   # in trenchchat/core/sync.py
SYNC_WINDOW_SECS = SYNC_WINDOW_DAYS * 86400
```

Requests never look back further than `now - SYNC_WINDOW_SECS`, preventing unbounded data exchange on long-offline clients. A fresh join, or a peer whose entitlement just changed, asks from `0.0` and reaches as far back as the responder is willing to serve, which is what the deep-sync cooldown paces.

The window is also the horizon of what reconciliation can find. A message older than it is reachable only through a hint naming it directly; once that hint is purged, it is genuinely gone (`tests/test_sync_hints.py`).

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
| `F_SYNC_SCAN_CURSOR` | `0x51` | `float` | `sync_response` (only on the sweep path, when truncated) |
| `F_SYNC_RANGES` | `0x52` | `bytes` (msgpack) | `sync_request`, `sync_response` |
| `F_SYNC_NEED` | `0x53` | `bytes` (msgpack) | `sync_request`, `sync_response` |

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
