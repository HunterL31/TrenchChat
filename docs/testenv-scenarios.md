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

### A — Public (open-join) channels

| ID | Peers | Actions | Expected result |
|---|---|---|---|
| A1 | A,B,C,D | A creates public channel | B, C, D each list it under discovered, none subscribed |
| A2 | A,B | A creates; B joins | A's subscriber set = {B}; B receives the signed subscriber list; B's roster view includes A |
| A3 | A,B,C,D | A creates; B, C join; A sends 3 | B and C hold all 3; D holds none |
| A4 | A,B,C,D | B (subscriber, not owner) sends 1 | A and C hold it; D does not — recipients are the subscriber set plus self |
| A5 | A,B,C,D | A creates; B, C join; A sends 5; **then** D joins | ⚠ **Confirmed.** D holds 0 at the instant it joins — public join calls `subscription_mgr.subscribe()` only, no `channel_joined` callback, so nothing requests sync. Backfill lands on A's next peer announce: measured 1.0s and 9.1s on two runs, tracking the 10s heartbeat phase. Scales to a 60s worst case in the real app |
| A6 | A,B,C,D | A6a: A creates public, grants `full_sync` to member, sends 5, D joins. A6b: identical without `full_sync` | ⚠ **Confirmed.** Both channels backfilled all 5 to D, with and without the grant. Public channels never open tenure, so `has_any_tenure` is false and tenure filtering — the only thing `full_sync` gates — never engages |
| A7 | A,B,C | B leaves; A sends 2 | A removes B from subscribers; C holds both; B holds neither |
| A8 | A,B,C,D | All 4 joined and the subscriber set has converged; each sends 2 in turn | All four converge on 9 messages (a seed plus 8). Roster settle measured at 0.5–4.0s |
| A9 | A,B,C,D | A (owner) leaves its own channel, then C sends | C's message still reaches B and D — the subscriber lists they already hold are unaffected by the owner leaving. The departed owner does not receive it and stays unsubscribed |
| A10 | A,B,C,D | B, C join; C goes offline; D joins (C misses the broadcast); C returns | C learns about D and its next send reaches D. Recovery measured at 0.5s, 1.0s and 18.1s across runs — LXMF's own retry backoff, not an application-level repair |

### B — Invite-only channels and membership

| ID | Peers | Actions | Expected result |
|---|---|---|---|
| B1 | A,B,C,D | A creates invite-only | Nobody sees it in discovered — `announce_channel` refuses invite-only regardless of the discoverable flag |
| B2 | A,B | A invites B; B accepts | B's pending invite clears; member-list doc lands; A and B rosters identical (2 members, owner+member); B's tenure opens |
| B3 | A,B | A sends 3; **then** invites B; B accepts. B3a: member role lacks `full_sync`. B3b: A grants `full_sync` to member first | B3a: B holds 0 backlog — tenure filtering drops rows from before B's join. B3b: B holds all 3. This is the real `full_sync` test (public channels can't show it — see A6) |
| B4 | A,B,C,D | A invites B, C, D; all accept | All four rosters identical: 4 members, A owner, rest member |
| B5 | A,B,C | A invites C; C declines | C is not a member; A, B rosters unchanged; nothing sent on decline |
| B6 | A,B,C,D | All 4 members; A kicks C | B, D, A rosters drop to 3; C's local membership clears and its pending outbound for the channel is cancelled; a message C sends after is dropped by A, B, D |
| B7 | A,B,C,D | A promotes B to admin; B invites D; D accepts | All four rosters identical, B `admin`, D `member` |
| B8 | A,B,D | A demotes B to member; B invites D | Join request rejected — `_handle_join_request` checks INVITE against B's current role; D never joins |
| B9 | A,B,C | A revokes `send_message` from member role | C's message is dropped by A and B (and by C's own outbound guard); B, still admin, sends fine |
| B10 | A,C | C calls `/roles` with `remove_members=[A]` | ✅ `{"ok": false}`, no document published, rosters unchanged on every peer — adversarial path, GUI bypassed |
| B11 | A,B,C | A grants `kick` to member; C kicks B | ❌ **Fails, and the failure is the finding.** The grant passes every local check and `/roles` reports success, but no peer ever applies it. See below |
| B12 | A,B,C,D | A promotes B; A and B both publish a roster change within ~1s | Both documents validate against stored state; final rosters identical on all four; no split-brain |
| B13 | A,B,C | C (member) attempts `/channels/{h}/permissions` | `{"ok": false}` — lacks `manage_channel`; stored perms unchanged everywhere |

