# Test environment scenario matrix

Scripted multi-peer scenarios for `devtools/testenv/`, driven through the same
HTTP API (`api.py` → `actions.py`) the Flutter client calls. Each row is a
sequence of client actions and the state every peer must converge on.

This is the honest half of the test story. `tests/` uses the in-process
`TestTransport` shim: instant, synchronous, ordered delivery. Everything here
runs over real RNS Links between independent OS processes, so it covers what
the shim cannot — path resolution, delivery ordering, retry queues, truncated
sync chains, cold restarts, lossy links.

Scope for now is 4 peers. The orchestrator supports up to 8.

## The cast

Four testers with fixed roles, so a scenario ID implies who does what.

| Peer | Role | Typical use |
|---|---|---|
| **A** | Owner / creator | Creates channels and servers, holds `owner` |
| **B** | Admin | Promoted by A; second authority for invite/kick/roles |
| **C** | Member | Plain member; the peer permissions are tested against |
| **D** | Late joiner / outsider | Joins last, goes offline, or is never a member |

## Action vocabulary

Every atomic action a scenario can take. All map to one endpoint on that
tester's API (8801+) or one orchestrator call (8800).

| Group | Actions |
|---|---|
| **Identity** | set display name, set avatar, remove avatar, search directory |
| **Friends** | add friend (nickname/note), update friend, remove friend |
| **Channel** | create public, create invite-only, list discovered, join discovered, leave |
| **Server** | create server, create channel in server, invite to server, leave server |
| **Membership** | send invite, accept invite, decline invite, kick, promote to admin, demote |
| **Permissions** | edit channel perms (`send_message`, `invite`, `kick`, `manage_roles`, `manage_channel`, `create_channel`, `full_sync`), edit server perms |
| **Messaging** | send message, reply to message, send image, add reaction, remove reaction, import custom emoji |
| **Lifecycle** | go offline (link drop), go online, kill process, start process, restart, reset tester, kill/start hub |
| **Link** | set profile — the names `link_profiles.py` actually defines: `broadband`, `satellite`, `serial` (9600), `lora_fast` (SF7), `lora_long` (SF10), `packet_radio`, `lossy` (15% loss), `custom` (explicit bitrate/latency/jitter/loss) |

## Observable vocabulary

What assertions read. Everything is polled with a timeout — real links, no
instant delivery.

| Observable | Source |
|---|---|
| Message set | `GET /channels/{h}/messages` → set of `message_id` |
| Roster | `GET /channels/{h}/members` → `{identity_hash: role}` |
| Sync state | `GET /channels/{h}/sync_status` → `state`, `received_count`, per-peer state |
| My permissions | `GET /channels/{h}/my_permissions` |
| Discovered | `GET /channels/discovered` |
| Pending invites | `GET /invites` |
| Reactions | reaction summary on each message row |
| Presence | `GET /peers/{h}/presence` |
| Link | `GET /net/status`, orchestrator `GET /status` |

**Convergence** is the workhorse assertion: named peers hold identical message
ID sets, identical rosters with identical roles, and identical reaction counts
for a channel.

## Timing rules

Getting these wrong produces phantom failures.

- **The testenv announces far more often than the real app.** `worker.py` — what
  the orchestrator launches — runs the heartbeat at 10s; `main.py` re-announces
  every 60s. (`backend_core.start_heartbeat` defaults to 1.5s, which only
  `smoke_test.py` uses.) `PeerAnnounceHandler` fires `on_peer_appeared` on
  *every* announce, not just transitions, so anything piggybacking on a peer
  announce — pending flush, sync request — happens ~6× faster here than in
  production. Any scenario whose result depends on that trigger must record
  time-to-converge, not just convergence, and be read against a 60s worst case.
- **Warm up before inviting.** `invite.py`'s `_send_raw` has no retry queue. If
  the path isn't resolved when the invite is sent, it is dropped silently. The
  harness resolves paths first (`Backend.warm_up`) before any invite step.
- **Settle after a membership change.** A member-list update and a chat message
  sent right after are two independent LXMF sends with no ordering guarantee,
  and `messaging.py` drops a chat message if the receiver isn't marked
  subscribed/member yet. Scenarios wait for the roster to converge before
  sending.
- **On a public channel, joining is not the same as being registered.**
  `join_public_channel` sets the joiner's own state and sends `MT_SUBSCRIBE`;
  the owner only adds them on receipt, and other subscribers only learn of them
  from the owner's next broadcast. Three distinct moments, in order: the joiner
  is subscribed → the owner has them in `get_subscribers` → every subscriber
  does. A send addressed before the relevant one has passed goes to a set the
  target isn't in, and no retry fixes it because the message was never
  addressed to them. `_join_all(..., owner)` waits for the second;
  `subscribers_converged()` waits for the third.
- **A sync backfill is a chain, not an exchange.** Wait for sync state to leave
  `syncing` rather than sampling after a fixed sleep.
- **Shaping a link can fail silently, so read it back.** An unknown profile name
  answers 400 and leaves the link at broadband. Scenarios once passed `"flaky"`
  and `"serial9600"` — neither exists — and three degraded-link scenarios ran
  unshaped while reporting they had exercised a bad radio. `set_link_profile()`
  now raises on rejection *and* reads `link_summary` back from the orchestrator,
  and every scenario reports the shaping it actually ran under.

## Matrix

⚠ marks a row probing a suspected gap — the expected result is what the code
currently implies, and the scenario exists to confirm it.

### `public` — Public (open-join) channels

| ID | Peers | Actions | Expected result |
|---|---|---|---|
| public1 | A,B,C,D | A creates public channel | B, C, D each list it under discovered, none subscribed |
| public2 | A,B | A creates; B joins | A's subscriber set = {B}; B receives the signed subscriber list; B's roster view includes A |
| public3 | A,B,C,D | A creates; B, C join; A sends 3 | B and C hold all 3; D holds none |
| public4 | A,B,C,D | B (subscriber, not owner) sends 1 | A and C hold it; D does not — recipients are the subscriber set plus self |
| public5 | A,B,C,D | A creates; B, C join; A sends 5; **then** D joins | ⚠ **Confirmed.** D holds 0 at the instant it joins — public join calls `subscription_mgr.subscribe()` only, no `channel_joined` callback, so nothing requests sync. Backfill lands on A's next peer announce: measured 1.0s and 9.1s on two runs, tracking the 10s heartbeat phase. Scales to a 60s worst case in the real app |
| public6 | A,B,C,D | A6a: A creates public, grants `full_sync` to member, sends 5, D joins. A6b: identical without `full_sync` | ⚠ **Confirmed.** Both channels backfilled all 5 to D, with and without the grant. Public channels never open tenure, so `has_any_tenure` is false and tenure filtering — the only thing `full_sync` gates — never engages |
| public7 | A,B,C | B leaves; A sends 2 | A removes B from subscribers; C holds both; B holds neither |
| public8 | A,B,C,D | All 4 joined and the subscriber set has converged; each sends 2 in turn | All four converge on 9 messages (a seed plus 8). Roster settle measured at 0.5–4.0s |
| public9 | A,B,C,D | A (owner) leaves its own channel, then C sends | C's message still reaches B and D — the subscriber lists they already hold are unaffected by the owner leaving. The departed owner does not receive it and stays unsubscribed |
| public10 | A,B,C,D | B, C join; C goes offline; D joins (C misses the broadcast); C returns | C learns about D and its next send reaches D. Recovery measured at 0.5s, 1.0s and 18.1s across runs — LXMF's own retry backoff, not an application-level repair |

