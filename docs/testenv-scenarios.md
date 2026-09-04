# Test environment scenario matrix

Scripted multi-peer scenarios for `devtools/testenv/`, driven through the same
HTTP API (`api.py` → `actions.py`) the Flutter client calls. Each row is a
sequence of client actions and the state every peer must converge on.

This is the honest half of the test story. `tests/` uses the in-process
`TestTransport` shim: instant, synchronous, ordered delivery. Everything here
runs over real RNS Links between independent OS processes, so it covers what
the shim cannot, path resolution, delivery ordering, retry queues, truncated
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
| **Friends** | add friend (nickname/note), update friend, remove friend, send friend request, accept request, decline request |
| **Direct messages** | open conversation, send direct message, list conversations, enable propagation node, pin/unpin outbound node, collect held mail |
| **Channel** | create public, create invite-only, list discovered, join discovered, leave |
| **Server** | create server, create channel in server, invite to server, leave server |
| **Membership** | send invite, accept invite, decline invite, kick, promote to admin, demote |
| **Permissions** | edit channel perms (`send_message`, `invite`, `kick`, `manage_roles`, `manage_channel`, `create_channel`, `full_sync`), edit server perms |
| **Messaging** | send message, reply to message, send image, add reaction, remove reaction, import custom emoji |
| **Files** | share a file with a message, start a download, read a download's state, read the bytes back, read the store's usage, dial a holder's file plane directly (a harness hook, so a non-member's request can reach the wire at all) |
| **Lifecycle** | go offline (link drop), go online, kill process, start process, restart, reset tester, kill/start hub |
| **Link** | set profile, the names `link_profiles.py` actually defines: `broadband`, `satellite`, `serial` (9600), `lora_fast` (SF7), `lora_long` (SF10), `packet_radio`, `lossy` (15% loss), `custom` (explicit bitrate/latency/jitter/loss) |

## Observable vocabulary

What assertions read. Everything is polled with a timeout, real links, no
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
| Conversations | `GET /dms` → conversation hash, peer, unread |
| Friend requests | `GET /friends/requests` → `incoming` / `outgoing` |
| Propagation node | `GET /propagation` → selected node, nodes heard, transfer state |
| Link | `GET /net/status`, orchestrator `GET /status` |
| Network map | `GET /network/map` → nodes (identity, kind, hops, quality, path detail), edges, interfaces |
| File card | message rows → `file: {name, size, hash, state, progress, reason}`, or null |
| Download | `GET /channels/{h}/files/{hash}/fetch` → state, progress, chunks held, current holder |
| File bytes | `GET /channels/{h}/files/{hash}` → the file, 404 until this node holds all of it |
| File store | `GET /files/usage` → bytes held against the own, received and partial budgets |
| Events | the `/ws` event socket, collected live → event types and their counts |

**Convergence** is the workhorse assertion: named peers hold identical message
ID sets, identical rosters with identical roles, and identical reaction counts
for a channel.

## Timing rules

Getting these wrong produces phantom failures.

- **The testenv announces far more often than the real app.** `worker.py` (what
  the orchestrator launches) runs the heartbeat at 10s; the real entrypoints
  re-announce every `REANNOUNCE_INTERVAL_SECS` (3 h, `trenchchat/network/
  router.py`). (`backend_core.start_heartbeat` defaults to 1.5s, which only
  `smoke_test.py` uses.) `PeerAnnounceHandler` fires `on_peer_appeared` on
  *every* announce, not just transitions, so anything piggybacking on a peer
  announce (pending flush, sync request) happens ~1000× faster here than in
  production, and announce-driven sync requests are additionally spaced by
  `ANNOUNCE_SYNC_COOLDOWN_SECS` (120s) per (channel, peer). Any scenario whose
  result depends on that trigger must record time-to-converge, not just
  convergence, and be read against a three-hour worst case.
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
  and `"serial9600"` (neither exists) and three degraded-link scenarios ran
  unshaped while reporting they had exercised a bad radio. `set_link_profile()`
  now raises on rejection *and* reads `link_summary` back from the orchestrator,
  and every scenario reports the shaping it actually ran under.

## Matrix

⚠ marks a row probing a suspected gap; the expected result is what the code
currently implies, and the scenario exists to confirm it.

### `public`: Public (open-join) channels

| ID | Peers | Actions | Expected result |
|---|---|---|---|
| public1 | A,B,C,D | A creates public channel | B, C, D each list it under discovered, none subscribed |
| public2 | A,B | A creates; B joins | A's subscriber set = {B}; B receives the signed subscriber list; B's roster view includes A |
| public3 | A,B,C,D | A creates; B, C join; A sends 3 | B and C hold all 3; D holds none |
| public4 | A,B,C,D | B (subscriber, not owner) sends 1 | A and C hold it; D does not, recipients are the subscriber set plus self |
| public5 | A,B,C,D | A creates; B, C join; A sends 5; **then** D joins | ⚠ **Confirmed.** D holds 0 at the instant it joins, public join calls `subscription_mgr.subscribe()` only, no `channel_joined` callback, so nothing requests sync. Backfill lands on A's next peer announce: measured 1.0s and 9.1s on two runs, tracking the 10s heartbeat phase. Scales to a 60s worst case in the real app |
| public6 | A,B,C,D | A6a: A creates public, grants `full_sync` to member, sends 5, D joins. A6b: identical without `full_sync` | ⚠ **Confirmed.** Both channels backfilled all 5 to D, with and without the grant. Public channels never open tenure, so `has_any_tenure` is false and tenure filtering (the only thing `full_sync` gates) never engages |
| public7 | A,B,C | B leaves; A sends 2 | A removes B from subscribers; C holds both; B holds neither |
| public8 | A,B,C,D | All 4 joined and the subscriber set has converged; each sends 2 in turn | All four converge on 9 messages (a seed plus 8). Roster settle measured at 0.5–4.0s |
| public9 | A,B,C,D | A (owner) leaves its own channel, then C sends | C's message still reaches B and D; the subscriber lists they already hold are unaffected by the owner leaving. The departed owner does not receive it and stays unsubscribed |
| public10 | A,B,C,D | B, C join; C goes offline; D joins (C misses the broadcast); C returns | C learns about D and its next send reaches D. Recovery measured at 0.5s, 1.0s and 18.1s across runs, LXMF's own retry backoff, not an application-level repair |
| public11 | A,B,C | B leaves; A sends; B rejoins; A sends again | ✅ The round trip public7 stops halfway through: the post-return send reaches B again, and the message B missed while away followed by backfill on every run (2.0–8.1s). 4/4 runs |

### `invite`: Invite-only channels and membership

| ID | Peers | Actions | Expected result |
|---|---|---|---|
| invite1 | A,B,C,D | A creates invite-only | Nobody sees it in discovered, `announce_channel` refuses invite-only regardless of the discoverable flag |
| invite2 | A,B | A invites B; B accepts | B's pending invite clears; member-list doc lands; A and B rosters identical (2 members, owner+member); B's tenure opens |
| invite3 | A,B | A sends 3; **then** invites B; B accepts. B3a: member role lacks `full_sync`. B3b: A grants `full_sync` to member first | B3a: B holds 0 backlog; tenure filtering drops rows from before B's join. B3b: B holds all 3. This is the real `full_sync` test (public channels can't show it; see public6) |
| invite4 | A,B,C,D | A invites B, C, D; all accept | All four rosters identical: 4 members, A owner, rest member |
| invite5 | A,B,C | A invites C; C declines | C is not a member; A, B rosters unchanged; nothing sent on decline |
| invite6 | A,B,C,D | All 4 members; A kicks C | B, D, A rosters drop to 3; C's local membership clears and its pending outbound for the channel is cancelled; a message C sends after is dropped by A, B, D |
| invite7 | A,B,C,D | A promotes B to admin; B invites D; D accepts | All four rosters identical, B `admin`, D `member` |
| invite8 | A,B,D | A demotes B to member; B invites D | Join request rejected, `_handle_join_request` checks INVITE against B's current role; D never joins |
| invite9 | A,B,C | A revokes `send_message` from member role | C's message is dropped by A and B (and by C's own outbound guard); B, still admin, sends fine |
| invite10 | A,C | C calls `/roles` with `remove_members=[A]` | ✅ `{"ok": false}`, no document published, rosters unchanged on every peer, adversarial path, GUI bypassed |
| invite11 | A,B,C | A grants `kick` to member; C kicks B | Grant refused, not stored; C's kick returns `{"ok": false}` and B stays in every roster. 93s |
| invite12 | A,B,C,D | A promotes B; A and B both publish a roster change within ~1s | Both documents validate against stored state; final rosters identical on all four; no split-brain |
| invite13 | A,B,C | C (member) attempts `/channels/{h}/permissions` | `{"ok": false}`, lacks `manage_channel`; stored perms unchanged everywhere |
| invite14 | A,B,C,D | A promotes B to admin; B kicks D | ✅ D dropped from A, B and C's rosters; D's later send rejected by all three. 4/4 runs, 39–45s. The rank invite6 and invite11 leave untested, an admin is a trusted signer, so the granted `kick` holds |
| invite15 | A,B,C | A grants `manage_channel` to admin and promotes B; B revokes `send_message` from member, then re-grants it | ✅ Both admin-signed documents applied everywhere: C silenced after B's revocation (its send rejected by A), then heard again after B's re-grant. 4/4 runs, 40–48s. The working mirror of invite13's refusal, and the re-grant invite9 never covered |
| invite16 | A,B,C,D | A grants `invite` to member; C (member) invites D; D accepts | ⚠ **Confirmed, worse than predicted, 4/4 runs.** The token verifies and C honours D's join request, but the admission document (signed by a plain member) is rejected by every peer *including C itself*, since `publish_member_list` applies its own document through `_accept_document`. D's token is spent, every roster is unchanged, and D ends holding no roster at all. invite11's gap, on the invite path. Re-confirmed unchanged after the second audit pass |
| invite17 | A,B,C | C (member) leaves the invite-only channel | ⚠ **Confirmed.** The departure is invisible: `leave_channel()` unsubscribes locally and notifies only the creator's *subscriber* set, and no member-list update is published, so every roster, C's own included, keeps listing C, and senders keep addressing it. Only C's `is_subscribed` gate goes quiet: it received nothing after leaving. 4/4 runs |
| invite18 | A,B,C | A kicks C, then re-invites; C accepts | ✅ A kick revokes C's outstanding tokens at every peer, but `invite_revoked_at` is a moment rather than a flag, so the fresh invite issued after the kick readmits C to every roster and A's next send reaches it. Moderation stays reversible. 4/4 runs, 24–36s |
| invite19 | A,B | B leaves an invite-only channel, is removed, then re-invited | ✅ **Was a defect.** Leaving drops the subscription and keeps the `channels` row, and the subscribe on re-admission was gated on that row being *absent*, so B was readmitted to a channel that never returned to its own sidebar, and every listing filters on the subscription. Fails without the fix (63s, timing out on the missing channel). 5/5 runs, 3–9s |
| invite20 | A,B,C | B killed; A promotes C; A restarted; B restarted | ✅ **Was a defect.** A membership document is sent once and the queue holding one for an unreachable peer is in memory, so restarting the sender took the only remaining copy with it, B stayed behind on every other roster with no way back but a fresh invite. An announce now re-sends the current document to a member, version-ordered so a peer already current ignores it. Fails without the fix (103s). 5/5 runs, 13–19s |

#### invite11: a grantable permission that could not take effect

