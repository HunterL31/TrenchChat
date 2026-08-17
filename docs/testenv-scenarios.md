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
| **Link** | set profile: broadband, satellite, serial 9600, lora_fast (SF7), lora_slow (SF10), packet radio, flaky (15% loss), custom |

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
| B10 | A,C | C calls `/roles` with `remove_members=[A]` | `{"ok": false}`, no document published, rosters unchanged on every peer — adversarial path, GUI bypassed |
| B11 | A,B,C | A grants `kick` to member; C kicks B | B removed from all rosters — confirms a granted permission actually takes effect, the mirror of B10 |
| B12 | A,B,C,D | A promotes B; A and B both publish a roster change within ~1s | Both documents validate against stored state; final rosters identical on all four; no split-brain |
| B13 | A,B,C | C (member) attempts `/channels/{h}/permissions` | `{"ok": false}` — lacks `manage_channel`; stored perms unchanged everywhere |

### C — Offline behavior and sync

The reason this environment exists. All three sync mechanisms only run on a
degraded or interrupted link.

| ID | Peers | Actions | Expected result |
|---|---|---|---|
| C1 | A,B,C | B goes offline (link drop); A sends 3; B goes online | B receives all 3. Pending-retry (mechanism 1) flushes on B's announce |
| C2 | A,B,C,D | B offline; A sends 3; A offline; B online (only C, D reachable) | B backfills all 3 from C or D — missed-delivery hints (mechanism 2) let a third party serve messages the sender never delivered |
| C3 | A,B,C | Same as C1 but B is **killed and restarted** instead of link-dropped | B still ends with all 3, but via a cold path: in-memory retry queue and sync status are gone, backfill comes from `request_sync_all` 3s after boot |
| C4 | A,D | D offline; A sends 60 (> `MAX_RESPONSE_MESSAGES` = 50); D online | D ends with all 60 and `state == synced`. Only passes if a truncated batch chains its own follow-up request |
| C5 | A,B,C | B offline for messages 1–5; B online, C offline for 6–10; C online | Both end with all 10, sourced from different responders — per-(channel, peer) watermarks, not a channel-wide one |
| C6 | A,D | D issues repeated deep (pre-window) sync requests in a burst | First deep sweep answered; subsequent ones inside `DEEP_SYNC_COOLDOWN_SECS` (60s) are silently refused. D's sync state reflects "not yet complete", never a flood on A |
| C7 | A,B | B joins invite-only, observing sync state throughout | Progression `unknown → syncing → synced`. With every other peer offline: `waiting`. With a known unclosable gap: `incomplete` |
| C8 | A,D | D joins without `full_sync` (gets no backlog); A grants `full_sync` to member | D's entitlement changed, so its next request re-asks from 0 rather than resuming — backlog arrives without D restarting |
| C9 | A,B,C | A kicks C; C requests sync | Request refused silently; C's message set frozen at kick time; C receives nothing further |
| C10 | A,B,C,D | Hub killed (total partition); each peer sends 2; hub restarted | During partition every peer shows `waiting`; after restart all four converge on 8 messages |
| C11 | A,B,C,D | All 4 offline simultaneously, each sends 2 locally, all come online | Full mesh convergence on 8 — the hardest sync case, four disjoint histories reconciling at once |
| C12 | A,B | B offline past `SYNC_WINDOW_SECS` (7 days, clock-shifted); B online | Deep backfill is served but rate-limited; assert what B ends up holding, and that neither peer spins |

### D — Degraded links

| ID | Peers | Actions | Expected result |
|---|---|---|---|
| D1 | A,B,C,D | A on `flaky` (15% loss); A sends 20 | All three peers eventually hold 20. Retries and hints visible in sync status; measure time-to-converge |
| D2 | A,B | B on `lora_slow` (977 bps); A sends an image at `MAX_IMAGE_BYTES` | Transfer completes; measure duration. Slow is expected, stalled is not |
| D3 | A,B,C,D | A broadband, B satellite, C `lora_fast`, D packet radio; each sends 2 | All converge on 8; arrival order differs per peer; no peer strands |
| D4 | A,B | B on `flaky`, toggling offline/online during a 20-message burst | B converges on 20; no duplicates, no gaps |
| D5 | A,B,C,D | All four on `lora_slow` during a roster change plus message burst | Convergence, with announce/beacon overhead visibly competing for the link — the documented "tester falls behind" case, asserted rather than assumed |

### E — Servers