#### B11: a grantable permission that cannot take effect

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

Worth noting what this does to **B10**, which asserts a member *cannot* kick the
owner. B10 passes — but while B11 fails, it passes for the wrong reason: no
member can effectively kick anyone. B11 is what gives B10 its meaning, which is
why the pair is worth keeping together.

Not fixed here: the resolution is a product decision (stop offering these
permissions below admin, or admit permission-holders as trusted signers), not a
bug with one obvious correction.

### C — Offline behavior and sync

The reason this environment exists. All three sync mechanisms only run on a
degraded or interrupted link.

| ID | Peers | Actions | Expected result |
|---|---|---|---|
Built: C1–C5, C7–C11. C6 (deep-sync cooldown) and C12 (a 7-day-old window)
need control of the clock and stay deferred.

| ID | Peers | Actions | Expected result |
|---|---|---|---|
| C1 | A,B,C | B goes offline (link drop); A sends 3; B goes online | ✅ B receives all 3, in 1.0–5.0s |
| C2 | A,B,C,D | B offline; A sends 3; A offline; B online (only C, D reachable) | ❌ **Fails ~half of runs**, root cause established. B holds 0/3 with both responders at `pending` for the full timeout. See below |
| C3 | A,B,C | Same as C1 but B is **killed and restarted** instead of link-dropped | ✅ B ends with its own history plus what it missed, in 3.5s, via the cold path |
| C4 | A,D | D offline; A sends 60 (> `MAX_RESPONSE_MESSAGES` = 50); D online | ✅ D ends with all 60 and `state == synced`, in 18.1s — the truncated batch does chain its follow-up |
| C5 | A,B,C | B offline for messages 1–5; B online, C offline for 6–10; C online | ✅ Both end with all 10, in 10.6s — per-(channel, peer) watermarks hold up |
| C7 | A,B | B offline across a batch, then back; watch the sync state | ✅ Settles on `synced` with every message present |
| C10 | A,B,C,D | Hub killed (total partition); each peer sends 1; hub restarted | ✅ All four reconcile in 12.1s once the hub returns |
| C8 | A,D | D joins without `full_sync`; A grants `full_sync` to member | ✅ The backlog arrives 3.0s after the grant, without D restarting |
| C9 | A,B,C | A kicks C; C requests sync | ✅ C's transcript stays frozen at the kick |
| C11 | A,B,C,D | All 4 offline simultaneously, each sends 2 locally, all come online | ✅ **Fixed.** Reconciled all 9 messages in 31.7s. Failed on every run before the fix below |
| C12 | A,B | B offline past `SYNC_WINDOW_SECS` (7 days, clock-shifted); B online | Deferred — needs clock control |

#### C11 and C2, root-caused: one watermark row, two directions

**Fixed.** The `sync_progress` table was being written from both directions
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

#### C11 before the fix: the first message of a partition is lost

Seven of the eight built rows pass, several on the first attempt. C11 does not,
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

Everything around it works: C1 shows pending-retry flushing, C2 shows a third
party serving what the sender never delivered, C5 shows disjoint histories
reconciling, C10 shows a full partition healing once the hub returns. The
difference in C11 is that *every* peer is both a requester and the sole source
of its own history at the same moment. Not yet root-caused; the sync watermark
is the obvious first place to look, since "first message skipped, later ones
served" is what an over-advanced watermark would produce.

Left strict rather than reclassified as a probe. The expectation is the sync
design's own — any online member can serve any gap — so `--family C` exits
non-zero until it is fixed, which is the correct signal for an open bug.