`kick` and `manage_roles` used to be grantable to any role, `ALL_PERMISSIONS`
offered them, the permissions dialog exposed them, `has_permission` honoured
them, and `update_membership` let the change through. So a member granted
`kick` got a successful `/roles` call and a published member-list document.

Every recipient then discarded it. `_validate_document` builds
`trusted_signers` from the stored document's `admins | owners`, so a plain
member is not a recognised signer no matter what permissions they hold. The
kick took effect on the actor's own device and nowhere else.

Two layers disagreed about what a permission means: the permission system
treated `kick` as role-independent, the document layer ties signing authority
to admin/owner.

**Resolved by narrowing the permission rather than widening signing
authority.** Removing someone from the member list strips every permission
they had, so `kick` is the authority to unmake other people's, granting it to
the base role would let every member do that to every other, which is the
opposite of what the failing scenario asked for. `manage_roles` is restricted
with it, because promoting yourself is how you would grant yourself `kick`.
Both are dropped from the member role on read and on write, so a grant is
refused rather than stored, and neither client offers the checkbox.

Worth noting what this does to **invite10**, which asserts a member *cannot*
kick the owner: it now passes for the right reason on the narrower rule, and
invite7 (an admin using `kick` legitimately) is what rules out the "the
endpoint refuses everything" reading invite11 used to cover.

invite16 later confirmed the same disagreement on the invite path (a member's
`invite` grant is honoured end-to-end right up to the document layer, where the
admission is rejected by everyone including the inviter itself), and invite14
and invite15 pin the boundary from the other side: the same grants demonstrably
work at admin rank, so the gap is specific to grants below admin, not to the
grants themselves.

### `sync`: Offline behavior and sync

The reason this environment exists. All three sync mechanisms only run on a
degraded or interrupted link.

| ID | Peers | Actions | Expected result |
|---|---|---|---|
Built: sync1–sync5, sync7–sync11. C6 (deep-sync cooldown) and C12 (a 7-day-old window)
need control of the clock and stay deferred.

| ID | Peers | Actions | Expected result |
|---|---|---|---|
| sync1 | A,B,C | B goes offline (link drop); A sends 3; B goes online | ✅ B receives all 3, in 1.0–5.0s |
| sync2 | A,B,C,D | B offline; A sends 3; A offline; B online (only C, D reachable) | ✅ **Fixed**, after three causes. 12/12 after the last one; recovery is 26–41s when the first ask is served and ~120s when it takes a retry. Was failing half of all runs. See below |
| sync3 | A,B,C | Same as sync1 but B is **killed and restarted** instead of link-dropped | ✅ B ends with its own history plus what it missed, in 3.5s, via the cold path |
| sync4 | A,D | D offline; A sends 60 (> `MAX_RESPONSE_MESSAGES` = 50); D online | ✅ D ends with all 60 and `state == synced`, in 18.1s, the truncated batch does chain its follow-up |
| sync5 | A,B,C | B offline for messages 1–5; B online, C offline for 6–10; C online | ✅ Both end with all 10, in 10.6s, per-(channel, peer) watermarks hold up |
| sync7 | A,B | B offline across a batch, then back; watch the sync state | ✅ Settles on `synced` with every message present |
| sync10 | A,B,C,D | Hub killed (total partition); each peer sends 1; hub restarted | ✅ All four reconcile in 12.1s once the hub returns |
| sync8 | A,D | D joins without `full_sync`; A grants `full_sync` to member | ✅ The backlog arrives 3.0s after the grant, without D restarting |
| sync9 | A,B,C | A kicks C; C requests sync | ✅ C's transcript stays frozen at the kick |
| sync11 | A,B,C,D | All 4 offline simultaneously, each sends 2 locally, all come online | ❌ **Still fails.** One watermark defect found and fixed (below); the scenario itself reconciles only sometimes. 1 pass in 5 runs |
| C12 | A,B | B offline past `SYNC_WINDOW_SECS` (7 days, clock-shifted); B online | Deferred, needs clock control |

#### sync11: still open

A four-way partition reconciles only sometimes: **2 passes in 7 runs**, and the
missing rows are always the *first* message each peer wrote in isolation, never
the second. Which peers lose which rows moves between runs, so it is not a
deterministic defect.

One cause was found here and is fixed: `sync_progress` was written from both
directions against the same key, collapsing the responder's trust-horizon floor
so history older than a requester's watermark was stranded on both sides.
Pinned by `TestResponderAcquiresOlderHistoryLater` in
`tests/test_sync_multipeer.py`. It was **not** the whole story, one passing run
was recorded as a fix while the scenario still failed four runs in five, which
is where the "a single pass is not a fix" rule came from.

Ruled out: author-signature rejection. `messages_rejected` reads 0 for every
peer in every observed run, and integrity1 independently shows that relayed
history from a departed author verifies fine.

Leading hypothesis: the deep-sync cooldown. A four-way reconcile needs several
rounds (a peer can only relay what it has itself received) and the cooldown
serves one deep request per pair per 60s. sync2 turned out to be exactly that
shape, so the retry tick added for it may help here too; unmeasured so far.

On a slow link it fails with the same fingerprint, pointing at a second
mechanism: `PEER_TRUST_HORIZON_SECS = 300` widens a responder's sweep by a
wall-clock constant, but how far back that must reach scales with how long the
exchange takes, 31.7s on broadband, 1141s at SF7. A fixed constant guarding a
link-speed-dependent window is the shape of the bug. Unverified.

#### sync2: fixed, after three causes

Kept for the last one, which is a design lesson rather than a bug fix: **a
silent refusal and a burst-only trigger deadlock each other.**

A responder's deep-sync cooldown refuses for 60 seconds without sending
anything, so the requester cannot tell refusal from a lost packet. Every
trigger to ask is an event (a peer announcing, or this node's own link
returning), and both fire in a burst and then stop, because Reticulum
suppresses announce replays for a destination it has already propagated. The
returning peer asked eight times in 31 seconds, every attempt landing inside
one cooldown window, then had nothing left to make it ask again; the window
expired 30 seconds later with nobody there to use it.

Neither behaviour is wrong alone. Anything added later that refuses silently
has to answer the same question: what will make the other side try again?

The three causes, all fixed and all covered by regression tests: nothing
noticed this node's *own* link returning (`connectivity.py`); the responder's
answer was dropped when it could not yet address the requester (`sync.py`'s
`_send_raw`, alone among the send paths in not even requesting a path); and
this one (`SyncManager.tick`). 12/12 after the last, recovering in 26–41s when
the first ask is served and ~120s when it takes a retry.

### `links`: Degraded links

| ID | Peers | Actions | Expected result |
|---|---|---|---|
| links1 | A,B,C,D | A on `lossy` (62.5 kbps, 250±150ms, 15% loss); A sends 10 | ✅ All three peers converge in **101.7s** |
| links2 | A,B | B on `serial` (9.6 kbps, lossless); A sends 5 | ✅ Delivered in 3.0s. Slow-but-lossless is not a stall |
| links3 | A,B,C,D | A broadband, B `satellite`, C `lora_fast`, D `serial`; each sends 1 | ✅ Converged in 4.5s across four differently-shaped links |
| links4 | A,B | B on `lossy`, dropped offline mid-burst | ✅ Caught up all 10 in 28.2s |
| links5 | A,B,C,D | All four on `lora_fast` (5.5 kbps) | ✅ Converged in 12.6s |
| links6 | A,B,C | B on `lossy`; A sends 15, **the link never drops** | ✅ Converged in 19.6s. The README's stated reason for the lossy profile: retry and hints reached the way a bad radio does, not by killing a link |
| links7 | A,B | B on `packet_radio` (AX.25 1200 baud, 5% loss) | ✅ Three messages in 10.1s. The worst link the app claims to support for text |
| links8 | A,B | B on `lora_long` (SF10, 1.0 kbps) | ✅ Three messages in 11.1s |
| links9 | A,B | `custom` profile with explicit bitrate/latency/jitter/loss | ✅ Applied exactly as asked (32 kbps · 120±20ms · 8% loss); converged in 2.0s |
| links10 | A,B | Retune B broadband → `serial` mid-run | ✅ Shaping applies live: 0.5s then 1.0s, no restart needed |

### `servers`: Servers

| ID | Peers | Actions | Expected result |
|---|---|---|---|
| servers1 | A,B | A creates a server with 3 channels; invites B once | ✅ One invite admits B to the server and all 3 channels; a send on the *third* proves it |
| servers2 | A,B,C | A grants `create_channel` to admin, promotes B; B creates a channel | ✅ Every member receives it via the re-published server document |
| servers3 | A,B | A edits server permissions | ✅ Mirrored into every child channel; a per-channel override returns 409 and the mirror survives |
| servers4 | A,B,C | B leaves the server | ✅ B unsubscribed from every channel in it; A and C unaffected |
| servers5 | A,B,C | A kicks C from the server | ✅ C loses every channel at once and its later send is rejected |
| servers6 | A,B | Server-level `full_sync` grant, then invite B with backlog in 2 channels | ✅ Both channels backfilled (1.0s, 0.0s), server-scoped tenure resolves per channel |
| servers7 | A,B | A invites B **to a channel inside the server**, not to the server | ✅ **Was the reported defect.** `publish_member_list` normalises to the owning scope, so the document answering a join request is always the server's, but the invite named the channel, so B anchored a hash no document would ever arrive for and could not trust the one it got. B's sidebar stayed empty while A's roster showed B admitted. `send_invite` now normalises the same way. Fails without the fix (3s). 5/5 runs, 2–7s |
| servers8 | A,B | A creates a server with no channels and admits B | ✅ A server's visibility is gated on membership rather than a subscription, and nothing asserted that positively before. **Does not** cover the live sidebar refresh: this polls `GET /servers` each time, so it cannot see a missing `server_joined` event the way a running client with a cached sidebar does, that half is the Flutter widget test. Passes with and without the event. 5/5 runs, 3–10s |

### `social`: Reactions, emoji, presence, identity

| ID | Peers | Actions | Expected result |
|---|---|---|---|
| social1 | A,B,C,D | Public channel, all joined; B reacts to A's message | ✅ A, C and D all show count 1, owner included |
| social2 | A,B,C | B reacts, then removes the reaction | ✅ Clears on every peer |
| social3 | A,B,D | D offline; B reacts to A's message; D online | ⚠️ **Prediction refuted.** D *does* recover the reaction, in 14.1s and 15.2s across runs, LXMF's own retry redelivers the broadcast. Reactions have no application-level backfill, but they do not need one for a peer whose path is known |
| social4 | A,B,C | B goes offline via link drop | ✅ Presence flips on the beacon timeout, measured at 59.8s |
| social5 | A,B,C | A sets an avatar, then removes it | ✅ Propagates to both peers; the removal waits out `SEND_RATE_LIMIT_SECS` (60s, answered 429 until it elapses) then clears |
| social6 | A,B,C | A changes display name | ✅ Propagates; directory search finds the new name |
| social7 | A,B,C | A adds B as a friend with a nickname | ✅ Local only, C sees nothing, B is not notified |
| social8 | A,B,C | B replies to A's message; C reacts to the reply | ✅ `reply_to` and the reaction target resolve identically on all three |
| social9 | A,B,C | Same conversation on an **invite-only** channel: B replies to A's message; C reacts to the reply | ✅ Resolves identically there too, 4/4 runs in 23–29s, member-list fan-out carries replies and reactions the same way the subscriber set does |
| social10 | A,B,C | B imports a custom emoji and reacts with it | ✅ A and C converge on the count and fetch the image over `MT_EMOJI_REQUEST`, in 0.0–3.5s after the count landed. 4/4 runs |
| social11 | A,B | A is slowed to a real client's announce cadence; B is wiped so it has heard nobody; A invites B | ✅ **3/3 runs, 20–25s.** Found a real defect first; see below. Fails in 64s without the fix |