### `invite` — Invite-only channels and membership

| ID | Peers | Actions | Expected result |
|---|---|---|---|
| invite1 | A,B,C,D | A creates invite-only | Nobody sees it in discovered — `announce_channel` refuses invite-only regardless of the discoverable flag |
| invite2 | A,B | A invites B; B accepts | B's pending invite clears; member-list doc lands; A and B rosters identical (2 members, owner+member); B's tenure opens |
| invite3 | A,B | A sends 3; **then** invites B; B accepts. B3a: member role lacks `full_sync`. B3b: A grants `full_sync` to member first | B3a: B holds 0 backlog — tenure filtering drops rows from before B's join. B3b: B holds all 3. This is the real `full_sync` test (public channels can't show it — see public6) |
| invite4 | A,B,C,D | A invites B, C, D; all accept | All four rosters identical: 4 members, A owner, rest member |
| invite5 | A,B,C | A invites C; C declines | C is not a member; A, B rosters unchanged; nothing sent on decline |
| invite6 | A,B,C,D | All 4 members; A kicks C | B, D, A rosters drop to 3; C's local membership clears and its pending outbound for the channel is cancelled; a message C sends after is dropped by A, B, D |
| invite7 | A,B,C,D | A promotes B to admin; B invites D; D accepts | All four rosters identical, B `admin`, D `member` |
| invite8 | A,B,D | A demotes B to member; B invites D | Join request rejected — `_handle_join_request` checks INVITE against B's current role; D never joins |
| invite9 | A,B,C | A revokes `send_message` from member role | C's message is dropped by A and B (and by C's own outbound guard); B, still admin, sends fine |
| invite10 | A,C | C calls `/roles` with `remove_members=[A]` | ✅ `{"ok": false}`, no document published, rosters unchanged on every peer — adversarial path, GUI bypassed |
| invite11 | A,B,C | A grants `kick` to member; C kicks B | ❌ **Fails, and the failure is the finding.** The grant passes every local check and `/roles` reports success, but no peer ever applies it. See below |
| invite12 | A,B,C,D | A promotes B; A and B both publish a roster change within ~1s | Both documents validate against stored state; final rosters identical on all four; no split-brain |
| invite13 | A,B,C | C (member) attempts `/channels/{h}/permissions` | `{"ok": false}` — lacks `manage_channel`; stored perms unchanged everywhere |

#### invite11: a grantable permission that cannot take effect

`kick` and `manage_roles` are grantable to any role — `ALL_PERMISSIONS` offers
them, the permissions dialog exposes them, `has_permission` honours them, and
`update_membership` lets the change through. So a member granted `kick` gets a
successful `/roles` call and a published member-list document.

Every recipient then discards it. `_validate_document` builds
`trusted_signers` from the stored document's `admins | owners`, so a plain
member is not a recognised signer no matter what permissions they hold. The
kick takes effect on the actor's own device and nowhere else.

Two layers disagree about what a permission means: the permission system treats
`kick` as role-independent, the document layer ties signing authority to
admin/owner. Both behaviours are defensible alone — signing authority *should*
be narrow — but together they advertise a grant that silently does nothing.

Worth noting what this does to **invite10**, which asserts a member *cannot* kick the
owner. invite10 passes — but while invite11 fails, it passes for the wrong reason: no
member can effectively kick anyone. invite11 is what gives invite10 its meaning, which is
why the pair is worth keeping together.

Not fixed here: the resolution is a product decision (stop offering these
permissions below admin, or admit permission-holders as trusted signers), not a
bug with one obvious correction.

### `sync` — Offline behavior and sync

The reason this environment exists. All three sync mechanisms only run on a
degraded or interrupted link.

| ID | Peers | Actions | Expected result |
|---|---|---|---|
Built: sync1–sync5, sync7–sync11. C6 (deep-sync cooldown) and C12 (a 7-day-old window)
need control of the clock and stay deferred.

| ID | Peers | Actions | Expected result |
|---|---|---|---|
| sync1 | A,B,C | B goes offline (link drop); A sends 3; B goes online | ✅ B receives all 3, in 1.0–5.0s |
| sync2 | A,B,C,D | B offline; A sends 3; A offline; B online (only C, D reachable) | ❌ **Fails ~half of runs**, root cause established. B holds 0/3 with both responders at `pending` for the full timeout. See below |
| sync3 | A,B,C | Same as sync1 but B is **killed and restarted** instead of link-dropped | ✅ B ends with its own history plus what it missed, in 3.5s, via the cold path |
| sync4 | A,D | D offline; A sends 60 (> `MAX_RESPONSE_MESSAGES` = 50); D online | ✅ D ends with all 60 and `state == synced`, in 18.1s — the truncated batch does chain its follow-up |
| sync5 | A,B,C | B offline for messages 1–5; B online, C offline for 6–10; C online | ✅ Both end with all 10, in 10.6s — per-(channel, peer) watermarks hold up |
| sync7 | A,B | B offline across a batch, then back; watch the sync state | ✅ Settles on `synced` with every message present |
| sync10 | A,B,C,D | Hub killed (total partition); each peer sends 1; hub restarted | ✅ All four reconcile in 12.1s once the hub returns |
| sync8 | A,D | D joins without `full_sync`; A grants `full_sync` to member | ✅ The backlog arrives 3.0s after the grant, without D restarting |
| sync9 | A,B,C | A kicks C; C requests sync | ✅ C's transcript stays frozen at the kick |
| sync11 | A,B,C,D | All 4 offline simultaneously, each sends 2 locally, all come online | ❌ **Still fails.** One watermark defect found and fixed (below); the scenario itself reconciles only sometimes. 1 pass in 5 runs |
| C12 | A,B | B offline past `SYNC_WINDOW_SECS` (7 days, clock-shifted); B online | Deferred — needs clock control |

#### One defect inside sync11, root-caused and fixed: one watermark row, two directions