#### C2, root-caused: a recovered link has no resync trigger

**Fixed.** C2 failed roughly half the time — 2 of 6 runs — with B holding 0 of 3
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
start, which is why C3 (kill and restart) always passed while C2 (drop and
recover) did not.

**The fix is not in yet, and one attempt has already been ruled out.** Making a
presence transition to online a second trigger looked right — presence is
maintained from inbound LXMF traffic, so it keeps working exactly where
announces do not. It does not fix this: presence transitions fire when a
*remote peer* returns, and in C2 it is the local node whose link recovered.
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

### D — Degraded links

| ID | Peers | Actions | Expected result |
|---|---|---|---|
| D1 | A,B,C,D | A on `lossy` (62.5 kbps, 250±150ms, 15% loss); A sends 10 | ✅ All three peers converge in **101.7s** |
| D2 | A,B | B on `serial` (9.6 kbps, lossless); A sends 5 | ✅ Delivered in 3.0s. Slow-but-lossless is not a stall |
| D3 | A,B,C,D | A broadband, B `satellite`, C `lora_fast`, D `serial`; each sends 1 | ✅ Converged in 4.5s across four differently-shaped links |
| D4 | A,B | B on `lossy`, dropped offline mid-burst | ✅ Caught up all 10 in 28.2s |
| D5 | A,B,C,D | All four on `lora_fast` (5.5 kbps) | ✅ Converged in 12.6s |
| D6 | A,B,C | B on `lossy`; A sends 15 — **the link never drops** | ✅ Converged in 19.6s. The README's stated reason for the lossy profile: retry and hints reached the way a bad radio does, not by killing a link |
| D7 | A,B | B on `packet_radio` (AX.25 1200 baud, 5% loss) | ✅ Three messages in 10.1s. The worst link the app claims to support for text |
| D8 | A,B | B on `lora_long` (SF10, 1.0 kbps) | ✅ Three messages in 11.1s |
| D9 | A,B | `custom` profile with explicit bitrate/latency/jitter/loss | ✅ Applied exactly as asked (32 kbps · 120±20ms · 8% loss); converged in 2.0s |
| D10 | A,B | Retune B broadband → `serial` mid-run | ✅ Shaping applies live: 0.5s then 1.0s, no restart needed |

### E — Servers

| ID | Peers | Actions | Expected result |
|---|---|---|---|
| E1 | A,B | A creates a server with 3 channels; invites B once | ✅ One invite admits B to the server and all 3 channels; a send on the *third* proves it |
| E2 | A,B,C | A grants `create_channel` to admin, promotes B; B creates a channel | ✅ Every member receives it via the re-published server document |
| E3 | A,B | A edits server permissions | ✅ Mirrored into every child channel; a per-channel override returns 409 and the mirror survives |
| E4 | A,B,C | B leaves the server | ✅ B unsubscribed from every channel in it; A and C unaffected |
| E5 | A,B,C | A kicks C from the server | ✅ C loses every channel at once and its later send is rejected |
| E6 | A,B | Server-level `full_sync` grant, then invite B with backlog in 2 channels | ✅ Both channels backfilled (1.0s, 0.0s) — server-scoped tenure resolves per channel |

### F — Reactions, emoji, presence, identity

| ID | Peers | Actions | Expected result |
|---|---|---|---|
| F1 | A,B,C,D | Public channel, all joined; B reacts to A's message | ✅ A, C and D all show count 1, owner included |
| F2 | A,B,C | B reacts, then removes the reaction | ✅ Clears on every peer |
| F3 | A,B,D | D offline; B reacts to A's message; D online | ⚠️ **Prediction refuted.** D *does* recover the reaction, in 14.1s and 15.2s across runs — LXMF's own retry redelivers the broadcast. Reactions have no application-level backfill, but they do not need one for a peer whose path is known |
| F4 | A,B,C | B goes offline via link drop | ✅ Presence flips on the beacon timeout, measured at 59.8s |
| F5 | A,B,C | A sets an avatar, then removes it | ✅ Propagates to both peers; the removal waits out `SEND_RATE_LIMIT_SECS` (60s, answered 429 until it elapses) then clears |
| F6 | A,B,C | A changes display name | ✅ Propagates; directory search finds the new name |
| F7 | A,B,C | A adds B as a friend with a nickname | ✅ Local only — C sees nothing, B is not notified |
| F8 | A,B,C | B replies to A's message; C reacts to the reply | ✅ `reply_to` and the reaction target resolve identically on all three |