### interop: direct messages with clients that are not TrenchChat

These use `lxmf_peer.py`, which imports RNS and LXMF and nothing else. Every
message it sends is a plain LXMessage with no fields at all, which is what
Sideband, NomadNet or anything else speaking LXMF sends. Nothing else in this
suite can show interoperability, because everything else is TrenchChat talking
to itself.

| ID | Peers | What it does | Result |
|----|-------|--------------|--------|
| interop1 | A + bare client | The bare client sends a plain LXMF message to a tester holding it as a friend | ✅ **5s.** Lands as an ordinary direct message, and the conversation is correctly marked as *not* TrenchChat |
| interop2 | A + bare client | The same message with the friendship removed | ✅ **19s.** Refused. Sending without the envelope is exactly what an attacker would do, since that is the half carrying a signature, it buys nothing, because the gate reads the identity LXMF authenticated |
| interop3 | A + bare client | A tester sends a direct message; the bare client reports what it received | ✅ **48s.** The text arrives in the ordinary content, and the only fields present are `0xFB`/`0xFC`, LXMF's own custom-payload fields. No TrenchChat field numbers reach a foreign client |
| interop4 | A + bare client | The bare LXMF client messages A, which has **not** added it; A accepts | ✅ **Was a real gap.** The message used to be dropped where the gate refused it (with LXMF having already proved the packet, so the sender was told it was delivered) and a client that cannot send `MT_FRIEND_REQUEST` had no other way to ask. It is now held as a request carrying its text, grants nothing until accepted, and is filed into the conversation on accept. Fails without the fix (64s). 5/5 runs, 3–5s |

### `restart`: Restart, persistence, ordering

| ID | Peers | Actions | Expected result |
|---|---|---|---|
| restart1 | A,B,C,D | Public channel with B, C joined; **A restarts**; D joins | ✅ **Fixed.** Was: A held `[B, C, D]` while B and C stayed at `[A, B, C]` and B's send never reached D. Now all four views agree, B learns about D in 1.0s. Promoted to strict as a regression guard |
| restart2 | A,B,C,D | Full history and roster built; all four killed and restarted | ✅ Identities, messages and the invite-only roster all survive |
| restart3 | A,B | A invites B with no path warm-up | ⚠️ **Confirmed.** The first invite is dropped silently; 2 attempts needed |
| restart4 | A,B,C | A admits C and sends a chat message immediately after | ✅ Landed in 0.0s on this run; the race is real but did not bite. Kept as a probe |
| restart5 | A,B,C | A single tester is reset (data wiped, same slot) | ✅ Returns as a new identity holding nothing, and the owner keeps a subscriber row for an identity that will never reappear |

### `voice`: Live group voice

Voice has two planes and the pytest suite only reaches one. Signalling is
LXMF, but frames travel over a full mesh of real RNS Links, one per
participant pair, each authorised on its own VP_HELLO/VP_ACCEPT handshake.
`tests/fake_voice.py` doubles that transport; nothing under `tests/` dials a
real link, and `smoke_test.py` covers exactly one pair. This family is the
three- and four-peer cases, and the states `docs/voice.md` is explicit about.

| ID | Peers | Actions | Expected result |
|---|---|---|---|
| voice1 | A,B,C | All three join voice on a public channel | ✅ Full mesh in 2.0–3.0s; every peer `streaming` to every other |
| voice2 | A,B,C,D | Three in voice, then D joins | ✅ D learns all three occupants and they learn D, roster in 0.0s, mesh in 3.0s. Exercises the unicast `voice_state` reply path three times over |
| voice3 | A,B,C | C leaves voice cleanly | ✅ Dropped from every roster in 0.0s; C reports no session |
| voice4 | A,B,C | C is **killed** mid-call, sending no `voice_leave` | ⚠️ Expires only on the roster TTL: **27.6s** here, and the testenv shortens that TTL to 30s from the production **180s**. A crashed participant lingers up to 3 minutes in the real client |
| voice5 | A,B,C | C's link drops mid-call | ⚠️ Kept in the roster rather than hidden, as the doc requires, but downgrades to `connecting`, not `unreachable`, and was still `connecting` 15.1s later. A UI would show "connecting…" indefinitely for someone who is gone |
| voice6 | A,C | Member without `voice_chat` joins voice; then granted | ✅ Refused, then admitted. The mirror pair, a refusal only means something if the grant demonstrably works |
| voice7 | A,C | Channel whose permissions predate voice (no `voice_chat` key) | ✅ Fails closed for the member, owner always passes, re-saving permissions admits the member |
| voice8 | A,B,C | A revokes `voice_chat` from member while C is streaming | ✅ Cut off in **0.5s**, matching the doc's ~1s re-authorisation sweep. Same claim `test_adversarial.py` makes against the transport double, here over real links |
| voice9 | A,B,C | All three stream the test tone for 8s | ✅ Each peer receives from both others: **384 frames, 0.0% loss, ~2ms jitter**. The full-mesh version of the smoke test's single pair |
| voice10 | A,B,C | Five chat messages while the voice mesh streams | ✅ Text delivery unaffected (0.0s), despite sharing the interface |
| voice11 | A,B | Voice over a `lora_fast` link, with the tone measured | ⚠️ **Two findings**: the link reports `streaming` and `loss_pct` reports ~6% while only ~8% of frames arrive. See below |
| voice12 | A,B,C | B mutes, then unmutes, mid-call | ✅ Both peers' rosters flip `muted` within 3.0s each way, the coalesced `voice_state` refresh carries it. 4/4 runs |
| voice13 | A,B,C | All three stream the tone for 10s, measured as a listener hears it | ✅ All six directions **50.2 fps and 0 starved playout ticks**, 5/5 runs. See below |

#### voice11: the quality metric cannot see a starved link

`docs/voice.md` says voice "is not viable over LoRa or packet radio" and that
the UI should surface that rather than mask it. Both halves are worth checking,
and the run says the first is right while the second does not currently work.

Over `lora_fast` (SF7, 5.5 kbps) the mesh link **comes up and reports
`streaming`**, then delivers 24–46 frames against ~384 expected across three
runs (6–12% of the audio) at 100–240ms jitter. Voice is indeed unusable, but
nothing the UI is told says so:

- `link_state` reads `streaming`, the same value a perfect link gets.
- `loss_pct` reads **0.0–7.7%**, because it counts gaps between frames that
  *arrived*. Frames that never reach the wire are not gaps, so a link
  delivering 8% of the audio reports a loss figure implying 94% got through.

`docs/voice.md` designates `rx_quality`'s `loss_pct` as "the backend signal for
a per-peer connection-quality indicator in the UI". A UI built on it would show
a healthy connection on an unusable call. The signal that does show it is the
delivered rate (frames received per second against the 50/s the codec
produces), which `frame_stats()` now exposes as `rate_fps` (added for voice13),
alongside the listener's own starved playout ticks.

Neither is a crash, so voice11 stays a probe. Both are worth a decision before the
voice UI ships a quality indicator.

#### voice13: what a listener hears, not what arrived

voice9 measures loss and jitter, and both are clocked by sequence number: a
sender that emits every frame it should, only slowly, scores 0% loss and ~0 ms
jitter. The listener's jitter buffer drains anyway, starves, refills to its 80 ms
target, plays, and starves again, the stream cutting in and out, with every
metric reading clean.

That was not hypothetical. `TonePipeline._loop` slept the whole 40 ms packet
interval and *then* generated and sent the frames, so its real period was 40 ms
plus the work: 47.4 fps measured here, worse on a slower machine. With that loop
restored, voice13 fails at **47.3–47.5 fps and 9–10 starved ticks per listener
per 10 s**, 180–200 ms of dead air from each sender every 10 s, which is
exactly what a person on the desktop client reported hearing. Anchoring the
loop to a monotonic schedule fixes it; the rate floor is what catches it,
since the starvation it causes (1.8–2.0%) sits right on the 2% ceiling.

The scenario shares its floors with `smoke_test.py` (95% of nominal, ≤2% starved
ticks), so a pair and a mesh mean the same thing by them.

### `api`: The API surface

Every other family drives the backend through `devtools/testenv/api.py` and
takes its access control on trust. This family tests that control directly,
because it is the only thing between a tester's identity and any process (or
any web page) that can reach the port. Added after the August security audit
(PR 52) gave the API a token; before it, this surface was unauthenticated with
wildcard CORS. The access-control scenarios touch no mesh and run in about four
seconds; api5 does, because the map's change event only exists when a real peer
announces.

| ID | Peers | Actions | Expected result |
|---|---|---|---|
| api1 | A | Call four reads, one mutation and the event socket with no token | ✅ Every route 401; the socket refused with HTTP 403; the identity unchanged |
| api2 | A | A wrong token, then the right one as a header, a bearer and `?token=` | ✅ Wrong token 401; all three routes 200; the socket opens on both the header and the query parameter |
| api3 | A | Open the event socket with `Origin: http://evil.example`, then with its own | ✅ Foreign origin refused 403, own origin accepted. The socket checks this itself, a browser applies neither CORS nor same-origin policy to a WebSocket handshake |
| api4 | A,B,C,D | Present each tester's token to a different tester's API | ⚠️ **One token for the whole environment**, as designed: B accepts A's, C's and D's (all 200), and the orchestrator's unauthenticated `/config` serves it. Recorded so the harness never claims per-identity isolation it does not have |
| api5 | A,B | A opens the event socket, B re-announces; wait for `network_map_changed`, then read `/network/map` | ✅ 5/5. The event reaches A in 0.5–2 s of B's announce, or on the next heartbeat (16 s) when A already held everything B just said; one event per burst, never a stream. B is on the map by the time it arrives (0.0 s), at 2 hops through the hub, online, with its interface named. The TrenchChat flag lags 0–15 s behind, because the delivery announce and the `trenchchat.user` announce are two packets |

### `dm`: Direct messages between mutual friends

A conversation has two participants and nothing else: no roster, no announce,
and no third peer who could serve its history later. Two things follow, and
both are why this family exists rather than living in `tests/`.

The gate is enforced at each end independently, so a one-sided friendship must
produce *nothing* on the far side, and over a real network "refused" and
"still in flight" look identical for a while, which is what `hold_for` is for.
And a message to an absent friend has no member to backfill it, so it goes to a
propagation node instead: a third process really acting as one, a peer that
really leaves, and real time passing before it returns and collects.

| ID | Peers | Actions | Expected result |
|---|---|---|---|
| dm1 | A,B | A requests, B accepts, one message each way | ✅ Handshake and both messages in **2.0s**. Both peers derive the same conversation address with nothing negotiated |
| dm2 | A,B | A adds B; B never adds A; A sends | ✅ Nothing lands at B across the full **15s** hold, and B's own send back is refused with **403** before it leaves. One-sided trust delivers nothing in either direction |
| dm3 | A,B | Mutual friends, one message, then B removes A and A sends again | ✅ First message held, second never arrives (15s hold), and the existing transcript survives the unfriending. **26s** |
| dm4 | A,B,C | C runs a propagation node; B goes offline; A sends; B returns and collects | ✅ **139s.** The path the whole propagation layer exists for, a channel would have been caught up by any member, and a conversation has none. Asserts the sender's own copy reads `propagated` before waiting: see below for why |
| dm5 | A,B,C | Two nodes announce; A auto-selects, pins the other, then reverts | ⚠ **Probe.** Pinning and reverting both work. Whether *hop count* separates two nodes is not shown here: the harness's topology is flat, so both are one hop and the tie is broken by which was heard last |
| dm6 | A,B,C | B's **process is killed**; A sends; B restarts and is never told to collect | ✅ **3/3 runs, 132–135s.** Found and fixed a real gap; see below. This is the case a person actually hits: nobody presses "collect" |
| dm7 | A,B | Mutual friends, one message, then **both** peers restart | ✅ **19s.** Friendship and transcript both survive, and a new message flows without running the handshake again |
| dm8 | A,B | A JPEG attachment and a reaction inside a conversation | ✅ **12s.** The attachment arrives unstripped and fetchable (200), and the reaction reaches the conversation's only other member |