| ID | Peers | Actions | Expected result |
|---|---|---|---|
| E1 | A,B | A creates a server with 3 channels; invites B once | One invite admits B to the server and all 3 channels |
| E2 | A,B,C,D | A grants `create_channel` to admin, promotes B; B creates a 4th channel | C and D receive channel 4 via the re-published server document; all four see 4 channels |
| E3 | A,B | A edits server permissions | Mirrored into every child channel row on every peer; a per-channel override attempt returns 409 |
| E4 | A,B,C | B leaves the server | B unsubscribed from every channel in it; A and C unaffected |
| E5 | A,B,C,D | A kicks C from the server | C loses all channels in it at once; B and D rosters converge |
| E6 | A,B | Server-level `full_sync` grant, then invite B with backlog in 2 channels | B backfills both channels — server-scoped tenure resolves per channel |

### F — Reactions, emoji, presence, identity

| ID | Peers | Actions | Expected result |
|---|---|---|---|
| F1 | A,B,C,D | Public channel, all joined; B reacts to A's message | A, C and D all show count 1. The owner is in the broadcast subscriber payload — the regression that previously left the owner blind to reactions |
| F2 | A,B,C | B imports a custom emoji and reacts with it | C and D lack the image, request it from B on demand, and render it |
| F3 | A,B,D | D offline; B reacts to A's message; D online | ⚠ Expected: D never sees the reaction. Reactions have no sync/backfill path — only chat messages do. Confirm, then decide whether it's a gap worth closing |
| F4 | A,B,C,D | B goes offline via link drop; separately, B exits gracefully | Presence flips to offline on A, C, D — fast on the graceful goodbye announce, slower on the beacon timeout. Assert both paths |
| F5 | A,B,C,D | A sets an avatar, then removes it | All three peers show, then drop it |
| F6 | A,B,C,D | A changes display name | Propagates to all three; directory search finds the new name |
| F7 | A,B,C | A adds B as a friend with a nickname | Local only — C sees nothing, B is not notified |
| F8 | A,B,C | B replies to A's message; C reacts to the reply | Reply threading and reaction target resolve identically on all three |

### G — Restart, persistence, ordering

| ID | Peers | Actions | Expected result |
|---|---|---|---|
| G1 | A,B,C,D | Public channel with B, C joined; **A restarts**; D joins | ⚠ Expected failure: `_subscriber_versions` is in-memory only. A's counter restarts at 0 and re-issues v1, while B and C still hold vN from before and reject anything not newer as replayed — so B and C never learn about D. Highest-value row here; if it reproduces, the version counter needs persisting |
| G2 | A,B,C,D | Full history and roster built; all four killed and restarted | Messages, membership, roles and identities all survive; message sets identical to pre-restart |
| G3 | A,B | A invites B with no path warm-up | Invite silently dropped (documented `_send_raw` quirk). Codifies why the harness warms up first — and measures how wide the window actually is |
| G4 | A,B | A publishes a roster change and sends a chat message immediately after | Chat message may be dropped if the roster hasn't landed. Assert the settle requirement rather than leaving it to luck |
| G5 | A,B,C | A single tester is reset (data wiped, same slot) | Returns as a **new identity**; old membership rows on B and C reference an identity that will never reappear. Assert the roster state that leaves behind |

## Suspected gaps

Each has a concrete code-level reason to expect a failure or a surprise. Two
are now confirmed by a run; two are still unbuilt.

| Gap | Status |
|---|---|
| **A5** — a public-channel join fires no sync request; backfill waits on the next peer announce | **Confirmed.** 0 messages at join, backfill at 1.0s / 9.1s tracking the 10s heartbeat. Up to 60s in the real client |
| **A6** — `full_sync` has no effect on public channels; any subscriber can pull full history | **Confirmed.** Identical backfill with and without the grant. The UI offers the toggle regardless |
| **G1** — subscriber-list version counter is in-memory, so it resets on owner restart and surviving subscribers reject later lists as replays | Not yet built (family G) |
| **F3** — reactions have no backfill path, so an offline peer misses them permanently | Not yet built (family F) |

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
| `asserts.py` | Polling assertions: `wait_until`, `settle`, `hold_for`, `all_hold`, `converged`, `rosters_identical`, `subscribers_converged`, `sync_settled`, `diff_report`, `subscriber_views` |
| `scenario.py` | The `@scenario` registry and the strict/probe distinction |
| `scen_public.py` | Family A |

Families B–G are not written yet; each is one more `scen_*.py` registering
against the same runner.

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

Family A: 10 scenarios, 6 strict + 4 probes, ~6 minutes a run (most of it
environment resets). Three consecutive clean runs after the ordering fix; the
two failures before it were both the precondition gap described above.

Remaining phases:

1. Family C — the highest-value coverage and the reason for real networking.
2. G1 and F3, the two unconfirmed suspected gaps.
3. Families B and E — membership and permissions across 4 peers.
4. Families D, F, G.