### G — Restart, persistence, ordering

| ID | Peers | Actions | Expected result |
|---|---|---|---|
| G1 | A,B,C,D | Public channel with B, C joined; **A restarts**; D joins | ✅ **Fixed.** Was: A held `[B, C, D]` while B and C stayed at `[A, B, C]` and B's send never reached D. Now all four views agree, B learns about D in 1.0s. Promoted to strict as a regression guard |
| G2 | A,B,C,D | Full history and roster built; all four killed and restarted | ✅ Identities, messages and the invite-only roster all survive |
| G3 | A,B | A invites B with no path warm-up | ⚠️ **Confirmed.** The first invite is dropped silently; 2 attempts needed |
| G4 | A,B,C | A admits C and sends a chat message immediately after | ✅ Landed in 0.0s on this run — the race is real but did not bite. Kept as a probe |
| G5 | A,B,C | A single tester is reset (data wiped, same slot) | ✅ Returns as a new identity holding nothing, and the owner keeps a subscriber row for an identity that will never reappear |

### H — Live group voice

Voice has two planes and the pytest suite only reaches one. Signalling is
LXMF, but frames travel over a full mesh of real RNS Links — one per
participant pair, each authorised on its own VP_HELLO/VP_ACCEPT handshake.
`tests/fake_voice.py` doubles that transport; nothing under `tests/` dials a
real link, and `smoke_test.py` covers exactly one pair. This family is the
three- and four-peer cases, and the states `docs/voice.md` is explicit about.

| ID | Peers | Actions | Expected result |
|---|---|---|---|
| H1 | A,B,C | All three join voice on a public channel | ✅ Full mesh in 2.0–3.0s; every peer `streaming` to every other |
| H2 | A,B,C,D | Three in voice, then D joins | ✅ D learns all three occupants and they learn D — roster in 0.0s, mesh in 3.0s. Exercises the unicast `voice_state` reply path three times over |
| H3 | A,B,C | C leaves voice cleanly | ✅ Dropped from every roster in 0.0s; C reports no session |
| H4 | A,B,C | C is **killed** mid-call, sending no `voice_leave` | ⚠️ Expires only on the roster TTL: **27.6s** here, and the testenv shortens that TTL to 30s from the production **180s**. A crashed participant lingers up to 3 minutes in the real client |
| H5 | A,B,C | C's link drops mid-call | ⚠️ Kept in the roster rather than hidden, as the doc requires — but downgrades to `connecting`, not `unreachable`, and was still `connecting` 15.1s later. A UI would show "connecting…" indefinitely for someone who is gone |
| H6 | A,C | Member without `voice_chat` joins voice; then granted | ✅ Refused, then admitted. The mirror pair — a refusal only means something if the grant demonstrably works |
| H7 | A,C | Channel whose permissions predate voice (no `voice_chat` key) | ✅ Fails closed for the member, owner always passes, re-saving permissions admits the member |
| H8 | A,B,C | A revokes `voice_chat` from member while C is streaming | ✅ Cut off in **0.5s**, matching the doc's ~1s re-authorisation sweep. Same claim `test_adversarial.py` makes against the transport double, here over real links |
| H9 | A,B,C | All three stream the test tone for 8s | ✅ Each peer receives from both others: **384 frames, 0.0% loss, ~2ms jitter**. The full-mesh version of the smoke test's single pair |
| H10 | A,B,C | Five chat messages while the voice mesh streams | ✅ Text delivery unaffected (0.0s), despite sharing the interface |
| H11 | A,B | Voice over a `lora_fast` link, with the tone measured | ⚠️ **Two findings**: the link reports `streaming` and `loss_pct` reports ~6% while only ~8% of frames arrive. See below |