### `integrity`: authorship and attachments

A synced message reaches you from a peer who did not write it. LXMF
authenticates the peer that handed it over and nothing else, so PR 52 gave
every message its author's own signature, and a receiver that cannot verify
one drops the message. That makes verification a *delivery* dependency, not
only a security check: a receiver who cannot resolve an author's public key
loses honest history with nothing but a log line to show for it.

| ID | Peers | Actions | Expected result |
|---|---|---|---|
| integrity1 | A,B,C,D | C offline; A sends 2; A's process killed; C online and backfills from B | ✅ C accepts and correctly attributes history whose author is gone, in 11.1s. A relay's own signature is not what C checks |
| integrity2 | A,B,C,D | B owns the channel; A sends, then dies; D wiped to a **new identity**, joins and backfills | ✅ **Was a confirmed gap, now fixed.** As a probe it measured 4.0s for the live owner's message and *never* for the dead author's. Responders now send each batch's author keys, and D holds both. Strict since |
| integrity3 | A,B,C,D | A sends a real 64×64 JPEG | ✅ All three receivers hold it with `image_stripped: false` and can fetch the bytes. The signature covers the attachment, so the two travel together |
| integrity4 | A,B,C,D | A sends a 68-byte PNG declaring 20000×20000 (400M pixels) | ✅ Delivered as text with no attachment on all four. The sender's own API is the gate: `prepare_image` fails closed rather than forwarding bytes it could not re-encode |

#### What dm6 found: a green run that proved nothing

dm6 passed the first time it was written, and the pass was worthless. The
message reached B, but through `Messaging`'s ordinary pending queue, which
re-sent it directly when B came back, not through the node at all. From the
outside the two are indistinguishable: a message arrives either way.

Asserting the sender's own delivery state (`propagated`) before waiting is what
separated them, and with that in place dm6 failed for four consecutive rounds
of investigation. Each round moved the wall back one step:

1. **B never re-asked after the first empty answer.** The collector treated one
   completed transfer as "settled" and dropped to a five-minute cadence, but a
   completed transfer only means the node answered, and a sender can still be
   uploading as we arrive (LXMF makes them generate a proof-of-work stamp
   first, ~5s here). Fixed by pacing on a settling *window* after coming back,
   not on whether an answer had been received.
2. **A restarted peer had no node to ask.** A propagation node announces when
   it is switched on and never again on a timer, so a client that forgot its
   node on restart could never hear of one. The selected node is now
   remembered across restarts (`propagation_node.last_selected`).
3. **The node was chosen before anything was listening.** `PropagationNodes`
   restores its selection during construction, so the collector's
   selection callback (registered a line later) never fired for a restored
   node, leaving the collector on its steady cadence. The collector now opens
   its settling window on first use instead of depending on that ordering.

None of the three is visible to pytest: all of them need a process that really
dies, a node that really holds a message, and real seconds passing. The
regression guards for the cadence itself live in `tests/test_propagation.py`,
where they are deterministic.

#### What social11 found: the dev environment hid a bug in the real client

A person testing by hand reported that invites from their client never reached
the testers, and that the testers never showed their client as online. Both had
one cause, and this suite could not have found it as it stood.

Every tester announces every 10s, so meeting one here is instantaneous. A real
client announces at startup and then every `REANNOUNCE_INTERVAL_SECS`, **three
hours**. Until a peer has heard that announce it cannot recall the sender's
identity, so LXMF cannot verify the signature, so `Router._quarantine_message`
holds the first message and drops it at `QUARANTINE_TTL_SECS` (300s). Nothing
released it except a full announce. Relaunching the client emitted one, which is
why relaunching made the missing invites appear, the tell that identified this.

The Qt client had already solved it (`main_window.py`'s
`_on_reannounce_debounced` says so in as many words), but that lives in the Qt
window, and the active Flutter client runs on `backend_core.Backend`, which never
got it. Two fixes, in different directions:

- **`FirstContactAnnouncer`** answers a peer the *first* time we hear them, so
  they can hear us back. Once per peer, not per announce: answering every
  announce leaves two idle clients replying to each other's replies for ever,
  while once-per-peer settles after exactly two announces.
- **`PathResponseHandler`** releases the quarantine when a sender's identity
  arrives as a path response, the quarantine already requests that path, but
  RNS only calls handlers that set `receive_path_responses`, so nothing was
  listening. This costs no airtime and would have delivered the invites with no
  relaunch at all.

Reproducing it needed the environment to stop being unrealistically chatty, so
testers now take a heartbeat interval (`POST /testers/{tag}/heartbeat`). Slowing
one tester to a real cadence and wiping the other is what makes the window
observable. Confirmed by disabling both fixes: the scenario fails in 64s.

#### Why a direct message looks like nothing in particular

TrenchChat's field numbers are its own and overlap LXMF's standard registry:
`0x02` is `FIELD_TELEMETRY` there, `0x06` `FIELD_IMAGE`, `0x0C` `FIELD_TICKET`.
Between TrenchChat peers that is harmless (both ends read them the same way),
and it is wrong the moment a message reaches a client that follows the
standard, which would read a display name as telemetry and a message id as an
image.

No message puts those numbers on the wire as LXMF field keys any more:
channels, sync, invites and voice carry their whole field dict msgpack-packed
inside `FIELD_CUSTOM_TYPE`/`FIELD_CUSTOM_DATA` under `trenchchat/1`, which the
standard sets aside for exactly this. A direct message is still the special
case (the one thing that can legitimately arrive at somebody else's client),
so its text rides in the ordinary content, an attachment in LXMF's own image
field, and only the TrenchChat extras sit in the envelope, under its own type.

interop3 is what keeps that true: it fails if any field number outside that set
appears in a message a foreign client received.

#### Why author keys travel with a synced batch

`verify_message` returns False for "we cannot check yet" exactly as it does for
"this is forged", and the caller drops the row either way. `resolve_author` can
only fall back to `RNS.Identity.recall()`, which needs an announce a departed
peer will never send again, so a channel's history was readable by whoever
was present to receive it directly, and progressively unreadable to everyone
who joined afterwards, one author at a time as people left the mesh.

A sync response therefore carries one deduplicated `{author: key}` map
(`F_AUTHOR_KEYS`, 0x71) alongside the batch, and a receiver caches a relayed
key only if it hashes back to the identity claiming it. That check is what
makes accepting a key from a relay safe: an identity hash is derived from its
public key, so a key that does not hash back simply is not that identity's.
The relay passes along public information it cannot forge; it vouches for
nothing.

One map per response rather than a key per row; a batch is a handful of
authors and up to fifty messages, and at 1 kbps the per-row form would cost
roughly the whole response over again. Storing keys in the roster was
considered and rejected: a roster lists *current* members, so a departing
author's key would be removed exactly when it became necessary, public
channels have no roster at all, and a whole-roster document is re-broadcast on
every membership change.

Still open, and smaller: a receiver that holds a message back says so only in
a `LOG_WARNING`. `messages_rejected` already reaches the sync status, so
surfacing "N held back, author unverifiable" is a UI change, not a protocol
one.

### `nomad`: Nomad Network page browsing and hosting

A tester that enables hosting announces a `nomadnetwork.node` destination and
serves its `nomad_pages` directory; a browser discovers it from the announce
and fetches over a real RNS Link via `Link.request`. This is the layer
pytest's `FakeNodeTransport` cannot touch: announce propagation, path
resolution, link establishment, and the request/response transfer itself.
The manual interop check (browsing a real pip `nomadnet` node and being
browsed back by it) is documented in `scen_nomad.py` and not automated.

Run against `nomadnet` 1.2.9 (rns 1.5.2) on 2026-09-01, joined to the testenv
hub, in both directions. Pages were fine from the start. Files were broken
both ways, and the shape is the reason: a node answers a `/file/` request
with `[open(path), {"name": ...}]`, which RNS delivers as an open handle on a
temp file it deletes the moment the response callback returns. We required
`bytes`, so every file from a real node failed as `bad_response`; and we
answered with raw bytes, which sends nomadnet's `file_received` down its
legacy `[name, data]` branch to do `basename(<int>)` and drop the download
with a `TypeError`. Both fixed, and re-verified by running nomadnet's own
`file_received` against our response. Pages remain plain bytes on both
sides; the handle-and-metadata shape is for files only.

A second parity pass on 2026-09-01 closed the remaining micron and browsing
gaps: `#!bg=`/`#!fg=` page colours, `` `{...} `` partials with their refresh
timers and `p:` reload links, the `anchor=` link variable, table block
arguments (`` `tc70 ``), section right-indent, the file name a node puts in
its response metadata, and browsing your own node. Each was checked against
nomadnet's own code rather than its documentation: the demo site's markup
is fed through upstream's `parse_partial`, its `#!bg=` header scan and its
table-argument reader, and all three read exactly what ours read. Two things
are worth knowing. A partial is an ordinary page request on the same link,
so nothing new reaches the wire and no scenario covers it separately. And
your own node is never dialled: RNS cannot link a destination to itself, so
`NodeBrowserManager._serve_loopback` reads the pages directory and answers
through the ordinary fetch machinery, which is also why a page of your own
is re-read on every visit rather than served from its `#!c=` cache.

Identify-on-connect was verified the same way, against a real nomadnet
1.2.9 node serving an executable page that echoes its `remote_identity`
environment variable, the same variable a forum like rns.recipes keys an
account to. Browsing anonymously, the page reported `ANONYMOUS`; after
enabling identify for that node, the very next request on the *same* link
reported our exact identity hash; tearing the link down and fetching again
identified the fresh link from the stored choice alone. So both models
work: nomadnet's directory checkbox (persisted, applies to the next link)
and MeshChat's fingerprint button (identifies the link already open). We do
not need nomadnet's disconnect-and-reconnect step, because `Link.identify`
is legal on an active link and the node reads `remote_identity` per request.

Turning it off is the half worth spelling out, because the obvious
implementation is wrong: a link cannot un-identify. The proof is sent once
and the node reads it on every request that link carries, so clearing the
stored flag alone would keep reporting the identity for as long as the link
lived, up to `NODE_LINK_IDLE_SECS`, and indefinitely while the user keeps
reading. `set_identify(..., False)` therefore drops the link, and the same
probe confirms the next page comes back `ANONYMOUS` rather than at the next
idle timeout.

It is opt-in per node and defaults to off, which is where the design choice
sits: identifying tells that operator, provably and permanently, that this
identity visited, so a bug that identified by accident is not recoverable by
turning the setting back off. The transport therefore fails closed: no
policy, a policy that raises, or a node the policy does not name all leave
the link anonymous, and `tests/test_node_transport.py` asserts each of those
three against a link that records every proof sent on it.

One deliberate divergence: nomadnet strips Unicode combining and format
characters from everything it renders, zero-width joiners included, which
also breaks emoji sequences. We strip only the characters that actually
mislead a reader (the bidirectional overrides and isolates, which let a
page reorder what is displayed so a link label reads as a destination it
does not name), and leave joiners alone.