**The defect is fixed; sync11 is not.** A single 31.7s pass was recorded when this
landed and read as a fix. It was not: re-running sync11 five times — twice at the
commit that recorded the pass, three times after merging `main` — fails four
times out of five, always on the same `-alone-0` rows. The watermark collision
below is real, is fixed, and is pinned by a pytest regression test; it was one
cause among more than one. See "What still fails in sync11" below.

The `sync_progress` table was being written from both directions
against the same `(channel_hash, peer_hash)` key:

- `_handle_sync_response` advances `(channel, responder)` to the newest
  timestamp we **received** from that peer.
- `_handle_sync_request` advances `(channel, requester)` to how far we have
  **served** that peer.

For a pair that only ever consumes or only ever serves, the two never meet. For
a pair that does both — which is every peer in a four-way partition recovery —
they collide, and the more recent write wins.

The damage lands in the responder's floor:

```python
own_progress = get_peer_sync_progress(channel, requester_hex)
trust_floor  = max(own_progress, window_start - PEER_TRUST_HORIZON_SECS, 0.0)
sweep_start  = min(window_start, trust_floor)
```

That widening exists so a responder still serves history older than the
requester's claimed watermark — history the requester cannot know to ask for.
But when `own_progress` is polluted by the *receive* direction it reads as
recent, `trust_floor` rises to meet `window_start`, and `sweep_start` collapses
back to a strict sweep. Combined with `get_messages_after`'s strict `>`, any
message the responder acquired after the requester's watermark was set, but
whose timestamp predates it, is invisible forever.

That is exactly the `-alone-0` signature: the older message of each pair sits
behind a watermark that a later, newer message had already pushed past.

The fix separates the directions into a new `sync_served` table, restoring the
original intent of the floor. `_handle_sync_request` now reads and writes served
progress; `sync_progress` keeps its single receive-direction meaning.

Covered by `TestResponderAcquiresOlderHistoryLater` in
`tests/test_sync_multipeer.py`, which reproduces it in 0.3s in-process — the
scenario found it, but the pytest suite is where it is pinned.

#### What still fails in sync11

The measured runs, all of the same scenario body:

| Commit | Runs | Result |
|---|---|---|
| `b18ca9b` (pre-merge, with the `sync_served` fix) | 1 | Passed, 31.7s — the run that was mistaken for a fix |
| `b18ca9b` | 2 | Both failed, 220s and 228s |
| Post-merge with `main` | 2 | Both failed, 269s and 272s, byte-identical missing sets |

Missing rows are always `-alone-0` and never `-alone-1`, but *which* peers lose
which rows moves between runs — in one pre-merge run A and B converged fully
and only C and D lost a row. A deterministic defect would not vary that way.

The debug capture points at the deep-sync cooldown as at least part of what
remains. Nine deep requests were refused in a single run:

```
TrenchChat [sync]: deep sync request from 0e5d8023e94b… for e0676b7fd873… throttled — cooldown active
```

A peer returning from a partition has no `last_sync_at`, so it asks from 0,
which classifies as deep. `_deep_sync_allowed` serves the first such request per
`(channel, requester)` and then refuses for `DEEP_SYNC_COOLDOWN_SECS` (60) —
silently, with no response at all, so the requester cannot tell refusal from
loss (the same gap as finding 4). A four-way reconcile needs several rounds,
because a peer can only relay what it has itself already received, and the
cooldown allows one round per pair per minute. Whether the scenario converges
then depends on whether the useful round happens to fall inside a window that
is open — which matches the variance measured above.

One candidate is already ruled out: author-signature rejection is not involved.
`messages_rejected` reads 0 for every peer in every observed run, and the debug
capture holds no "signature missing or invalid" line — which integrity1 independently
confirms, since relayed history from a *dead* author verifies fine.

Not yet proven, and the next step: instrument one responder to log every
refusal alongside what it *would* have served, and confirm the refused rows are
the missing ones.

#### sync11's failure shape: the first message of a partition is lost

Seven of the eight built rows pass, several on the first attempt. sync11 does not,
and the shape of its failure is specific enough to be worth acting on:

```
A missing: B-alone-0, C-alone-0, D-alone-0
B missing:            C-alone-0, D-alone-0
C missing: A-alone-0,            D-alone-0
D missing: A-alone-0, B-alone-0, C-alone-0
```

Each peer sent two messages while every peer was offline. The second of each
pair propagated to everyone; the first reached nobody. Reproduced across three
runs, and still incomplete after 420s, so it is not slow convergence.

Everything around it works: sync1 shows pending-retry flushing, sync2 shows a third
party serving what the sender never delivered, sync5 shows disjoint histories
reconciling, sync10 shows a full partition healing once the hub returns. The
difference in sync11 is that *every* peer is both a requester and the sole source
of its own history at the same moment. Not yet root-caused; the sync watermark
is the obvious first place to look, since "first message skipped, later ones
served" is what an over-advanced watermark would produce.

Left strict rather than reclassified as a probe. The expectation is the sync
design's own — any online member can serve any gap — so `--family sync` exits
non-zero until it is fixed, which is the correct signal for an open bug.

#### sync2, after two fixes: 2/5 to 4/5

The original root cause — nothing tells a node its *own* link came back — was
real but not the whole story. `LinkWatcher` supplies that trigger now, and
alone it moved sync2 from failing most runs to **2 of 5 passing**. The failures
that remained were more informative than the originals: the sync status showed
all three peers at `pending` with a real `requested_at`, so the returning node
*was* asking. Nobody was answering.

`sync.py`'s `_send_raw` was the reason. A responder can read a request from a
peer whose path it cannot yet resolve — the request arrived, after all — but
addressing a reply needs `Identity.recall()`, and on failure it returned False
and dropped the answer. Alone among the send paths it did not even call
`request_path()`. Holding the answer and re-sending it on the peer's announce
took sync2 to **4 of 5**.

It still fails sometimes. Both fixes are in and the remaining failure has the
same outward shape, so the next step is to log, on the responder, every request
it answers and every one it holds — and find out which of the two the missing
answer was.

#### sync2, as originally root-caused: a recovered link has no resync trigger

**Fixed, but it was only half of it — see above.** sync2 failed roughly half the time — 2 of 6 runs — with B holding 0 of 3
and both responders sitting at `pending`, `answered_peers: 0`, for the full
206s.

My first two hypotheses were both wrong, and worth recording as such: it is
not the deep-sync cooldown (`_deep_sync_allowed` only records the timestamp
when it *allows*, so refusals do not extend it), and not `_peer_may_participate`
(open-join returns True unconditionally). Reading the code could not settle it;
capturing the testers' debug output could, which is what `--tester-log` is for.

The logs showed it plainly. In a failing run every sync request stops at
22:54:50, and the scenario ran to 22:58:17 — three and a half minutes of total
silence — while presence beacons kept flowing in both directions the whole
time. The network was fine. Nothing was being refused. B simply stopped asking.