#### H11: the quality metric cannot see a starved link

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

Neither is a crash, so H11 stays a probe. Both are worth a decision before the
voice UI ships a quality indicator.

## Findings

Everything the matrix turned up, across all seven families.

### Fixed

| Finding | Detail |
|---|---|
| **C11** — one `sync_progress` row written from both directions, collapsing the responder's trust-horizon floor and stranding history older than a requester's watermark | Root-caused and fixed by splitting the serve direction into `sync_served`. C11 now reconciles in 31.7s; regression test in `tests/test_sync_multipeer.py` |
| **G1** — `_subscriber_versions` lived only in memory, so a restarted owner renumbered from 1 and every later list was rejected as a replay | Fixed by persisting the counter in a `subscriber_versions` table, loaded on construction and written through on both issue and accept. Regression test in `tests/test_subscriptions.py` |

### Open

| Finding | Detail |
|---|---|
| **C2** — a node whose own link recovers has no resync trigger. `on_peer_appeared` fires only on a *received announce*, and RNS suppresses announce replays for a destination the transport has already propagated | **Root-caused, not fixed.** Fails ~half of runs (4/8, 4/6). The fix needs a local link-recovery signal the production client does not currently have — see below |
| **B11** — `kick` and `manage_roles` are grantable to any role, but a non-admin's member-list document is rejected by every recipient | **Confirmed.** The grant succeeds locally and does nothing on the network. Resolution is a product decision — see below |
| **Subscribe/invite have no retry** — `_send_raw` in both `subscription.py` and `invite.py` drops the message when the path is unresolved, and nothing re-sends it | **Confirmed twice.** C4's setup hit it for `MT_SUBSCRIBE` (the owner never registered the joiner); G3 measured it for invites (first attempt dropped, 2 needed). Only re-issuing recovers |
| **H11** — `loss_pct`, the metric `docs/voice.md` designates for the UI's per-peer quality indicator, cannot see a starved link. It counts gaps between frames that arrived, so a link delivering 8% of the audio reports ~6% loss, and `link_state` still reads `streaming` | **Confirmed** across three runs. Delivery ratio (frames received against ~48/s) is the signal that shows it; `frame_stats()` has the raw counts but exposes no rate |
| **H5 / H4** — a voice participant whose link drops shows `connecting` indefinitely rather than `unreachable`, and one whose process dies lingers for the roster TTL — 180s in production | **Confirmed.** Neither is wrong, but a UI showing "connecting…" for three minutes after someone crashed is not the honest state `docs/voice.md` asks for |
| **A5** — a public-channel join fires no sync request; backfill waits on the next peer announce | **Confirmed.** 0 messages at join, backfill at 1.0s / 9.1s tracking the 10s heartbeat. Up to 60s in the real client |
| **A6** — `full_sync` has no effect on public channels; any subscriber can pull full history | **Confirmed.** Identical backfill with and without the grant. The UI offers the toggle regardless |

### Predictions the runs refuted

Both were written to demonstrate a gap and demonstrated its absence instead.

| Prediction | What actually happened |
|---|---|
| **F3** — reactions have no backfill path, so an offline peer misses them permanently | D recovered the reaction in 14.1s and 15.2s. LXMF's own retry redelivers the broadcast once the link returns; no application-level backfill is needed for a peer whose path is known |
| **A10** — a subscriber that misses a subscriber-list broadcast is stranded | It recovered every time, by the same LXMF retry. The no-retry gap only bites when the path was *never* resolved — a cold-start race, not an offline-peer case |

### One suspected gap that turned out not to be

An earlier draft recorded a third finding from family A: that non-owner fan-out
on a public channel was unreliable, from A8 failing intermittently and A9
varying run to run. That was wrong, and the way it was wrong is worth keeping.