| ID | Peers | Actions | Expected result |
|---|---|---|---|
| nomad1 | A,B | A enables hosting; B browses the node's index | ✅ B discovers the node from the announce and fetches the default `index.mu` in 0.5s. 4/4 runs |
| nomad2 | A,B | A page and a 16 KB file added to A's directory; A rescans; B fetches both | ✅ Rescan serves the new paths; page and file each fetch in 0.5s and the file round-trips byte-identical. 4/4 runs |
| nomad3 | A,B | B fetches once; A goes offline; B requests an uncached page; A returns; B fetches a new page | ⚠ **Confirmed.** The offline fetch never arrives and never hangs; the dial ladder gives up within its bounded backoff (browse accepted, nothing after 90s). After A returns, a fresh page fetch succeeds in 20.1s, tracking path re-resolution. Probe |
| nomad4 | A,B | B fetches A's index; a new page is written into A's directory; A's process is restarted; B fetches the new page | ✅ Hosting comes back from config on boot and the new page fetches in 0.5s over a fresh link. 4/4 runs. Note: it passes with the dead-link redial disabled too; RNS closes the link when the host dies, so this is not that fix's guard (`tests/test_node_transport.py` is) |

### `files`: Shared files in invite-only channels

A file message carries a manifest and never the bytes: name, size, SHA-256 and
chunk root, under 200 bytes, riding the ordinary message path. The bytes move
only when a member asks for them, over the file plane's own RNS Links on the
`files` aspect, one chunk range per request, every chunk verified against the
signed chunk root before it is stored. `tests/test_files.py` covers that
engine against a transport double. This family covers the three things the
double cannot produce: a real path to a destination, a holder that dies with a
transfer in flight, and what a request window that climbs and halves actually
costs on a radio.

It earned its keep immediately. **files1, the plainest row here, failed twice
for two different reasons before it passed once**, and neither was visible to
the 170 file tests under `tests/`: nothing on the mesh could resolve a path to
the file plane, and the concurrent-serve cap refused every range after the
first. files3 then found a third (holder choice treated a member it had not
heard from lately as one that was gone) and files8 a fourth (a parked download
with no way to try again). All four were fatal in production, all four are
fixed, and each has a regression test; see the findings below. The radio rows then
found two more between them: a holder chosen on presence alone, which sent every
download to the member holding nothing first, and a retry wait that doubled across
the evidence that the holder was answering. files9, the first row to ask with
more members than the caps allow, found a seventh on its first run: the per-link
serve rate limit was below what one download costs, so it refused every
downloader mid-transfer.

The request counts in the notes are read from the captured tester log
(`--tester-log`), so they are indicative rather than exact: worker stdout is
block buffered and a count can be short when a run ends.

The radio rows stay probes after this pass, and the ratios are the reason: four
finishes in five at SF7 and three in five under 15% loss are measurements of a
link, not a settled behaviour, and the runs that do not finish fail on RNS's own
response handshake rather than on anything this project decides. A strict row
here would report a bad radio as a regression. files10 is a probe for the same
reason and one more: what it measures is how one shared uplink is divided
between members who all want the same file, and that is a spread of numbers
rather than a behaviour.

| ID | Peers | Actions | Expected result |
|---|---|---|---|
| files1 | A,B,C | A shares a 2 MB file in an invite-only channel; B and C both ask for it | ✅ **6/6 runs, 8-19s**, of which 3.5s is the transfer. Both hold it byte for byte and neither holds a byte before asking. Fails at 0 chunks without either of the first two fixes below, which is how both were found |
| files2 | A,B,C | C offline through the share; B downloads; A's **process is killed**; C returns | ✅ **6/6 runs, 151-162s.** The check-1 proof on the file plane: C backfills the manifest by sync from B in 1.5-17.2s and pulls 2 MB from a member that never wrote the message. Its own fetch takes 123s, of which 120 are one stall timeout spent on the dead sender before the fallback |
| files3 | A,B,C | Same shape with a 20 KB file, asserting C holds nothing before it asks | ✅ **6/6 runs, 156-174s.** Manifest in 1.5-10.1s, `GET` 404 and an empty file store held for 15s, then the download completes. 20 KB is small enough that pushing it would have been cheap, and it is still not pushed |
| files4 | A,B,D | D, in no channel this file was shared in, drives its own file plane at A's with the real hash; B, who is a member, makes the identical request | ✅ **5/5 runs, 249-258s.** B is served the chunk list in 2.5s; D gets nothing twice over, and A's log names it. Almost all the time is D waiting: a refusal is silence, and silence costs the asker the 120s stall timeout |
| files5 | A,B,C | files1 at `lora_fast` (SF7, 5.5 kbps), 200 KB, B downloading, testers slowed to a 60s announce cadence | ⚠ Probe, and the answer is still two numbers. At 32 KB chunks with holder choice reading the path table, **four of five runs finished, in 495.6-651.0s** (314.6-413.2 B/s, 6 requests, none lost); the fifth stopped at **4 of 7 chunks** after losing 4. The same five runs before that fix were 506.1-1150.0s with 15 lost requests, and at 64 KB chunks 2 of 5 finished, in 609.9 and 686.7s. See below for what that says about the constants |
| files6 | A,B,C | C downloads first; B starts; A's process is killed mid-transfer | ✅ **7/7 runs on the assertion it makes now, 82-206s.** B keeps its 7 verified chunks and takes the other 25 with the sender dead, in 21.7s when it notices the dead link at once and 142s when it spends a stall timeout first. Which holder served it is recorded, not asserted: a 0.5s poll saw C in only one run of three |
| files7 | A,B | B's **process is killed mid-download** and restarted | ✅ **6/6 runs, 50-54s.** Comes back holding exactly the 7 chunks it had verified, resumes 4.0s later with nobody asking it to, and finishes in 27-35s |
| files8 | A,B | files1 under `lossy` (62.5 kbps, 250±150 ms, **15% loss**), 512 KB | ⚠ Probe. At 32 KB chunks (16 of them), **three of five runs arrived, in 427.8-564.2s**, 8-9 requests and one lost each; the other two held 4 and 1 chunk after losing two. The same five runs before the retry-wait fix arrived **none of five**, and at 64 KB one of four arrived, in 214.2s. This link pays per request, so the chunk that helps files5 costs here. See the entry below |
| files9 | A-H | A shares a 2 MB file in an invite-only channel and **every other tester posts its fetch inside the same second**, seven askers against a concurrent-serve cap of two | ✅ **5/5 runs at `--testers 8`, 136-309s.** All seven finish byte for byte every time. Four of the five never reached either cap and the whole fan-in is queueing: 3-49s each, 44s of spread, 84 requests and none lost. In the fifth all seven first requests landed together, five were refused by the serve cap, and each of those paid exactly 120.0s of silence for a slot that had freed inside a second; one was refused twice and finished at 242s. **0 of 3 finished in 924s before the rate-limit fix below** |
| files10 | A,B,C,D | files9 at `lora_fast` (SF7, 5.5 kbps), 200 KB, three askers, testers slowed to a 60s announce cadence | ⚠ Probe. **Four runs of five finished all three members, in 1695-2737s**; in the fifth one member finished at 2557s and the other two were still holding 4 and 5 of 7 chunks at the 3000s ceiling. Each downloader takes 671-2672s against files5's 496-651s for a single one, and the three together move 230-377 B/s against that row's 315-413: one uplink divided three ways, not three uplinks. Nobody is starved, and **66 of the 97 lost requests across the five runs were dials at the two members who were themselves still downloading**, at 120s each. Multi-source is real here and not on broadband: four runs in five had a member served in part by an earlier finisher |

#### files5: what the LoRa run says about the constants

Measured three times over on the same file and the same profile, five runs each:
at 64 KB chunks, at 32 KB, and at 32 KB with holder choice reading the path
table (the fix below).

| Chunks | Finished | Fetch time | Rate | Requests | Failed requests |
|---|---|---|---|---|---|
| 64 KB, 4 chunks | 2 of 5 | 609.9s, 686.7s | 335.8, 298.3 B/s | 4 | 4 in the last run |
| 32 KB, 7 chunks | 4 of 5 | 506.1-1150.0s | 178.1-404.7 B/s | 6-9 | 15 across the five |
| 32 KB, holder choice fixed | 4 of 5 | 495.6-651.0s | 314.6-413.2 B/s | 6 | 4, all in one run |

The chunk change on its own is the middle row: two runs in five became four, and
the runs that finished stayed slow, because what the smaller chunks bought in
airtime they gave back in lost dials. **Eleven of that row's fifteen failed
requests were dials at the third member**, which holds nothing, never announces
on the `files` aspect, and so has no path to fail fast against; each cost about
two minutes, and the run that did not finish spent four of its five failures
there. Ordering candidates by the path table took the failed requests from
fifteen to four, the runs that never lost one from one in five to four in five,
and the median finish from 1050s to 625s. Four of the five 32 KB runs move the
file at 315-413 B/s against a nominal 687, which is the same rate the 64 KB runs
managed: the extra round trips cost nothing measurable.

Nineteen requests failed across the two 32 KB passes and they split cleanly by
cause. Thirteen were dials at the member holding nothing, which is the fix
above. The other six stalled at the holder, and none of those six died in this
project's code: four were ranges whose *advertisement* was still queued on the
holder's uplink when RNS's own advertisement watchdog gave up (four retries,
about 35s, against a queue that took 50s to drain), one was a 32 KB range that
crossed the link intact and was cancelled because its proof did not get back in
time, and one was a request that never reached the holder at all. In every one
RNS gave the requester nothing, and the plane's 120s stall sweep is what ended
it.

Read against the constants:

- `FILE_CHUNK_BYTES` at 32 KB is **70-110s per chunk measured here**, against the
  47s the constant's comment computes from the link's rated speed: a shaped
  sender and a shaped receiver are two hops of the same radio, so the wire runs
  at about half its nominal rate. The 32 KB choice is confirmed at SF7 by ratio
  (4 of 5, twice, against 2 of 5) and costs nothing in throughput. It is *not*
  confirmed as the better number everywhere: files8 below is worse at 32 KB than
  it was at 64 KB, for the opposite reason, and the comparison there is against
  a 64 KB run that predates the holder fix. Re-running both rows at 64 KB with
  holder choice fixed is the measurement that would close the question.
- `FILE_STALL_SECS` (120s) bounds a response that never starts, and nothing else.
  A 65 KB range that starts flowing takes 127-201s here and finishes: every
  resource part resets the timer, which is what a no-progress timer is for. The
  chunk-against-the-sweep arithmetic in the constant's comment reads as though
  the sweep were a total deadline for a chunk; it is not, and the comments now
  say so.
- The **window rule** carried a 2-chunk range fifteen times at SF7 across the two
  passes and lost four, always to the advertisement race above rather than to the
  size of what it asked for: every clean run carried two 65 KB ranges after
  70-76s chunks, which is exactly the shape a throughput-bounded window would
  have refused. Bounding the window by the time the last range took, the
  alternative rejected as "a second estimator to get wrong", would therefore have
  cost these runs their fastest requests and prevented none of the failures. The
  streak rule stays.
- `FILE_REQUEST_MAX_CHUNKS` (16) is still never approached: the window reaches 2.
- `MAX_SHARED_FILE_BYTES` (5 MB) is **3.5 to 4.4 hours** at the rates measured
  here. Unchanged as an argument: the transfer is resumable across links, holders
  and restarts, and the client is what should say so.