`SyncManager.on_peer_appeared` was the only resync trigger after startup, and it
fires solely from `PeerAnnounceHandler` — a *received announce*. RNS suppresses
announce replays for a destination the transport has already propagated, so a
peer whose link drops and recovers can go on exchanging traffic for the rest of
the session without ever seeing a fresh announce from its peers. It then never
asks for what it missed. `request_sync_all()` covers this only at process
start, which is why sync3 (kill and restart) always passed while sync2 (drop and
recover) did not.

**The fix is not in yet, and one attempt has already been ruled out.** Making a
presence transition to online a second trigger looked right — presence is
maintained from inbound LXMF traffic, so it keeps working exactly where
announces do not. It does not fix this: presence transitions fire when a
*remote peer* returns, and in sync2 it is the local node whose link recovered.
Its peers never went offline in its own presence table, so no transition
occurs. Measured 4/8 after the change against 4/6 before — no improvement — and
the change was reverted rather than left in as a speculative core edit.

What the fix actually needs is a trigger on *our own* link recovering, calling
`request_sync_all()` the way process startup does 3s in. The production client
has no such signal today: `main.py` polls interfaces for display, and nothing
tells `SyncManager` the node is reachable again. Adding one to the harness
alone would hide the gap rather than close it, since the real client has the
same hole.

One reporting gap this exposed is worth noting separately: `sync.py` refuses
requests silently by design, so from the requester's side "refused", "lost" and
"still in flight" are indistinguishable — all three read as `pending` forever.
`SyncStatusTracker` cannot currently tell a user which of those is happening.

### `links` — Degraded links

| ID | Peers | Actions | Expected result |
|---|---|---|---|
| links1 | A,B,C,D | A on `lossy` (62.5 kbps, 250±150ms, 15% loss); A sends 10 | ✅ All three peers converge in **101.7s** |
| links2 | A,B | B on `serial` (9.6 kbps, lossless); A sends 5 | ✅ Delivered in 3.0s. Slow-but-lossless is not a stall |
| links3 | A,B,C,D | A broadband, B `satellite`, C `lora_fast`, D `serial`; each sends 1 | ✅ Converged in 4.5s across four differently-shaped links |
| links4 | A,B | B on `lossy`, dropped offline mid-burst | ✅ Caught up all 10 in 28.2s |
| links5 | A,B,C,D | All four on `lora_fast` (5.5 kbps) | ✅ Converged in 12.6s |
| links6 | A,B,C | B on `lossy`; A sends 15 — **the link never drops** | ✅ Converged in 19.6s. The README's stated reason for the lossy profile: retry and hints reached the way a bad radio does, not by killing a link |
| links7 | A,B | B on `packet_radio` (AX.25 1200 baud, 5% loss) | ✅ Three messages in 10.1s. The worst link the app claims to support for text |
| links8 | A,B | B on `lora_long` (SF10, 1.0 kbps) | ✅ Three messages in 11.1s |
| links9 | A,B | `custom` profile with explicit bitrate/latency/jitter/loss | ✅ Applied exactly as asked (32 kbps · 120±20ms · 8% loss); converged in 2.0s |
| links10 | A,B | Retune B broadband → `serial` mid-run | ✅ Shaping applies live: 0.5s then 1.0s, no restart needed |

### `servers` — Servers

| ID | Peers | Actions | Expected result |
|---|---|---|---|
| servers1 | A,B | A creates a server with 3 channels; invites B once | ✅ One invite admits B to the server and all 3 channels; a send on the *third* proves it |
| servers2 | A,B,C | A grants `create_channel` to admin, promotes B; B creates a channel | ✅ Every member receives it via the re-published server document |
| servers3 | A,B | A edits server permissions | ✅ Mirrored into every child channel; a per-channel override returns 409 and the mirror survives |
| servers4 | A,B,C | B leaves the server | ✅ B unsubscribed from every channel in it; A and C unaffected |
| servers5 | A,B,C | A kicks C from the server | ✅ C loses every channel at once and its later send is rejected |
| servers6 | A,B | Server-level `full_sync` grant, then invite B with backlog in 2 channels | ✅ Both channels backfilled (1.0s, 0.0s) — server-scoped tenure resolves per channel |

### `social` — Reactions, emoji, presence, identity

| ID | Peers | Actions | Expected result |
|---|---|---|---|
| social1 | A,B,C,D | Public channel, all joined; B reacts to A's message | ✅ A, C and D all show count 1, owner included |
| social2 | A,B,C | B reacts, then removes the reaction | ✅ Clears on every peer |
| social3 | A,B,D | D offline; B reacts to A's message; D online | ⚠️ **Prediction refuted.** D *does* recover the reaction, in 14.1s and 15.2s across runs — LXMF's own retry redelivers the broadcast. Reactions have no application-level backfill, but they do not need one for a peer whose path is known |
| social4 | A,B,C | B goes offline via link drop | ✅ Presence flips on the beacon timeout, measured at 59.8s |
| social5 | A,B,C | A sets an avatar, then removes it | ✅ Propagates to both peers; the removal waits out `SEND_RATE_LIMIT_SECS` (60s, answered 429 until it elapses) then clears |
| social6 | A,B,C | A changes display name | ✅ Propagates; directory search finds the new name |
| social7 | A,B,C | A adds B as a friend with a nickname | ✅ Local only — C sees nothing, B is not notified |
| social8 | A,B,C | B replies to A's message; C reacts to the reply | ✅ `reply_to` and the reaction target resolve identically on all three |

### `restart` — Restart, persistence, ordering

| ID | Peers | Actions | Expected result |
|---|---|---|---|
| restart1 | A,B,C,D | Public channel with B, C joined; **A restarts**; D joins | ✅ **Fixed.** Was: A held `[B, C, D]` while B and C stayed at `[A, B, C]` and B's send never reached D. Now all four views agree, B learns about D in 1.0s. Promoted to strict as a regression guard |
| restart2 | A,B,C,D | Full history and roster built; all four killed and restarted | ✅ Identities, messages and the invite-only roster all survive |
| restart3 | A,B | A invites B with no path warm-up | ⚠️ **Confirmed.** The first invite is dropped silently; 2 attempts needed |
| restart4 | A,B,C | A admits C and sends a chat message immediately after | ✅ Landed in 0.0s on this run — the race is real but did not bite. Kept as a probe |
| restart5 | A,B,C | A single tester is reset (data wiped, same slot) | ✅ Returns as a new identity holding nothing, and the owner keeps a subscriber row for an identity that will never reappear |

### `voice` — Live group voice

Voice has two planes and the pytest suite only reaches one. Signalling is
LXMF, but frames travel over a full mesh of real RNS Links — one per
participant pair, each authorised on its own VP_HELLO/VP_ACCEPT handshake.
`tests/fake_voice.py` doubles that transport; nothing under `tests/` dials a
real link, and `smoke_test.py` covers exactly one pair. This family is the
three- and four-peer cases, and the states `docs/voice.md` is explicit about.