The reasoning was that `SubscriptionManager._send_raw` has no retry queue, so a
dropped subscriber-list broadcast would strand a peer permanently. A10 was
written to demonstrate exactly that — take a subscriber offline across a roster
change and watch it never recover. It recovered every time. `_send_raw` only
drops when `Identity.recall()` returns `None`, i.e. the path was never
resolved; once a path is known the message goes to LXMF, whose own outbound
retry redelivers when the link returns. The no-retry gap is a cold-start race,
not an offline-peer case.

The narrow version of it is real, though, and family C found it by accident:
C4's setup failed because the owner never registered a joiner whose `MT_SUBSCRIBE`
went out before its path resolved. Same code path, but only reachable when the
peers have never talked — which is why A10, whose peers were already in contact,
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
| `peer.py` | `Peer` (one tester's API) and `Orchestrator` (process/link lifecycle). One method per endpoint, no logic |
| `asserts.py` | Polling assertions: `wait_until`, `settle`, `hold_for`, `all_hold`, `subscribers_converged`, `diff_report`, `subscriber_views` |
| `scenario.py` | The `@scenario` registry and the strict/probe distinction |
| `flows.py` | Shared setup: discovery, joining with owner registration, invite/accept, link up/down |
| `scen_public.py` | Family A |
| `scen_invite.py` | Family B |
| `scen_sync.py` | Family C |
| `scen_links.py` | Family D |
| `scen_servers.py` | Family E |
| `scen_social.py` | Family F |
| `scen_restart.py` | Family G |
| `scen_voice.py` | Family H |

All eight families are built: 73 scenarios, 55 strict and 18 probes.

```bash
.venv/bin/python devtools/testenv/scenarios/runner.py                # everything
.venv/bin/python devtools/testenv/scenarios/runner.py --family A
.venv/bin/python devtools/testenv/scenarios/runner.py --scenario A5 A6
.venv/bin/python devtools/testenv/scenarios/runner.py --scenario A8 --repeat 6
.venv/bin/python devtools/testenv/scenarios/runner.py --json out.json
```

`--repeat` re-runs the selection to characterise a flake; `--attach` uses an
orchestrator you already have running.

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

Slow by design: family A alone is ~6 minutes, most of it environment resets. It
belongs on a nightly or on-demand run, not the per-PR gate — `pytest tests/`
stays the merge gate.

## Status

All eight families built and run: **73 scenarios, 55 strict and 18 probes.**

| Family | Scenarios | Result |
|---|---|---|
| A — public channels | 10 (6 strict, 4 probes) | All passing, three consecutive clean runs |
| B — invite-only and membership | 13 (12 strict, 1 probe) | 11/12 — **B11 fails** on a confirmed permission gap |
| C — offline and sync | 10 (9 strict, 1 probe) | 8/9 — **C2 fails**, root-caused. C11 fixed |
| D — degraded links | 10 (5 strict, 5 probes) | All passing, on genuinely shaped links |
| E — servers | 6 (5 strict, 1 probe) | All passing |
| F — reactions, presence, identity | 8 (7 strict, 1 probe) | All passing; F3's prediction refuted |
| G — restart and ordering | 5 (3 strict, 2 probes) | All passing; G1 confirmed, then fixed; G3 confirmed |
| H — live group voice | 11 (8 strict, 3 probes) | All passing; H4, H5 and H11 recorded gaps |

**53 of 55 strict scenarios pass.** The two that do not — B11 and C2 — are real
defects, left strict and failing on purpose, so `--family B` and `--family C`
exit non-zero until they are resolved.

Roughly 5–10 minutes a family, most of it environment resets between scenarios.
The whole matrix is around 45 minutes, which is why it belongs on a nightly or
on-demand run rather than the per-PR gate.

Remaining work:

1. Fix C2 by giving the client a resync trigger for its own link recovering.
2. Decide B11: stop offering `kick`/`manage_roles` below admin, or admit
   permission-holders as trusted signers.
3. Give `subscription.py` and `invite.py`'s `_send_raw` a retry queue, or an
   explicit failure the caller can act on. Both drop silently on an unresolved
   path today, and only re-issuing recovers.
4. Let `SyncStatusTracker` distinguish "refused" from "waiting" — today both
   read as `pending` forever.
5. C6 and C12, which need control of the clock.