The half-rate result is the honest cost of a request-and-response transfer: each
range is a resource with its own handshake and proof, and the link is idle
between them. Fetching from several holders at once would fill that gap and is
deliberately out of scope (it doubles what one download can take from a shared
link).

#### files8: what a 15% loss link costs

Measured at both chunk sizes, five runs each at 32 KB.

| Chunks | Arrived | Fetch time | Rate | Requests | Failed requests |
|---|---|---|---|---|---|
| 64 KB, 8 chunks | 1 of 4 | 214.2s | 2450 B/s | 5 | 0 in that run |
| 32 KB, 16 chunks | 0 of 5 | never | n/a | 0-5 | 2 in every run |
| 32 KB, retry wait fixed | 3 of 5 | 427.8-564.2s | 930-1226 B/s | 8-9 | 1 each |

This link pays per request, not per byte, which is the opposite of files5 and
the reason the same chunk change lands the other way here. A request that
vanishes costs the full 120 s stall sweep, and the download then parks. Doubling
the chunk count doubled the exposure, and the middle row is the result: **not
one of five runs arrived**, three of them holding nothing at all after 600 s.

What each run actually got was two attempts. The wait after a silence doubles
every time one is spent, so a download that lost its first request asked again
at 120 s, lost that one too, and was still waiting out 240 s when the probe's
ceiling arrived. A served range did not put the wait back, though hearing the
holder announce would have, and a transport node damps those announces to
nothing. Counting a served range as the sign of a member it plainly is took the
row to **three of five**, and the two that still did not arrive held 4 and 1
chunk of 16 rather than nothing.

The rate on this link is now 12-16% of nominal against the 64 KB run's 31%:
sixteen request-and-response round trips at 250 ms and 15% loss are what the
smaller chunk buys. Whether 64 KB would now beat it here is untested, because
that row predates both fixes; running both radio rows again at 64 KB is the
measurement that would settle the chunk size properly.