| ID | Peers | Actions | Expected result |
|---|---|---|---|
| voice1 | A,B,C | All three join voice on a public channel | ✅ Full mesh in 2.0–3.0s; every peer `streaming` to every other |
| voice2 | A,B,C,D | Three in voice, then D joins | ✅ D learns all three occupants and they learn D — roster in 0.0s, mesh in 3.0s. Exercises the unicast `voice_state` reply path three times over |
| voice3 | A,B,C | C leaves voice cleanly | ✅ Dropped from every roster in 0.0s; C reports no session |
| voice4 | A,B,C | C is **killed** mid-call, sending no `voice_leave` | ⚠️ Expires only on the roster TTL: **27.6s** here, and the testenv shortens that TTL to 30s from the production **180s**. A crashed participant lingers up to 3 minutes in the real client |
| voice5 | A,B,C | C's link drops mid-call | ⚠️ Kept in the roster rather than hidden, as the doc requires — but downgrades to `connecting`, not `unreachable`, and was still `connecting` 15.1s later. A UI would show "connecting…" indefinitely for someone who is gone |
| voice6 | A,C | Member without `voice_chat` joins voice; then granted | ✅ Refused, then admitted. The mirror pair — a refusal only means something if the grant demonstrably works |
| voice7 | A,C | Channel whose permissions predate voice (no `voice_chat` key) | ✅ Fails closed for the member, owner always passes, re-saving permissions admits the member |
| voice8 | A,B,C | A revokes `voice_chat` from member while C is streaming | ✅ Cut off in **0.5s**, matching the doc's ~1s re-authorisation sweep. Same claim `test_adversarial.py` makes against the transport double, here over real links |
| voice9 | A,B,C | All three stream the test tone for 8s | ✅ Each peer receives from both others: **384 frames, 0.0% loss, ~2ms jitter**. The full-mesh version of the smoke test's single pair |
| voice10 | A,B,C | Five chat messages while the voice mesh streams | ✅ Text delivery unaffected (0.0s), despite sharing the interface |
| voice11 | A,B | Voice over a `lora_fast` link, with the tone measured | ⚠️ **Two findings**: the link reports `streaming` and `loss_pct` reports ~6% while only ~8% of frames arrive. See below |

#### voice11: the quality metric cannot see a starved link

`docs/voice.md` says voice "is not viable over LoRa or packet radio" and that
the UI should surface that rather than mask it. Both halves are worth checking,
and the run says the first is right while the second does not currently work.

Over `lora_fast` (SF7, 5.5 kbps) the mesh link **comes up and reports
`streaming`**, then delivers 24–46 frames against ~384 expected across three
runs — 6–12% of the audio — at 100–240ms jitter. Voice is indeed unusable, but
nothing the UI is told says so:

- `link_state` reads `streaming`, the same value a perfect link gets.
- `loss_pct` reads **0.0–7.7%**, because it counts gaps between frames that
  *arrived*. Frames that never reach the wire are not gaps, so a link
  delivering 8% of the audio reports a loss figure implying 94% got through.

`docs/voice.md` designates `rx_quality`'s `loss_pct` as "the backend signal for
a per-peer connection-quality indicator in the UI". A UI built on it would show
a healthy connection on an unusable call. The signal that does show it is
delivery ratio — frames received against the ~48/s the codec produces — which
`frame_stats()` has the raw numbers for but does not expose as a rate.

Neither is a crash, so voice11 stays a probe. Both are worth a decision before the
voice UI ships a quality indicator.

### `api` — The API surface

Every other family drives the backend through `devtools/testenv/api.py` and
takes its access control on trust. This family tests that control directly,
because it is the only thing between a tester's identity and any process — or
any web page — that can reach the port. Added after the August security audit
(PR 52) gave the API a token; before it, this surface was unauthenticated with
wildcard CORS. Nothing here touches the mesh, so the whole family runs in
about four seconds.

| ID | Peers | Actions | Expected result |
|---|---|---|---|
| api1 | A | Call four reads, one mutation and the event socket with no token | ✅ Every route 401; the socket refused with HTTP 403; the identity unchanged |
| api2 | A | A wrong token, then the right one as a header, a bearer and `?token=` | ✅ Wrong token 401; all three routes 200; the socket opens on both the header and the query parameter |
| api3 | A | Open the event socket with `Origin: http://evil.example`, then with its own | ✅ Foreign origin refused 403, own origin accepted. The socket checks this itself — a browser applies neither CORS nor same-origin policy to a WebSocket handshake |
| api4 | A,B,C,D | Present each tester's token to a different tester's API | ⚠️ **One token for the whole environment**, as designed: B accepts A's, C's and D's (all 200), and the orchestrator's unauthenticated `/config` serves it. Recorded so the harness never claims per-identity isolation it does not have |

### `integrity` — Message integrity: authorship and attachments

A synced message reaches you from a peer who did not write it. LXMF
authenticates the peer that handed it over and nothing else, so PR 52 gave
every message its author's own signature — and a receiver that cannot verify
one drops the message. That makes verification a *delivery* dependency, not
only a security check: a receiver who cannot resolve an author's public key
loses honest history with nothing but a log line to show for it.

| ID | Peers | Actions | Expected result |
|---|---|---|---|
| integrity1 | A,B,C,D | C offline; A sends 2; A's process killed; C online and backfills from B | ✅ C accepts and correctly attributes history whose author is gone, in 11.1s. A relay's own signature is not what C checks |
| integrity2 | A,B,C,D | B owns the channel; A sends, then dies; D wiped to a **new identity**, joins and backfills | ✅ **Was a confirmed gap, now fixed.** As a probe it measured 4.0s for the live owner's message and *never* for the dead author's. Responders now send each batch's author keys, and D holds both. Strict since |
| integrity3 | A,B,C,D | A sends a real 64×64 JPEG | ✅ All three receivers hold it with `image_stripped: false` and can fetch the bytes. The signature covers the attachment, so the two travel together |
| integrity4 | A,B,C,D | A sends a 68-byte PNG declaring 20000×20000 (400M pixels) | ✅ Delivered as text with no attachment on all four. The sender's own API is the gate: `prepare_image` fails closed rather than forwarding bytes it could not re-encode |

#### integrity2: history used to die with its author — fixed

integrity1 and integrity2 differ by one thing — whether the receiver ever shared the network
with the author — and they used to land on opposite sides of it:

| | Author reachable when written | Receiver's state | Before | After |
|---|---|---|---|---|
| integrity1 | yes, C was a subscriber throughout | knows A from before | accepted in 11.1s | 2.0s |
| integrity2 | yes, but D did not exist yet | fresh identity, never saw A announce | **never accepted, 90s** | 0.0s |

`verify_message` returns False for "we cannot check yet" exactly as it does
for "this is forged", and the caller drops the row either way.
`resolve_author` can only fall back to `RNS.Identity.recall()`, which needs an
announce the departed peer will never send again. So a channel's history is
readable by whoever was present to receive it directly, and progressively
unreadable to everyone who joins afterwards — one author at a time, as people
leave the mesh.

**Fixed** by relaying the author's public key with their messages: a sync
response now carries one deduplicated `{author: key}` map (`F_AUTHOR_KEYS`,
0x71) alongside the batch, and the receiver caches each key only if it hashes
back to the identity claiming it. That check is what makes accepting a key
from a relay safe — an identity hash is derived from its public key, so a key
that does not hash back simply is not that identity's. The relay is passing
along public information it cannot forge, not vouching for anything.

One map per response rather than a key per row: a batch is a handful of
authors and up to fifty messages, and at 1 kbps the per-row form would cost
roughly the whole response over again.

Still open, and smaller: a receiver that *does* hold back a message says so
only in a `LOG_WARNING`. `messages_rejected` already reaches the sync status,
so surfacing "N held back, author unverifiable" is a UI change, not a
protocol one.

A related defect found while reading this path is **fixed** on this branch: a
row rejected for an unverifiable signature did not bound the sync watermark,
so a batch of `[rejected_old, accepted_new]` advanced past the rejected row and
no future sweep would ever offer it again. An author's key arriving *late* was
enough to lose their history permanently. Regression test in
`tests/test_sync_multipeer.py::TestRejectedRowBoundsTheWatermark`, which fails
without the one-line guard.

## The LoRa pass

Every family was re-run with `--link-profile lora_fast` (SF7, 5.5 kbps, 60±20ms,
1% loss). Same scenario bodies, timeouts scaled 6×. This is where the suite
earned its keep: five scenarios that pass on broadband fail on a radio, one
fails on broadband and *passes* on a radio, and one probe turns from a latency
footnote into an outright failure.

| Family | Broadband | LoRa SF7 | New on LoRa |
|---|---|---|---|
| `public` — public channels | 6/6 | 6/6 | public5 probe now returns **nothing at all** |
| `invite` — invite-only | 11/12 | 10/12 | **invite7** |
| `sync` — sync | 8/9 | 7/9 | **sync8**, **sync11**; sync2 *inverts* |
| `links` — degraded links | 5/5 | n/a (already shaped) | — |
| `servers` — servers | 5/5 | 5/5 | — |
| `social` — social | 7/7 | 5/7 | **social5**, **social6** |
| `restart` — restart | 3/3 | 3/3 | — |

Servers (E) and restart/persistence (G) are unaffected — including restart1, the
persisted subscriber-list counter. Everything that broke is a *propagation*
path: sync, member-list documents, avatars, directory entries.

### sync11 regresses on a slow link, with the same fingerprint

The four-way partition reconcile fails again at SF7, and the failure looks
exactly like the one the `sync_served` fix cured:

```
A missing: B-alone-0, C-alone-0, D-alone-0
B missing: A-alone-0
C missing: A-alone-0
D missing: A-alone-0, B-alone-0
```

Only the *first* message of each peer's pair, never the second. So the fix
removed one path to that failure and a second one remains, reachable only when
the reconcile takes long enough.

The likely mechanism is a constant that does not scale:
`PEER_TRUST_HORIZON_SECS = 300` widens a responder's sweep 300 seconds behind
the requester's claimed watermark, which is what lets it serve history it
acquired since the requester last asked. But how far back that needs to reach
depends on how long the exchange takes, and that scales with link speed —
broadband reconciles in **31.7s** (comfortably inside 300s), SF7 took **1141s**
(far outside it). A wall-clock constant guarding a link-speed-dependent window
is the shape of the bug. Unverified, but it is where to look.

### sync2 inverts — it passes on LoRa and fails on broadband

The scenario that has failed on broadband since it was written passed at SF7 in
**75s**, well inside the broadband budget it never met. That is not patience:
it recovered faster in wall-clock on the slower link.

That reframes sync2 from "sync sometimes doesn't answer" to a **timing race** that
a slow link happens to win. It is the strongest lead the open sync2 investigation
has had, and it argues the cause is in the reconnect/request cadence rather
than in the responder's authorisation.

### public5 escalates from latency to failure

On broadband a late public-channel joiner backfills in 1–9s, riding the owner's
next announce. At SF7 it received **nothing in 723 seconds**, with
`sync_state: unknown` — meaning no sync request was ever issued, not that one
went unanswered.

The known gap (public join fires no sync request) was previously worth "up to
60s of blindness in the real client". On a radio it looks closer to "may never
backfill at all", which is a materially different bug.

### The other three

- **invite7** — a promoted admin invites a fourth peer; the four rosters never
  converge in 360s (broadband: 30s). Member-list documents are signed msgpack
  blobs carrying the whole roster, and the invite → join-request → document
  chain is four hops.
- **sync8** — granting `full_sync` mid-session does not re-open withheld history:
  D still held 0 of 3 after 1375s (broadband: 3.0s). The re-ask depends on
  noticing an entitlement change on the *next* request, which is announce-driven.
- **social5 / social6** — an avatar removal never reaches a peer, and a display-name
  change never reaches the directory within 360s. Both propagate as
  announce/metadata rather than as retried messages.

social5 and social6 in particular may be latency rather than loss — neither scenario
waits indefinitely, and both payloads are large relative to the link. Worth
re-running at a higher scale before treating them as defects.

### SF10 (1 kbps) finds a bandwidth floor, not more bugs

A targeted `--link-profile lora_long` pass over the rows most likely to expose
an ordering problem (public5, invite3, sync4, sync11, restart3, restart4) was cut short after the first
two, because both failed the same uninformative way:

| | |
|---|---|
| public5 | timed out delivering **5 messages to two subscribers** in 600s — before the scenario's subject was reached |
| invite3 | timed out waiting for the owner to admit a member in 250s — the invite chain, not the `full_sync` question |

Both failures are in *setup*, not in the behaviour under test, so they say
"1 kbps cannot carry this workload" rather than anything about the logic. That
is a real limit worth knowing — and it is consistent with links7/links8, where three
short messages *do* cross `packet_radio` and `lora_long` in ~10s. The floor is
not the link, it is the size of the control-plane operations: signed
member-list documents and multi-message batches.

Running the remaining four rows would have cost roughly another hour to
re-confirm the same ceiling, so the pass was stopped. SF7 is the useful radio
profile for this suite; SF10 belongs in the links family, where the payloads are sized
for it.

## Findings

Everything the matrix turned up, across all ten families.

### Fixed