The older half of this entry is worth keeping in order, because it is how two
defects were found. The first two 64 KB runs held **zero chunks** after 600 s:
the download was not slow, it never began. The link to the holder's file plane
came up (RTT 3.2 s), the requester identified, the first request (the chunk-hash
list) went out, and nothing came back, because a request packet that is proven
and then never answered has no failure of its own in RNS. What made that
terminal rather than slow was the [sync2 lesson](#sync2-fixed-after-three-causes)
again: the only thing that made a parked download try again was hearing the
holder announce. That is fixed, and this pass fixed the other half of the same
rule.

The probe is kept as the measurement of what a bad radio costs: a download under
15% loss is a sequence of clean minutes and 120 s silences, and which one
dominates is decided by whether the early requests survive.

#### files9 and files10: what happens when every member asks at once

The serve cap and the per-link rate limit exist for one moment: several members
wanting the same file from the same holder. Until this pass neither had been
asked for more than two downloads at once, and the first run with more askers
than slots found that the cheaper of the two ceilings was set below what a
single legitimate download costs.

Eight requests a second is a rate no download chooses. A download issues one
range at a time and waits for the answer, so how often it asks is the link's
speed: over loopback a 2 MB file at 32 KB chunks is about twelve requests and
the first eight of them landed inside one second. The ninth was refused, a
refusal is silence, and silence costs the asker the plane's 120s stall sweep.
All three members were refused mid-transfer, all three then spent two minutes
each rediscovering a holder, and **none of the three finished in 924s**, all
stopping at 52 of 64 chunks. The file plane now sets its own ceiling of 64 per
link per second, above the 18 ranges the largest file allowed costs in total.

| Run | Askers | Finished | Per downloader | Spread | Cap refusals | Rate limited | Served / lost |
|---|---|---|---|---|---|---|---|
| before the fix | 3 | 0/3 | never, 52 of 64 chunks | n/a | 0 | 6 | 54 / 18 |
| 1 | 7 | 7/7 | 4.1-48.8s | 44.7s | 0 | 0 | 84 / 0 |
| 2 | 7 | 7/7 | 1.0-242.0s | 241.0s | 6 | 0 | 90 / 6 |
| 3 | 7 | 7/7 | 3.1-47.8s | 44.7s | 0 | 0 | 84 / 0 |
| 4 | 7 | 7/7 | 4.1-47.9s | 43.8s | 0 | 0 | 84 / 0 |
| 5 | 7 | 7/7 | 3.1-47.8s | 44.7s | 0 | 0 | 84 / 0 |

Four of the five runs never reached either ceiling. Seven members asking at
once are simply served one after another: 14 MB leaves the holder in about 48
seconds, the last finisher is the seventh in the queue rather than a starved
one, and the 44s of spread is the queue and nothing else. That is the answer to
the row's question on a fast link, and it is a boring one.

The fifth run is the interesting one, and it is a race rather than a load: all
seven first requests reached the holder inside the same instant, so five of them
met the concurrent-serve cap. **Each of those five then waited exactly 120.0s**,
the stall sweep, for a slot that had freed within a second of the refusal; one
was refused a second time on its retry and finished at 242.0s, against the
first poll after the fetches went out for the two the cap admitted. The cap cost that run 241s of spread to save
about a second of contention.

Two things follow, and neither is a number to change:

- **The cap counts responses in flight, not downloads.** A slot is released as
  soon as its link has no outgoing resource left, which on a fast link is the
  gap between two ranges of the same transfer, so a fan-in of seven passes
  through two slots by rotation and never notices them. Whether it fires at all
  is decided by how tightly the first requests bunch, which is why it fired once
  in five identical runs. Raising `MAX_CONCURRENT_SERVES` would not change that
  shape, only the width of the instant.
- **What the refused member pays is the silence, not the wait.** 120s against a
  slot free in one is the whole of that run's spread. This is the cost
  [already recorded](#open) for a refusal on this plane, now measured in the
  case it was predicted for. The cap refusal is decided before the request is
  parsed and before any membership lookup, so it is one of the two refusals that
  could answer "busy" without telling an asker anything the holder's own `files`
  announce does not. That is a wire change, and it belongs to a decision rather
  than to a stress pass.

Multi-source is **not** exercised on a fast link: in four runs of five every
member was served by the sharer alone, and only the member refused twice ever
asked anybody else. The design's promise that a later downloader can pull from
an earlier finisher is real, but a healthy sharer means nothing ever needs it,
which is why files9 records it and does not assert it.

#### files10: the same fan-in on a radio

Three members, 200 KB, SF7 at 5.5 kbps, the size and profile files5 measures a
single download at. Four testers rather than eight, because at SF7 seven askers
would measure the announce traffic rather than the transfer.

| Run | Finished | Per downloader | Spread | Cap refusals | Served | Lost (unreachable / stalled) |
|---|---|---|---|---|---|---|
| 1 | 3/3 | 671.2, 1511.9, 1630.9s | 959.7s | 0 | 24 | 4 / 4 |
| 2 | 1/3, the others at 4 and 5 of 7 chunks | 2557.0s | n/a | 1 | 30 | 23 / 11 |
| 3 | 3/3 | 1064.5, 1461.6, 1922.3s | 857.8s | 0 | 22 | 11 / 3 |
| 4 | 3/3 | 1766.1, 2048.4, 2672.3s | 906.2s | 0 | 26 | 18 / 8 |
| 5 | 3/3 | 1430.7, 1739.1, 1885.3s | 454.6s | 1 | 26 | 10 / 5 |

**The uplink is divided, not multiplied.** The three transfers together move
230-377 B/s, which is the 315-413 B/s files5 measured for one downloader minus
the extra handshakes, so the last finisher waits about three times as long as a
single downloader does and the first waits about as long as one. That is the
honest shape of a shared radio: nobody is starved, everybody pays for the queue
in front of them, and the file is never sent more than once per asker because
nothing here pushes.

Neither ceiling is what shapes these runs. The rate limit never fired once: at
SF7 a download asks roughly every 70-110s, which is thousands of times under
the limit that was refusing it on loopback. The serve cap fired twice in
five runs and cost the refused member the usual 120s of silence, which against a
1700s run is noise rather than the whole result it is on broadband.

**What dominates is dialling members who hold nothing.** Two thirds of the lost
requests, 66 of 97, were dials at the other two askers, and every one cost the
full `FILE_FETCH_TIMEOUT_SECS` of 120s before it was given up as unreachable.
Holder choice already orders a member with no path to its file plane last, which
is the [fix from the previous pass](#files5-what-the-lora-run-says-about-the-constants),
but in a fan-in those are the only other candidates and they provably hold
nothing yet: the sharer is skipped for the round the moment one of its requests
fails, and the next two attempts are then spent proving what the path table
already said. The run that did not finish spent 23 of its 34 losses that way.

Multi-source is what carries the rest: in four runs of five at least one member
was served in part by another downloader rather than by the sharer, and in run 1
one of them took chunks from all three of the others. The same property is never
needed on broadband (files9 sees it once in five), so this row is where the
design's claim that any holder will do is actually exercised.

## The LoRa pass

Every family re-run with `--link-profile lora_fast` (SF7, 5.5 kbps, 60±20ms, 1%
loss), same scenario bodies, timeouts scaled 6×. The suite earned its keep here:
five scenarios that pass on broadband fail on a radio.

| Family | Broadband | LoRa SF7 | New on LoRa |
|---|---|---|---|
| `public` | 6/6 | 6/6 | public5 probe returns **nothing at all** |
| `invite` | 11/12 | 10/12 | **invite7** |
| `sync` | 8/9 | 7/9 | **sync8**, **sync11** |
| `links` | 5/5 | n/a (already shaped) |, |
| `servers` | 5/5 | 5/5 |, |
| `social` | 7/7 | 5/7 | **social5**, **social6** |
| `restart` | 3/3 | 3/3 |, |

Servers and restart/persistence are unaffected. Everything that breaks is a
*propagation* path: sync, member-list documents, avatars, directory entries.
What the radio changes is the size of the gap, not usually its nature, the
exception being public5, which escalates from a latency footnote to an outright
failure.

- **public5**: a late joiner received nothing in **723s** with
  `sync_state: unknown`, meaning no request was ever issued. On broadband the
  same gap is 1–9s of blindness. Deferred by decision along with public6.
- **invite7**: four rosters never converge in 360s (broadband: 30s). A
  member-list document is a signed blob carrying the whole roster, and
  invite → join-request → document is four hops.
- **sync8**: granting `full_sync` mid-session did not re-open withheld history
  after 1375s (broadband: 3.0s). The re-ask waits on the next announce-driven
  request.
- **social5 / social6**: an avatar removal and a display-name change never
  arrived within 360s. Both propagate as announce metadata rather than as
  retried messages, and both payloads are large relative to the link, so these
  may be latency rather than loss; re-run at a higher timeout scale before
  treating them as defects.

The `files` family carries its own radio rows rather than a whole-family pass:
files5 is files1 at SF7 and files10 is files9 at SF7, and the rest of the family
is broadband because what it tests (a dead holder, a killed downloader, a
non-member's request) is not about the link. One thing that pass makes plain: at SF7 the harness's own
announces are a large fraction of the link, four testers announcing three
destinations each every 10s against 687 bytes a second, so files5 slows every
tester to a 60s cadence first and puts it back afterwards. Without that, the
transfer queues behind the harness and the measurement is of the harness. The other is that both ends are
shaped, so the wire runs at about half the profile's rated speed: files5 moves a
file at 315-413 B/s against SF7's nominal 687. Any timing read off a LoRa row is
that halved number, not the datasheet one.

At SF10 (1 kbps) the suite stops being informative: a five-message fan-out to
two subscribers exceeded 600s before reaching the scenario's actual subject.
That is a bandwidth floor, not a defect, shape to SF7 for behaviour, and treat
SF10 as a question about payload sizes instead.

## Findings

Everything the matrix turned up, across all ten families.

### Fixed

| Finding | Detail |
|---|---|
| **History died with its author**, a peer who joins after an author leaves could never read that author's messages, because verifying needs a key only the author's own announce could supply | Fixed: a sync response carries a deduplicated `{author: public key}` map, and a relayed key is cached only if it hashes back to the identity claiming it. integrity2 went from *never* to 0.0s; regression test in `tests/test_sync_multipeer.py` |
| **Join and invite were sent once and never retried**, `_send_raw` in `subscription.py` and `invite.py` dropped the message outright when the recipient's path was unresolved, which is exactly when a first join or a first invite happens | Fixed: both hold the message in a bounded `ControlRetryQueue` and flush it when the peer announces. Regression tests in `tests/test_subscriptions.py` and `tests/test_invites.py` |
| **A sync answer was dropped when the responder could not yet address the requester**, and unlike every other send path it did not even request a path. The requester sat at `pending`, unable to tell silence from refusal | Fixed: the answer is held (for no longer than the requester will accept one) and re-sent when the peer announces. This is most of sync2; see below |
| **sync2**, a returning peer recovered nothing, in half of all runs | **Fixed**, after three causes: no trigger for this node's own link returning, the responder's answer dropped on an unresolved path, and a silent refusal with nothing left to re-ask. 12/12 after the last. Regression tests in `tests/test_sync_multipeer.py` |
| **Nothing noticed our own link returning**: every catch-up path is driven by hearing *from* a peer, so the node that was itself away had no trigger | Fixed: `connectivity.py`'s `LinkWatcher` polls Reticulum's interface state and resyncs on the transition back to online, ignoring shared-instance client interfaces. Tests in `tests/test_connectivity.py` |
| **A sync row rejected for an unverifiable author signature did not bound the watermark**, so a batch of `[rejected_old, accepted_new]` skipped past it and no future sweep would offer it again | Fixed on this branch: the signature-rejection path now sets `failed_ts` the same way a failed insert does. A row with an *implausible timestamp* deliberately does not, since it cannot be placed and would let one bad row freeze sync. Regression test in `tests/test_sync_multipeer.py` |
| **One `sync_progress` row written from both directions**, collapsing the responder's trust-horizon floor and stranding history older than a requester's watermark | Root-caused and fixed by splitting the serve direction into a `sync_served` table; regression test in `tests/test_sync_multipeer.py`. Found through sync11, but **it did not fix sync11**; see below |
| **invite11**, `kick` and `manage_roles` were grantable to any role, but a non-admin's member-list document is rejected by every recipient, so the grant did nothing on the network | Fixed by narrowing the permission: both are dropped from the member role on read and on write, and neither client offers them. Scenario rewritten to the new rule; regression tests in `tests/test_permissions.py` |
| **restart1**, `_subscriber_versions` lived only in memory, so a restarted owner renumbered from 1 and every later list was rejected as a replay | Fixed on `main` by the August security audit, which persists the counter in a `subscriber_list_versions` table and re-checks the version under the commit lock. This branch's own fix was dropped at the merge in favour of it; the end-to-end regression test in `tests/test_subscriptions.py` stays |
| **An invite to a channel inside a server anchored a hash no document would arrive for**, `publish_member_list` normalises to the owning scope, so a join request is always answered with the *server's* document, but `send_invite` did not. The invitee could not trust what it received, held it unapplied, and its sidebar stayed empty while the inviter's roster showed it admitted | Fixed: `send_invite` and `send_join_request` normalise through `scope_for` the same way. servers7 fails without it. Regression tests in `tests/test_servers.py` |
| **A re-invited member was never re-subscribed**, leaving drops the subscription and keeps the `channels` row, and the subscribe on re-admission was gated on that row being absent. Every listing filters on the subscription, so the channel never came back | Fixed: the guard now covers only the metadata write; subscribing and the joined callback run whenever an accepted document names us, matching what the server roster already did. invite19 fails without it. Tests in `tests/test_invites.py` |
| **A server document from a non-creator admin was rejected**: the `servers` row is written before the document is validated, so the creator anchor displaced the accepted-invite anchor instead of joining it | Fixed: with no stored member list the two anchors are unioned, and a rejected document no longer leaves an empty `servers` row behind. Adversarial coverage in `tests/test_adversarial.py` proves the union admits nothing new |
| **A held membership was unreachable from the Flutter client**, a document for a scope with no anchor is parked for the user to confirm and surfaced with a null token, which `api.py` called `.hex()` on. That raised inside the manager's callback guard, so the entry *and* its event were lost, and no endpoint exposed the held document anyway | Fixed: held documents ride the invite list under a null token, accept and decline branch on it exactly as the Qt client does, and the held metadata now carries its scope kind so a server confirms as a server rather than a phantom channel. Tests in `tests/test_api_invites.py` |
| **A membership document was delivered exactly once**, and the queue holding one for an unreachable peer is in memory, so a peer that missed the only copy stayed a member on every other node and a stranger on its own, recoverable only by a fresh invite | Fixed: hearing a peer re-sends the current document for any scope they are a member of, behind a per-(scope, peer) cooldown matched to the announce interval. Documents are version-ordered, so a peer already current ignores it. invite20 fails without it |
| **The equal-version tiebreak compared against a sentinel**, it re-derives the *stored* document's signer rather than trusting its signature map, but rebuilt that document without `joined_at`, `departed` or `channels`. The payload no longer matched the signature, nothing validated, and the `0xff…` fallback lost every tie, so any equal-version document from a trusted signer was re-applied over the one already held | Fixed: every signed field is carried across, present-or-absent as stored. Latent while equal-version documents were rare; the membership resync made them routine and servers4 went 0/5. 5/5 after the fix. Regression tests in `tests/test_invites.py` |
| **A message from an unaccepted sender vanished**, dropped where the gate refused it, while LXMF had already proved the packet, so the sender's client showed it delivered. A client speaking only plain LXMF cannot send `MT_FRIEND_REQUEST`, so messaging was its only way to ask and it had no way at all | Fixed: the message is held as a request carrying its text, shown wherever a friend request is, and filed into the conversation on accept. The gate is untouched, holding grants nothing. Bounded where the row is written, because this path is deliberately exempt from the router's control throttle. interop4 fails without it; `tests/test_adversarial.py::TestAdversarialMessageRequests` pins the bounds |
| **A file plane nobody could dial**: the holder registered its destination on the `files` aspect and never announced it, and a path request for a destination that has never announced is answered only by a node that already knows the path. A transport node in between does not go looking (`Interface.DISCOVER_PATHS_FOR` leaves out the ordinary full mode), so on any mesh with a hop in it every fetch failed as `unreachable` after its 120s timeout | Fixed: a node announces on the `files` aspect while it holds something to serve, when it becomes a holder and every `FILE_ANNOUNCE_INTERVAL_SECS` (300s) after. A node holding nothing stays quiet, since nobody has a reason to dial it. files1 fails at 0 chunks without it; regression tests in `tests/test_files.py` |
| **The concurrent-serve cap counted requests, not downloads**: a slot was held per request for a two-second settle floor, so with two members downloading, every range after the first was refused. A refusal is silence, which the requester can only tell from a lost packet by waiting out its 120s stall | Fixed: the slot is held per link. A download issues one request at a time, so the next range on a link already serving is the same transfer continuing, not a third one. Regression test in `tests/test_file_transport.py` |
| **A quiet member was treated as an absent one**: holder choice filtered candidates through `PresenceManager.is_online`, and presence is evidence of having *heard* a peer. A returning member found the only other holder unheard-from and waited for an announce with the bytes one link away | Fixed: presence orders the candidates and no longer gates them. Asking a quiet member costs one dial that fails and parks the download; not asking cost the download. Regression tests in `tests/test_files.py`, including the replacement for the test that encoded the old rule |
| **A member that holds nothing was asked first**: holder choice ordered candidates by presence, and a node announces on the `files` aspect only while it holds something. The member holding nothing reads as the livelier peer and is the one with no path to fail fast against, so eleven of the fifteen requests files5 lost across five SF7 runs were dials at it, about two minutes each | Fixed: candidates are ordered by whether a path to their file plane is already known, then by presence, and all of them are still asked. The same five runs went to 4 lost requests, four clean runs in five, and a median finish of 625s against 1050s. Regression test in `tests/test_files.py` |
| **The retry wait doubled across evidence that the holder was there**: a parked download's wait doubles every time one is spent and is put back by any sign of a member, and a served range was not counted as one. A download that lost a request early asked twice in ten minutes on a link that answered in between | Fixed: a served range puts the wait back to the floor, the same reset hearing a peer performs. It is the strongest sign of a member there is. Regression test in `tests/test_files.py` |
| **A parked download had one trigger and it was the wrong one**: only hearing the holder announce made it try again, and a transport node damps repeat announces while the liveness beacon informs only its receiver. Under 15% loss the requester heard nothing from a holder that was up the whole time and never asked again in eight minutes | Fixed: a parked download asks again on its own after 120s, doubling to an hour, reset by any sign of a member. The same shape `SyncManager.tick` needed for sync2, and for the same reason. Regression tests in `tests/test_files.py` |
| **The per-link serve rate limit was below what one download costs**: a download issues one range at a time and waits for it, so how many requests it makes in a second is the link's speed rather than anything either end chose, and over loopback a 2 MB file asks eight times in well under one. Every member of files9 was refused mid-transfer by the shared 8-per-second ceiling, and a refusal is silence, so each paid the 120s stall sweep and none of three finished in 924s | Fixed: the file plane sets its own ceiling of 64 per link per second, above the 18 ranges the largest file allowed costs in total. What bounds the work here is the concurrent-serve cap and the response ceiling, not the request rate; this only bounds a peer that is not waiting for answers at all. files9 goes from 0 of 3 to 7 of 7 with seven askers, 5/5 runs. Regression test in `tests/test_file_transport.py` |
| **Rejections were silent**: `_validate_document` returned `None` with no log for a failed signature or an unrecallable signer, and the auto-join block aborted without one for a name mismatch or a missing channel name. From outside, a rejected document is indistinguishable from one never sent | Fixed: each of those paths logs a warning naming the scope and the reason |

### Open

| Finding | Detail |
|---|---|
| **api4**, the dev environment shares one API token across every tester, and the orchestrator's unauthenticated `/config` serves it | **Confirmed** (probe). Fine for a dev box; it means port 8800 is the real trust boundary, not the tester ports |
| **sync11**, a four-way partition reconciles only sometimes: 2 passes in 7 runs, always losing the *first* message a peer wrote in isolation | **Partly root-caused.** The watermark collision above was one cause and is fixed. The deep-sync cooldown refusing a returning peer's request silently, once per pair per 60s, is the leading candidate for the rest |
| **invite16**, `invite` remains grantable to the member role (invite11's narrowing covers only `kick` and `manage_roles`), and the token check and join-request handler honour it, but the admission document a member publishes is rejected by every peer, including the inviter itself, whose own `_accept_document` refuses it | **Confirmed** (probe). The invitee's token is spent while membership lands nowhere. The same disagreement invite11 had, awaiting the same kind of decision: narrow the grant or admit the inviter's document. invite14 and invite15 show the identical grants working at admin rank |
| **invite17**, leaving an invite-only channel propagates to nobody: `leave_channel()` unsubscribes locally and notifies only the creator's *subscriber* set, and no member-list update is published | **Confirmed** (probe). Every roster (the leaver's own included) keeps the departed member, and senders keep addressing it; only the leaver's `is_subscribed` gate goes quiet. A UI reading the roster shows a ghost member indefinitely. A self-removal document would hit the same trusted-signer wall as invite16, so the fix likely belongs to the owner or an admin noticing the goodbye |
| **voice11**: `loss_pct`, the metric `docs/voice.md` designates for the UI's per-peer quality indicator, cannot see a starved link. It counts gaps between frames that arrived, so a link delivering 8% of the audio reports ~6% loss, and `link_state` still reads `streaming` | **Confirmed** across three runs. The signal that shows it now exists (`rx_quality`'s `rate_fps`, added for voice13, plus the listener's starved playout ticks) and the UI still reads `loss_pct` |
| **voice5 / voice4**, a voice participant whose link drops shows `connecting` indefinitely rather than `unreachable`, and one whose process dies lingers for the roster TTL, 180s in production | **Confirmed.** Neither is wrong, but a UI showing "connecting…" for three minutes after someone crashed is not the honest state `docs/voice.md` asks for |
| **public5**: a public-channel join fires no sync request; backfill waits on the next peer announce | **Confirmed, and deliberately left.** 0 messages at join, backfill at 1.0s / 9.1s tracking the 10s heartbeat; up to 60s in the real client, and at SF7 it never arrived at all (see the LoRa pass). Deferred by decision, public-channel behaviour is being left alone for now |
| **A presence beacon is evidence for its receiver only**, so a peer that beacons is never answered and its own view stays stale. In files3 the returning member beaconed the holder every 35s for three minutes and the holder, having fresh evidence of it, had no reason to beacon back: the holder read as offline the whole time | **Confirmed**, and the reason the presence gate above had to go. The fix belongs to `presence.py` (answer a beacon from a peer we have gone quiet toward, once, the way `FirstContactAnnouncer` answers a first announce), and is not made here: nothing in `files/` depends on it any more, and every other consumer of presence deserves its own look first |
| **An identify that does not land makes every request on that link a silent refusal**: the file plane identifies once per link and then trusts that it did, so a lost identify packet leaves the holder refusing a member it cannot name. Seen twice in ten files8 runs, both times on the first request after a dial | **Confirmed**, and it recovers on its own: the stall sweep ends the request, the download parks, and the link is dropped as idle because a stalled request never refreshes it, so the next attempt dials and identifies again. The cost is about four minutes. Answering an unidentified requester with something rather than silence would halve it and is the one refusal that could say so safely, since it is decided before any lookup |
| **A response resource that fails after it has started is silent to the requester**: RNS reports it through `RequestReceipt.request_timed_out`, which returns early unless the receipt is still `DELIVERED`, and the first resource part has already moved it to `RECEIVING`. The receipt then sits in the link's pending requests for ever | **Confirmed** in five of the six requests files5 stalled on at SF7: the holder's advertisement watchdog expired, or its resource proof did not get back in time, and the requester learned nothing until the plane's own 120s stall sweep. The sweep is what makes it survivable, and RNS exposes nothing cheaper to detect it with, so this is recorded rather than fixed |
| **The concurrent-serve cap counts responses in flight, not downloads**: a slot is freed as soon as its link has no outgoing resource left, which on a fast link is the gap between two ranges of the same transfer. Seven members pulling one file at once reached it in one files9 run of five, and only because all seven first requests landed inside the same instant | **Confirmed.** Not wrong, but not the admission control the name suggests: what it bounds is concurrent response resources, and a fan-in it never refuses is served by rotation instead, which is what the four clean runs did in 44s of spread. No other number for it is better on this evidence; the run where it did fire cost 241s, and all of that was the silence below rather than the waiting |
| **A fan-in spends most of its time proving that the other askers hold nothing**: when several members want one file, the only candidates besides the sharer are members who are themselves still downloading it, and a member with no path to its file plane costs the full 120s fetch timeout to give up on. 66 of the 97 requests files10 lost across five runs were those dials, about 1600s a run | **Confirmed**, and not covered by the path-table ordering: that decides the order candidates are asked in, not whether they are asked. The cheaper shape is to park when the only candidates left this round have no path, since a parked download re-asks after 120s and is put back by any sign of a member, rather than spending 240s discovering two dead ends first. Not changed here: `tests/test_files.py` specifies that a member with no path is still asked, which is the guard against never asking a holder that has simply gone quiet, and choosing between the two is a decision rather than a measurement |
| **A refusal on the file plane costs the asker 120s of silence**: `None` from the serve handler means RNS sends nothing, and a request packet that is proven and then never answered has no failure callback in RNS, so only the plane's own stall sweep ends it | **Confirmed** in files4, at 120.1s and 127.5s, and measured again in files9: five members refused by the concurrent-serve cap each waited 120.0s for a slot that had freed within a second, one of them twice, which took that run's spread from 44s to 241s. Deliberate for a non-member, who learns nothing from silence. The cap refusal is the one that could safely answer instead: like the unidentified-requester refusal above it is decided before the request is parsed and before any membership lookup, so a bare "busy" tells an asker nothing the holder's own `files` announce does not already say. That is a wire change and belongs to a decision rather than to a stress pass |
| **public6**, `full_sync` has no effect on public channels; any subscriber can pull full history | **Confirmed, and deliberately left.** Identical backfill with and without the grant, and the UI offers the toggle regardless, so it reads as a privacy control that is not one. Deferred by decision, same as public5 |

### Predictions the runs refuted

Two were written to demonstrate a gap and demonstrated its absence instead; the third is a mechanism this document had wrong.

| Prediction | What actually happened |
|---|---|
| **social3**, reactions have no backfill path, so an offline peer misses them permanently | D recovered the reaction in 14.1s and 15.2s. LXMF's own retry redelivers the broadcast once the link returns; no application-level backfill is needed for a peer whose path is known |
| **public10**, a subscriber that misses a subscriber-list broadcast is stranded | It recovered every time, by the same LXMF retry. The no-retry gap only bites when the path was *never* resolved, a cold-start race, not an offline-peer case |
| **A 64 KB range cannot fit inside the 120s stall sweep**, the reasoning that halved `FILE_CHUNK_BYTES` | It fits. The sweep is a no-progress timer and every resource part resets it: 65 KB ranges took 127-201s at SF7 and finished. 32 KB is still the better number there by ratio (4 of 5, twice, against 2 of 5), but the mechanism was not the one written down: what killed the large ranges was the holder's own advertisement watchdog expiring while the advert sat in a queue on the uplink |

### One suspected gap that turned out not to be

An earlier draft recorded a third finding from the public family: that non-owner fan-out
on a public channel was unreliable, from public8 failing intermittently and public9
varying run to run. That was wrong, and the way it was wrong is worth keeping.

The reasoning was that `SubscriptionManager._send_raw` has no retry queue, so a
dropped subscriber-list broadcast would strand a peer permanently. public10 was
written to demonstrate exactly that, take a subscriber offline across a roster
change and watch it never recover. It recovered every time. `_send_raw` only
drops when `Identity.recall()` returns `None`, i.e. the path was never
resolved; once a path is known the message goes to LXMF, whose own outbound
retry redelivers when the link returns. The no-retry gap is a cold-start race,
not an offline-peer case.

The narrow version of it is real, though, and the sync family found it by accident:
sync4's setup failed because the owner never registered a joiner whose `MT_SUBSCRIBE`
went out before its path resolved. Same code path, but only reachable when the
peers have never talked, which is why public10, whose peers were already in contact,
could not produce it.

The real cause was in the scenarios: they asserted fan-out before the owner had
processed the joiners' `MT_SUBSCRIBE`, so the sends were addressed to a set the
targets weren't in. Waiting on the owner's registration (and, where a peer
addresses other peers, on full subscriber convergence) made three consecutive
family runs clean. It is a genuine ordering constraint (now written into the
timing rules above), but it belongs to the harness, not the app.

Worth stating plainly: an intermittent scenario is far more likely to be an
under-specified precondition than a real race, and "no retry here" is not a bug
until you check what the layer underneath does.

## Harness

`devtools/testenv/scenarios/`: `runner.py` (process lifecycle and selection),
`peer.py` (one method per API endpoint), `asserts.py` (polling assertions),
`flows.py` (shared setup), `scenario.py` (the registry and the strict/probe
distinction), and one `scen_*.py` per family.

How to run it, when a scenario is the right tool, and how to add one live in
`.claude/rules/scenario-testing.md`.

## Status

All fourteen families built and run: **123 scenarios, 97 strict and 26 probes**, counted from the registry rather than by hand, which the total here had drifted behind.

| Family | Scenarios | Result |
|---|---|---|
| `public`: public channels | 11 (7 strict, 4 probes) | All passing, three consecutive clean runs |
| `invite`: invite-only and membership | 20 (17 strict, 3 probes) | All passing; invite11 rewritten to the narrowed `kick` rule; invite16 and invite17 (probes) record the ineffective member `invite` grant and the invisible leave; invite19 and invite20 each found a real defect, 5/5 after the fix |
| `sync`: offline and sync | 10 (9 strict, 1 probe) | 8/9, **sync11 fails**, 2 passes in 7 runs. sync2 fixed, 12/12 |
| `links`: degraded links | 10 (5 strict, 5 probes) | All passing, on genuinely shaped links |
| `servers`: servers | 8 (7 strict, 1 probe) | All passing; servers7 is the reported channel-invite defect, 5/5 after the fix |
| `social`: reactions, presence, identity | 10 (9 strict, 1 probe) | All passing; social3's prediction refuted |
| `restart`: restart and ordering | 5 (3 strict, 2 probes) | All passing; restart1 confirmed, then fixed; restart3 confirmed |
| `voice`: live group voice | 13 (10 strict, 3 probes) | All passing; voice13 found a real cadence defect, 5/5 after the fix; voice4, voice5 and voice11 recorded gaps |
| `api`: the API surface | 5 (4 strict, 1 probe) | All passing; api4 records the shared-token property; api5 covers the pushed network map, 5/5 |
| `integrity`: message integrity | 4 (4 strict) | All passing; integrity2 found a real gap, now fixed and strict |
| `nomad`: page browsing and hosting | 4 (3 strict, 1 probe) | All passing, 4/4 runs each; nomad3 confirmed bounded offline failure and recovery |
| `interop`: direct messages with other LXMF clients | 4 (4 strict) | All passing against a real bare RNS+LXMF client; interop4 found a real gap, 5/5 after the fix |
| `files`: shared files in invite-only channels | 10 (7 strict, 3 probes) | All strict rows passing; files1 alone found three defects, files8 a fourth, the two radio probes two more and files9 a seventh, all fixed. files5, files8 and files10 record what a slow link, a lossy one and a shared one each cost |

**One strict scenario fails, on purpose.** sync11 is a real defect, left
strict and failing so `--family sync` exits non-zero until it is resolved:
intermittently, 2 passes in 7. invite11 is now passing on the narrowed `kick`
rule described above.

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
2. Let `SyncStatusTracker` distinguish "refused" from "waiting", today both
   read as `pending` forever.
3. Surface held-back messages in the UI rather than in a log line.
4. Decide whether the two refusals taken before any lookup, an unidentified
   requester and the concurrent-serve cap, should answer rather than go silent.
   Both cost the asker 120s today, files9 measured what that does to a fan-in,
   and neither can become an oracle for anything the holder does not already
   announce.
5. The two deferred sync rows and a clock-skew scenario, all of which need
   control of a tester's clock; the audit's 300s ceiling silently drops every
   message from a peer whose clock runs fast, and nothing tests it.
6. Re-run the LoRa pass: it predates the `api` and `integrity` families and all
   five fixes, several of which are timing-sensitive by construction.