| Finding | Detail |
|---|---|
| **History died with its author** — a peer who joins after an author leaves could never read that author's messages, because verifying needs a key only the author's own announce could supply | Fixed: a sync response carries a deduplicated `{author: public key}` map, and a relayed key is cached only if it hashes back to the identity claiming it. integrity2 went from *never* to 0.0s; regression test in `tests/test_sync_multipeer.py` |
| **Join and invite were sent once and never retried** — `_send_raw` in `subscription.py` and `invite.py` dropped the message outright when the recipient's path was unresolved, which is exactly when a first join or a first invite happens | Fixed: both hold the message in a bounded `ControlRetryQueue` and flush it when the peer announces. Regression tests in `tests/test_subscriptions.py` and `tests/test_invites.py` |
| **A sync answer was dropped when the responder could not yet address the requester** — and unlike every other send path it did not even request a path. The requester sat at `pending`, unable to tell silence from refusal | Fixed: the answer is held (for no longer than the requester will accept one) and re-sent when the peer announces. This is most of sync2 — see below |
| **Nothing noticed our own link returning** — every catch-up path is driven by hearing *from* a peer, so the node that was itself away had no trigger | Fixed: `connectivity.py`'s `LinkWatcher` polls Reticulum's interface state and resyncs on the transition back to online, ignoring shared-instance client interfaces. Tests in `tests/test_connectivity.py` |
| **A sync row rejected for an unverifiable author signature did not bound the watermark**, so a batch of `[rejected_old, accepted_new]` skipped past it and no future sweep would offer it again | Fixed on this branch: the signature-rejection path now sets `failed_ts` the same way a failed insert does. A row with an *implausible timestamp* deliberately does not, since it cannot be placed and would let one bad row freeze sync. Regression test in `tests/test_sync_multipeer.py` |
| **One `sync_progress` row written from both directions**, collapsing the responder's trust-horizon floor and stranding history older than a requester's watermark | Root-caused and fixed by splitting the serve direction into a `sync_served` table; regression test in `tests/test_sync_multipeer.py`. Found through sync11, but **it did not fix sync11** — see below |
| **restart1** — `_subscriber_versions` lived only in memory, so a restarted owner renumbered from 1 and every later list was rejected as a replay | Fixed on `main` by the August security audit, which persists the counter in a `subscriber_list_versions` table and re-checks the version under the commit lock. This branch's own fix was dropped at the merge in favour of it; the end-to-end regression test in `tests/test_subscriptions.py` stays |

### Open

| Finding | Detail |
|---|---|
| **api4** — the dev environment shares one API token across every tester, and the orchestrator's unauthenticated `/config` serves it | **Confirmed** (probe). Fine for a dev box; it means port 8800 is the real trust boundary, not the tester ports |
| **sync11** — a four-way partition reconciles only sometimes: 1 pass in 5 runs, always losing the *first* message a peer wrote in isolation | **Partly root-caused.** The watermark collision above was one cause and is fixed. The deep-sync cooldown refusing a returning peer's request silently, once per pair per 60s, is the leading candidate for the rest |
| **sync2** — a returning node sometimes still fails to recover history | **Two causes found and fixed** (no local link-recovery trigger; the responder's answer dropped on an unresolved path), taking it from 2/5 to **4/5** runs. It still fails occasionally, so it stays strict and failing. See below |
| **invite11** — `kick` and `manage_roles` are grantable to any role, but a non-admin's member-list document is rejected by every recipient | **Confirmed.** The grant succeeds locally and does nothing on the network. Resolution is a product decision — see below |
| **voice11** — `loss_pct`, the metric `docs/voice.md` designates for the UI's per-peer quality indicator, cannot see a starved link. It counts gaps between frames that arrived, so a link delivering 8% of the audio reports ~6% loss, and `link_state` still reads `streaming` | **Confirmed** across three runs. Delivery ratio (frames received against ~48/s) is the signal that shows it; `frame_stats()` has the raw counts but exposes no rate |
| **voice5 / voice4** — a voice participant whose link drops shows `connecting` indefinitely rather than `unreachable`, and one whose process dies lingers for the roster TTL — 180s in production | **Confirmed.** Neither is wrong, but a UI showing "connecting…" for three minutes after someone crashed is not the honest state `docs/voice.md` asks for |
| **public5** — a public-channel join fires no sync request; backfill waits on the next peer announce | **Confirmed, and deliberately left.** 0 messages at join, backfill at 1.0s / 9.1s tracking the 10s heartbeat; up to 60s in the real client, and at SF7 it never arrived at all (see the LoRa pass). Deferred by decision — public-channel behaviour is being left alone for now |
| **public6** — `full_sync` has no effect on public channels; any subscriber can pull full history | **Confirmed, and deliberately left.** Identical backfill with and without the grant, and the UI offers the toggle regardless — so it reads as a privacy control that is not one. Deferred by decision, same as public5 |

### Predictions the runs refuted

Both were written to demonstrate a gap and demonstrated its absence instead.

| Prediction | What actually happened |
|---|---|
| **social3** — reactions have no backfill path, so an offline peer misses them permanently | D recovered the reaction in 14.1s and 15.2s. LXMF's own retry redelivers the broadcast once the link returns; no application-level backfill is needed for a peer whose path is known |
| **public10** — a subscriber that misses a subscriber-list broadcast is stranded | It recovered every time, by the same LXMF retry. The no-retry gap only bites when the path was *never* resolved — a cold-start race, not an offline-peer case |

### One suspected gap that turned out not to be

An earlier draft recorded a third finding from the public family: that non-owner fan-out
on a public channel was unreliable, from public8 failing intermittently and public9
varying run to run. That was wrong, and the way it was wrong is worth keeping.

The reasoning was that `SubscriptionManager._send_raw` has no retry queue, so a
dropped subscriber-list broadcast would strand a peer permanently. public10 was
written to demonstrate exactly that — take a subscriber offline across a roster
change and watch it never recover. It recovered every time. `_send_raw` only
drops when `Identity.recall()` returns `None`, i.e. the path was never
resolved; once a path is known the message goes to LXMF, whose own outbound
retry redelivers when the link returns. The no-retry gap is a cold-start race,
not an offline-peer case.

The narrow version of it is real, though, and the sync family found it by accident:
sync4's setup failed because the owner never registered a joiner whose `MT_SUBSCRIBE`
went out before its path resolved. Same code path, but only reachable when the
peers have never talked — which is why public10, whose peers were already in contact,
could not produce it.

The real cause was in the scenarios: they asserted fan-out before the owner had
processed the joiners' `MT_SUBSCRIBE`, so the sends were addressed to a set the
targets weren't in. Waiting on the owner's registration (and, where a peer
addresses other peers, on full subscriber convergence) made three consecutive
family runs clean. It is a genuine ordering constraint — now written into the
timing rules above — but it belongs to the harness, not the app.

Worth stating plainly: an intermittent scenario is far more likely to be an
under-specified precondition than a real race, and "no retry here" is not a bug
until you check what the layer underneath does.

## Harness

Built and running, in `devtools/testenv/scenarios/`:

| File | Purpose |
|---|---|
| `runner.py` | Spawns `orchestrator.py --testers N`, waits for every API, runs the selection, resets between scenarios, reports |
| `peer.py` | `Peer` (one tester's API) and `Orchestrator` (process/link lifecycle). One method per endpoint, no logic. Every tester call carries the environment's API token, read from the orchestrator's `/config` |
| `asserts.py` | Polling assertions: `wait_until`, `settle`, `hold_for`, `all_hold`, `subscribers_converged`, `diff_report`, `subscriber_views` |
| `scenario.py` | The `@scenario` registry and the strict/probe distinction |
| `flows.py` | Shared setup: discovery, joining with owner registration, invite/accept, link up/down |
| `scen_public.py` | Family `public` |
| `scen_invite.py` | Family `invite` |
| `scen_sync.py` | Family `sync` |
| `scen_links.py` | Family `links` |
| `scen_servers.py` | Family `servers` |
| `scen_social.py` | Family `social` |
| `scen_restart.py` | Family `restart` |
| `scen_voice.py` | Family `voice` |
| `scen_api.py` | Family `api` |
| `scen_authorship.py` | Family `integrity` |

All ten families are built: 81 scenarios, 62 strict and 19 probes.

```bash
.venv/bin/python devtools/testenv/scenarios/runner.py                # everything
.venv/bin/python devtools/testenv/scenarios/runner.py --family public
.venv/bin/python devtools/testenv/scenarios/runner.py --scenario public5 public6
.venv/bin/python devtools/testenv/scenarios/runner.py --scenario public8 --repeat 6
.venv/bin/python devtools/testenv/scenarios/runner.py --json out.json
```

`--repeat` re-runs the selection to characterise a flake; `--attach` uses an
orchestrator you already have running.

### Link-profile passes

`--link-profile` shapes every tester before each scenario, so the same
scenario bodies run again on a radio instead of broadband:

```bash
.venv/bin/python devtools/testenv/scenarios/runner.py --family sync --link-profile lora_fast
.venv/bin/python devtools/testenv/scenarios/runner.py --scenario restart3 restart4 --link-profile lora_long
```

This is a matrix dimension rather than duplicated rows: one scenario body, two
link conditions, so a difference between the passes is a real behavioural
difference rather than two tests that drifted apart.

Assertion timeouts scale automatically from the profile (`--timeout-scale`
overrides), because every timeout in the suite is tuned for broadband and a
radio pass should fail on behaviour, not on patience. The scale factors come
from the measured the links family timings — the same 10-message batch takes 5s on
broadband and 102s on `lossy`.

One caveat: shaping applies live, but the matching bitrate hint written into
each tester's RNS config is only read at boot, so it stays stale for the run.
The shaper still enforces the rate on the wire, which is what these passes are
about; RNS's own announce pacing is the part that does not see it.

Needs both `requirements.txt` and `devtools/testenv/requirements.txt` in the
venv. Exits 0 if every strict scenario passed.

**Strict vs probe.** A strict scenario asserts settled behavior — failing is a
bug. A probe tests a prediction about behavior nothing covers yet: it records
what actually happened and never fails the run. That keeps a known gap from
sitting permanently red while still reporting it every time.

**Teardown owns the process group.** The orchestrator does not reap its hub and
workers when it is terminated, and the survivors hold the ports the next run
preflights against. `runner.py` starts it via `start_new_session=True` and kills
the whole group.

Every assertion polls — `wait_until` defaults to 30s, 90s for backfill and
anything on a degraded profile. Each scenario emits a JSON record with
pass/fail, duration and whatever it measured, so timing regressions show up as
data rather than just red.

Slow by design: the public family alone is ~6 minutes, most of it environment resets. It
belongs on a nightly or on-demand run, not the per-PR gate — `pytest tests/`
stays the merge gate.

## Status

All ten families built and run: **81 scenarios, 62 strict and 19 probes.**

| Family | Scenarios | Result |
|---|---|---|
| `public` — public channels | 10 (6 strict, 4 probes) | All passing, three consecutive clean runs |
| `invite` — invite-only and membership | 13 (12 strict, 1 probe) | 11/12 — **invite11 fails** on a confirmed permission gap |
| `sync` — offline and sync | 10 (9 strict, 1 probe) | 7/9 on the latest run — **sync2 and sync11 both fail**, intermittently and rarely together. sync11 has passed 1 run in 6 |
| `links` — degraded links | 10 (5 strict, 5 probes) | All passing, on genuinely shaped links |
| `servers` — servers | 6 (5 strict, 1 probe) | All passing |
| `social` — reactions, presence, identity | 8 (7 strict, 1 probe) | All passing; social3's prediction refuted |
| `restart` — restart and ordering | 5 (3 strict, 2 probes) | All passing; restart1 confirmed, then fixed; restart3 confirmed |
| `voice` — live group voice | 11 (8 strict, 3 probes) | All passing; voice4, voice5 and voice11 recorded gaps |
| `api` — the API surface | 4 (3 strict, 1 probe) | All passing; api4 records the shared-token property |
| `integrity` — message integrity | 4 (4 strict) | All passing; integrity2 found a real gap, now fixed and strict |

**60 of 62 strict scenarios pass.** The failures are real defects, left strict
and failing on purpose, so `--family invite` and `--family sync` exit non-zero until
they are resolved. invite11 fails every run. sync2 and sync11 are both intermittent and
trade places: the pre-merge run lost sync2, the post-merge run lost sync11, and the
count landed at 53/55 either way.

Re-run against `main` after the August security audit merged (PR 52): the suite
is unchanged at 53/55, so the audit regressed nothing here. Its one effect on
the harness is that every tester API now requires a token, which `peer.py`
reads from the orchestrator's `/config`.

Roughly 5–10 minutes a family, most of it environment resets between scenarios.
The whole matrix is around 45 minutes, which is why it belongs on a nightly or
on-demand run rather than the per-PR gate.

Remaining work:

1. Finish sync11: confirm the deep-sync cooldown is what strands the remaining
   `-alone-0` rows, then decide whether a refusal should answer with an
   explicit "throttled" rather than silence.
2. Decide invite11: stop offering `kick`/`manage_roles` below admin, or admit
   permission-holders as trusted signers.
3. Let `SyncStatusTracker` distinguish "refused" from "waiting" — today both
   read as `pending` forever.
4. Track down sync2's remaining 1-in-5 failure, and surface held-back messages
   in the UI rather than a log line.
5. C6 and C12, which need control of the clock — and, now, a J5 for the
   audit's 300s clock-skew ceiling, which silently drops every message from a
   peer whose clock runs fast.
