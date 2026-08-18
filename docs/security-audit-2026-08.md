# TrenchChat — Full Security Audit (August 2026)

Scope: the `trenchchat/` core, the network layer, and the active Flutter UI
(`flutter_ui/` + its Python backend `devtools/testenv/api.py`, bundled for real
use by `main_flutter.py`). This audit re-verifies the hardening recorded in
`docs/security-improvements.md`, then looks for what remains. It complements
that document rather than replacing it: items there confirmed still-fixed are
noted briefly; the new material is the findings and the test plan below.

> **Status: everything except finding D (the PIN / encryption-at-rest rework)
> has since been implemented.** See `docs/security-improvements.md` for what
> landed, and §6 below for what was deliberately left. The findings are kept
> here as written, so the reasoning behind each fix stays on record.

Baseline test run at audit time: `675 passed, 1 skipped` on the non-GUI suite.
The Qt/GUI failures seen alongside it were environmental — a missing
`libEGL.so.1` — and not code defects; with that library installed the whole
suite runs. After this work, rebased onto main (voice, touch UI, link
shaping): **879 passed, 1 skipped, no failures**, GUI tests included.

---

## 1. The Reticulum trust model, and why it shapes every finding

Reticulum is a coordination-free, serverless network stack. Its design
commitments are the frame for this whole audit:

- **Addresses are public keys.** Every destination hash is derived from an
  Ed25519/X25519 identity. There is no DNS, no CA, no registrar — an address
  is self-authenticating. LXMF signs every message, so a receiver can know
  *cryptographically who sent it*.
- **The network authenticates identity, never authority.** Reticulum will tell
  you a packet genuinely came from identity `X`. It has no concept of "X is an
  admin of channel Y" or "X is allowed to post here." That is entirely the
  application's job. TrenchChat is, in effect, a distributed authorization
  system with no server to adjudicate it — every peer must independently and
  identically decide what every other peer is allowed to do, from signed
  documents alone.
- **Identities are free to mint.** There is no cost or scarcity to creating an
  identity, so **Sybil resistance is structurally impossible** at this layer.
  Rate limits and membership gates raise the cost of abuse but can never assume
  "one identity = one person." Any control that would only work if identities
  were scarce is not a real control here.
- **The mesh is partition-tolerant and gossip-relayed.** Messages arrive late,
  out of order, or via a third peer that was merely reachable. History is
  served by *whoever happens to be online*, not by an authority. So a peer
  relaying history is not the author of it, and the app cannot assume the
  relayer vouches for the content.

The consequence: **the confidentiality and sender-authenticity of the transport
are not in question** (X25519 + AES-256, Ed25519). Every finding below is an
*application-layer trust* issue — the gap between "I know who sent this" and "I
know what they're allowed to do with it" — or a *local/at-rest* issue, or the
*local API bridge* that fronts the whole identity. That is exactly the surface
Reticulum hands to the application and refuses to manage for it.

TrenchChat's core already reflects this well: the CLAUDE.md "three-layer
permission" rule, the signed-and-versioned member-list documents, the
`signature_validated` enforcement in `network/router.py`, and the tenure model
are all the *right* shape of defense for this model. The findings are where that
shape has a hole, is incompletely applied, or stops at the core boundary and
doesn't reach the new Flutter surface.

---

## 2. Posture summary

| Area | Verdict |
|---|---|
| Transport crypto (RNS/LXMF) | Out of scope; sound. |
| Inbound signature enforcement / quarantine (`router.py`) | **Sound**, load-bearing, well-tested. No bypass found. |
| Member-list / invite / permission trust (`invite.py`) | **Strong.** Matches its documented contracts; only Low/DiD residue. |
| Sync correlation & tenure | Correlation **sound**; tenure gap #0 **confirmed open** (bound holds). New: timestamp/authorship gaps. |
| SQL / storage injection | **Clean.** All values parameter-bound. |
| Subscriber lists | Signed & versioned, but **replay protection doesn't survive restart**. |
| Image/avatar/emoji ingestion | **Weak:** shipping backend fails *open*; inbound bytes never re-sanitised. |
| Encryption at rest / PIN | **Weak** (documented open gap #1), confirmed not worse than stated. |
| **Flutter API bridge** | **Critical exposure:** unauthenticated act-as-identity API, wildcard CORS, no WS Origin check, `0.0.0.0` dev binds. |
| Flutter rendering | **Safe** — native `Text`/`Image.memory`, no HTML/webview/url_launcher, no injection surface. |

The single most serious area is the **API bridge** (§3.A/B): it is the one place
where a remote or web attacker needs no membership, no permission, and no crafted
protocol payload — just the ability to reach a port or lure the user to a web
page — to fully impersonate the user's cryptographic identity.

---

## 3. Findings (consolidated, ranked)

Severity reflects exploitability under the Reticulum threat model. "DiD" =
defense-in-depth (not independently exploitable, but the codebase's own posture
elsewhere is stricter). Line numbers are indicative — verify against current
`HEAD`.

### CRITICAL

#### A. The Flutter API bridge is an unauthenticated "act-as-identity" surface with wildcard CORS and no WebSocket Origin check
`devtools/testenv/api.py` (`create_app`, all endpoints). `main_flutter.py`
bundles this as the *shipping* backend, binding `127.0.0.1:<port>`
(`main_flutter.py:107-109`) — correct bind, but:

- **No endpoint authenticates anything.** There is no token, session, or
  per-request credential. Every endpoint acts as the logged-in identity: send
  messages (`POST /channels/{h}/messages`), dump the whole transcript and images
  (`GET /channels/{h}/messages`, `.../image`), change display name/avatar,
  rewrite the Reticulum interface config (`POST /reticulum/interfaces`), drive
  membership, leak the identity hash and peer graph (`GET /me`, `/network/map`,
  `/directory`), or DoS the node (`POST /net/offline`).
- **CORS is fully open:** `CORSMiddleware, allow_origins=["*"],
  allow_methods=["*"], allow_headers=["*"]` (`api.py:231-233`). Because there is
  no cookie/credential to protect, the wildcard fully applies: **any website the
  user merely visits can issue cross-origin `fetch()` to `http://127.0.0.1:<port>`
  and read the responses** — send messages as the user and exfiltrate the DB.
  `localhost` is a secure context, so an `https://` page reaches `http://127.0.0.1`.
- **The WebSocket `/ws` accepts with no Origin check** (`ws.accept()`,
  `api.py:971-982`). Browsers do not apply CORS/same-origin to WS handshakes, so
  any page can `new WebSocket('ws://127.0.0.1:<port>/ws')` and **passively stream
  every message the user receives, live.**

**Exploit:** user runs `main_flutter.py`, browses to `evil.com`; the page's JS
owns their identity and reads their history. This defeats the "localhost-only"
protection entirely. **No test exercises the HTTP surface at all.**

**Fix:** mint a random per-session bearer token at backend startup, deliver it to
the client via `TC_API_URL`/dart-define/served page, and require it on every HTTP
request *and* the WS handshake; replace `allow_origins=["*"]` with the exact
served origin; validate the WS `Origin` header; add Host-header allowlisting
against DNS-rebinding.

#### B. Dev/host launchers bind the *real-profile* API to `0.0.0.0` with no auth
- `devtools/testenv/serve_profile.py:41-42,91` — `--host` **defaults to
  `0.0.0.0`**, serving `Backend.for_real_profile()` (the real `~/.trenchchat`
  identity and mesh). The docstring even advertises `http://<host>:8810/ remotely`.
- `devtools/testenv/worker.py:44` and `orchestrator.py:367` — `0.0.0.0` (test
  identities, but same mistake).
- `devtools/testenv/remote_host.sh` runs `serve_profile.py` with no `--host`
  (inherits `0.0.0.0`) and publishes it over Tailscale; `cmd_status` prints the
  tailnet URL.

Combined with finding A (no auth), any host on the LAN/VPN/tailnet gets full
control of the served identity. **Critical** for `serve_profile.py`/`remote_host.sh`
because they front a *real* profile. **Fix:** default every bind to `127.0.0.1`;
make `0.0.0.0` an explicit, warned opt-in; never expose an unauthenticated build
over Tailscale.

### HIGH

#### C. Image sanitisation fails *open* in the shipping Flutter backend (regression) — VERIFIED
`devtools/testenv/api.py:871-876`. On `prepare_image()` failure the endpoint
forwards the **original unsanitised bytes** (`image_data = raw`) as long as
`len(raw) <= MAX_IMAGE_BYTES`:

```python
try:
    image_data, _ = prepare_image(raw)
except Exception as exc:
    RNS.log(...)
    if len(raw) <= MAX_IMAGE_BYTES:
        image_data = raw          # <-- fails OPEN
```

`prepare_image()` raises precisely on the inputs sanitisation exists to catch —
decompression bombs (`DecompressionBombError` past `MAX_IMAGE_PIXELS`), GIFs over
`MAX_GIF_FRAMES`, malformed files PIL refuses. Those are then shipped raw to every
recipient's decoder. This **directly contradicts** `security-improvements.md`
("Image sanitisation no longer fails open") and the code comment claiming parity
with `main_window.py` — whose handler actually fails *closed* (`image_data =
None`, "a rejected image must not be forwarded instead"). Since `main_flutter.py`
ships this backend, it is live behaviour.

**Fix (one line):** drop to `None` on exception; delete the `<= MAX_IMAGE_BYTES`
raw fallback. Regression test in §4.

#### D. Encryption at rest is off by default and the PIN is weak (documented open gap #1) — VERIFIED, not worse than stated
`core/lockbox.py`, `gui/pin_dialog.py`. Confirmed exactly as documented:
PIN is 4–8 numeric digits (keyspace 10⁴–10⁸ ≈ 13–27 bits); KDF is
PBKDF2-HMAC-SHA256 @ 600k; `lock.verify` is a Fernet token over a hardcoded
sentinel sitting next to the salt — an **offline verification oracle** needing
neither the DB nor the identity file; `lockbox.unlock()` is **unthrottled**, and
the GUI lockout resets after each cooldown (non-persistent, non-escalating, and
irrelevant to an attacker who never runs the GUI). The *same* PBKDF2 output keys
both the identity Fernet blob and the SQLCipher DB, so one recovered PIN yields
private key *and* message history. Estimated crack on one GPU: 4-digit ≈ seconds,
6-digit ≈ minutes, 8-digit ≈ hours. Remains the recommended-but-deferred
versioned-KDF + Scrypt + persistent-lockout rework.

### MEDIUM

#### E. Inbound images/avatars/emoji are never re-sanitised — only byte-capped
`messaging.py` (`F_IMAGE_DATA`), `sync.py` (sync image path), `avatar.py`,
`reaction.py`. All four validate a byte length (900 KB / 16 KB / 64 KB) then store
raw and hand the bytes to the recipient's native decoder (Qt `QPixmap`/`QMovie`,
or Flutter's decoder). A byte cap is **not** a pixel/frame cap: a ~900 KB JPEG can
declare 65535×65535, a GIF can carry thousands of frames — decoding to multi-GB
rasters. `Image.MAX_IMAGE_PIXELS` only guards the *send-side* PIL path, not the
inbound decoder. A malicious member crafts the LXMF message directly, bypassing
`api.py`/`prepare_image` entirely. This is the memory-safety-adjacent surface the
threat model calls out. **Fix:** re-encode inbound images through one bounded
library (PIL, pixel-capped) on receipt, dropping on failure, instead of passing
raw bytes to the UI decoder.

#### F. Subscriber-list replay protection does not survive a restart — VERIFIED
`core/subscription.py`. `_subscriber_versions` is an in-memory dict (`:79`),
read at `:223` and written at `:253`; **no subscriber-version column exists in
`storage.py`** (confirmed by grep). The subscriber *set* is persisted; the
monotonic version high-water mark is not, so it resets to `0` on every process
start. A peer who was ever a legitimate recipient captures an older
owner-signed `MT_SUBSCRIBER_LIST` (version ≥ 1); after the victim restarts,
`last_seen = 0`, the genuine owner signature still verifies, and
`replace_channel_subscribers` overwrites the current roster with the stale one —
**resurrecting a removed subscriber** (redirecting a victim's outbound traffic to
a kicked peer) or dropping current ones. This is exactly the replay
`security-improvements.md` claims closed; it is closed only within one process
lifetime. Same root cause gives a correctness bug: the owner's counter also
resets to 1 on restart, so long-lived subscribers reject the owner's genuine
post-restart updates until it climbs back past their high-water mark.
**Fix:** persist the per-channel subscriber version (mirror
`get_member_list_version`) and load it in `__init__`.

#### G. Self-asserted `F_TIMESTAMP` is unbounded → sync watermark poisoning + transcript pollution
`messaging.py` (`timestamp = fields.get(F_TIMESTAMP) or time.time()`) and
`sync.py` (`float(m.get("timestamp", ...))`) accept any value with no sanity
clamp. A member/responder sends a message stamped in the year 2100: it passes
tenure (author is a current member), is inserted, and advances the requester's
persisted per-peer `sync_progress` via `MAX(...)`. Since `get_messages_after`
filters strict `timestamp >`, **every future sync request to that peer returns
empty forever** — a one-shot or buggy response permanently wedges that peer
relationship (surviving restart), and the future-dated row pins to the top of the
transcript unremovably. Related (N4): `accepted_ts.append` runs before the
`if inserted:` check and `message_id` is globally unique, so a responder can
advance the watermark citing a `message_id` that exists in another channel
without inserting anything visible. **Fix:** clamp `F_TIMESTAMP` to `<= now +
small_skew` on both the messaging and sync-ingest paths *before* it can advance a
watermark; move `accepted_ts.append` inside the successful-insert branch.

#### H. Solicited `MT_SYNC_RESPONSE` authorship is unauthenticated
`sync.py` — inserted `sender_hash`/`content` come straight from the responder's
msgpack payload; the *original author's* LXMF signature is not carried or
re-verified (only the responder is authenticated). On a public/open-join channel
(no tenure) a solicited responder injects a message attributed to **any**
identity. On a tenured channel a member can forge a message from any co-member
enrolled at the chosen timestamp. `security-improvements.md` documents the
*unsolicited*-injection fix but not that the *solicited* path still trusts
authorship — this should be documented explicitly, or original-author signatures
propagated through sync. Partly inherent to gossip relay.

#### I. Quarantine path-request amplification
`network/router.py` `_quarantine_message` calls
`RNS.Transport.request_path(message.source_hash)` for every `SOURCE_UNKNOWN`
message (`:222-226`). These fail `_authenticate`, so they never reach the control
rate limiter, and `source_hash` is attacker-chosen. N cheap unsigned packets with
distinct random `source_hash` values induce N path-request broadcasts onto the
shared mesh — 1:1 amplification with no throttle. Quarantine *memory* is bounded
(128); the path-request side effect is not. **Fix:** rate-limit/de-dupe
`request_path` per source over a window.

#### J. Unsolicited, unauthorised emoji-response storage fill / weak emoji-request gate
`reaction.py`. `_handle_emoji_response` stores any emoji that passes the 64 KB cap
and hash check **without** requiring it was requested and **without** a
membership/shared-channel check; distinct hashes defeat the dedup, so an
authenticated peer fills `custom_emojis` at up to the control ceiling × 64 KB.
Separately, `_shares_any_channel` returns `True` for *every* requester whenever
the victim is in any open-join channel (the open-join branch ignores the requester
identity), so the "cannot be enumerated by an arbitrary node" property does not
hold. **Fix:** require the hash to be in `_pending_emoji_requests` (or a shared
channel) on the response path; make `_shares_any_channel` actually check the
requester.

#### K. `MT_SUBSCRIBE` triggers an unconditional full-list re-broadcast (N× amplification)
`core/subscription.py`. Each inbound subscribe re-signs and re-sends the entire
subscriber list to *every* subscriber, with no check that the sender wasn't
already subscribed (`.add()` is idempotent, the broadcast is not; `_remove` does
*not* broadcast, making the asymmetry clear). One control message amplifies to N
outbound; bounded only by the 60/min control throttle. **Fix:** broadcast only
when the subscriber set actually changed.

#### L. Plaintext remnants on PIN-enable, including WAL/-shm sidecars
`core/storage.py`. Enabling a PIN unlinks the old plaintext DB rather than
overwriting it, and never handles the WAL-mode `storage.db-wal`/`-shm` sidecars
(which hold recently-written plaintext rows) or the old identity inode. Forensic
recovery from free sectors after "turning on" encryption. Extends the documented
remnant note.

#### M. Storage layer imposes no size or row caps of its own
`core/storage.py`. `insert_message` stores unbounded `content TEXT` with no
length or per-channel retention cap, and chat messages are throttle-exempt; a
member can slowly fill the victim's disk. Inbound reactions insert a peer-chosen
`(message_id, emoji_hash)` before the existence check, minting unbounded distinct
rows (and amplifying emoji fetches). Both are gated by membership + the control
throttle, so not anonymous-outsider DoS, but the storage boundary relies entirely
on upstream caps that don't exist for text/rows.

### LOW / Defense-in-depth

- **N. `messaging._on_lxmf_message` fails *open* when the channel row is absent.**
  The membership/`SEND_MESSAGE` block is under `if channel:`; a "subscribed but no
  channel row" state stores the message unchecked. `reaction._may_react` handles
  the same state by failing *closed* — messaging is the odd one out. Not
  attacker-inducible, but inconsistent. **Fix:** treat missing channel as
  unauthorised.
- **O. Tenure gap #0 (documented open) — confirmed, bound holds.** `has_any_tenure`
  gates the whole per-message tenure check channel-wide; a tenure-blind peer
  applies none. Verified the bound: the accept path opens tenure intervals
  (`invite.py` `update_tenure` alongside `replace_members`), so any genuinely
  enrolled member is not tenure-blind and the hole closes on the first accepted
  document. `full_sync` is checked against the requester's role on the responder
  side and the local role on the receiver side (not invertible). Nothing found
  worse than documented. A real fix needs per-identity provenance, not a
  channel-level flag.
- **P. INVITE is not enforced on *direct* member additions in a member-list doc**
  (`invite.py` `_signer_may_apply`/`publish_member_list` gate removals/roles but
  not `add_members`). This is *pinned as intended* by
  `test_member_without_invite_cannot_approve_join` — but it makes INVITE
  unenforceable as a restriction on a malicious/ modified admin client. Revisit if
  any deployment relies on withholding INVITE from some admins.
- **Q. `_signer_may_apply` fails *open* if the stored doc blob can't be unpacked**
  (`invite.py` `except Exception: return True`). Not reachable (we wrote the blob),
  but inconsistent with the file's fail-closed posture. **Fix:** `return False`.
- **R. Standalone-channel `creator_hash` is adopted from an *unsigned* LXMF field
  with no hash-binding** (`invite.py` auto-join upsert), unlike servers/roster
  entries which enforce `channel_hash_for`/`server_hash_for`. Bounded (a
  `member_list_versions` row shadows the creator fallback on later docs), but
  inconsistent with the deliberate server-side hardening. **Fix:** bind
  `channel_hash_for(creator, name) == channel_hash_hex` before trusting it.
- **S. Sybil-driven unbounded per-identity maps** (`avatar._last_received`,
  `reaction._emoji_request_times` keys, `router._control_rate`). Slow memory
  growth, individually tiny; no hard cap. **Fix:** cap distinct-identity entries.
- **T. Tied-timestamp group can be split by the post-merge `rows[:MAX_RESPONSE_MESSAGES]`
  slice** (`sync.py`), and the strict-`>` resume then skips the sliced-off swept
  rows permanently. Needs coincident equal timestamps + hints. **Fix:** route the
  hint+sweep merge through the group-preserving truncation.
- **U. Missed-delivery hint griefing by a member** (`sync.py`): a member writes a
  hint naming the victim for a message id that doesn't exist, flipping the victim
  to `INCOMPLETE` until the 7-day purge (the gap never clears because the message
  is never served). Throttle-bounded griefing.
- **V. Quarantine release bypasses the control throttle** (`router.py`
  `release_quarantined` → `_dispatch` directly). Bounded to 8/sender.
- **Reticulum-config rewrite & recon via the API** (`api.py` `/reticulum/interfaces`,
  `/me`, `/network/map`, `/directory`) — subsumed under finding A/B exposure; note
  that `PipeInterface` is correctly excluded from `EDITABLE_TYPES`, so there is no
  RCE-via-`command` path.

---

## 4. Verified sound (recorded so they are not re-discovered as findings)

- **Inbound authentication choke point** (`router.py`): the only path to
  `_dispatch` passes `_authenticate`; `signature_validated` required;
  `SOURCE_UNKNOWN` quarantined and re-validated from packed bytes via
  `unpack_from_bytes`; `SIGNATURE_INVALID` dropped. Quarantine bounds
  (8/sender, 128 total, 300 s TTL) enforced under lock with correct re-fetch after
  global eviction. Control rate limiter runs *after* auth, so keyed by validated
  senders only. No bypass. Well covered by `TestAdversarialUnauthenticatedDelivery`.
- **Member-list trust chain** (`invite.py`): `_validate_document` trusts *stored*
  admins/owners (self-signer fallback removed); `channel_hash` checked before
  signature; owner-set mutation requires prior-owner membership; v1 permissions
  blob untrusted; no-authority lockout rejected; malformed entry rejected before
  version commit; accept serialised under lock. `server_hash` write-once; roster
  and server entries hash-bound. Tokens single-use (atomic claim), revoked on
  kick, bound to sender, expiry + INVITE-of-signer checked. Extensively covered by
  `test_adversarial.py`.
- **Sync correlation** is atomic and replay-safe (`_claim_pending_request` pops one
  FIFO entry under lock); request authorisation fails closed on unknown channel;
  watermark never advances past withheld/rejected/failed rows; internal sweep
  never splits a timestamp group.
- **Presence / goodbye / unsubscribe**: mutate only the authenticated sender's own
  state; no field lets a peer forge presence or unsubscribe a third party; sync is
  driven by RNS-signed announces, not presence content.
- **Reaction remove path** keyed to authenticated `sender_hex` — no cross-user
  deletion or reactor spoofing.
- **Subscriber-list signatures**: non-owner rejected, unsigned rejected, Ed25519
  verified over `(channel_hash, version, packed_list)`, entries filtered to
  well-formed hex. (The gap is version *persistence*, finding F, not the signature.)
- **SQL injection: clean.** Every value is `?`-bound; the few interpolated
  identifiers are hard-coded literals or whitelist-guarded (`_KNOWN_TABLE_RE`);
  `ATTACH DATABASE ?` is parameterised; the SQLCipher hex key is a 64-char hex
  string derived from PBKDF2 (never user/peer-controlled — the one idiom that
  genuinely cannot be parameterised).
- **Local file writes**: `atomic_write_bytes` creates at 0600 via
  `mkstemp`+`fchmod` *before* writing, then `os.replace` — no world-readable
  window. `load_private_key` return value checked.
- **Flutter rendering: no injection surface.** Peer content renders through
  `Text.rich`/`TextSpan` and `Image.memory` only — no HTML, markdown,
  `flutter_html`, webview, or `url_launcher` anywhere. Classic XSS/HTML-injection
  does not apply. Core permission checks are correctly routed through `actions.py`
  and not bypassed by the API (the exposure in A is that the attacker *is* the
  served identity, not that a check is skipped).

---

## 5. Testing gaps and how to fill them

The core adversarial suite (`tests/test_adversarial.py`, 60+ cases) is genuinely
strong — it already pins the member-list trust chain, token misuse, quarantine,
sync injection, and subscriber-list signatures. The gaps cluster in three places:
**(a) the API bridge has zero tests**, **(b) several confirmed behaviours are
untested even though the code is correct**, and **(c) every new finding above is
uncovered.** Concrete additions, highest value first:

### 5.1 API bridge security tests (new file `tests/test_api_security.py`)
No test imports `create_app` today. These should be added alongside the fix for
finding A (they codify the fixed contract):

- **Auth required:** every mutating endpoint returns 401 without the session
  token; returns 200 with it. Parametrise over the endpoint table.
- **CORS locked down:** a request with `Origin: https://evil.com` does not receive
  an `Access-Control-Allow-Origin` echo; the served origin does.
- **WebSocket Origin check:** a `/ws` handshake with a foreign `Origin` is
  refused; the served origin is accepted.
- **Bind-address assertions:** unit-assert that `main_flutter.py`,
  `serve_profile.py`, `worker.py` default `host` to `127.0.0.1` (guards against a
  regression back to `0.0.0.0`). This is a pure-import assertion, no network.

Use FastAPI's `TestClient(create_app(backend))` with a `Backend` built on a
temp profile — no RNS network needed for the auth/CORS/bind checks.

### 5.2 Image fail-closed regression (finding C) — ready to drop in
```python
# tests/test_api_security.py
from fastapi.testclient import TestClient
from devtools.testenv.api import create_app

def test_send_endpoint_drops_unsanitisable_image(api_backend):
    client = TestClient(create_app(api_backend))
    # A blob prepare_image() rejects (not a valid image) but under MAX_IMAGE_BYTES.
    bad = b"\xff\xd8\xff" + b"\x00" * 1000        # JPEG SOI then garbage
    import base64
    client.post(f"/channels/{api_backend.some_channel}/messages",
                json={"content": "x", "image_data_b64": base64.b64encode(bad).decode()})
    stored = api_backend.storage.get_latest_message(api_backend.some_channel)
    assert stored["image_data"] is None      # must NOT forward raw bytes
```
This test fails today (raw bytes are stored) and passes once `api.py:875-876`
fails closed.

### 5.3 Subscriber-list cross-restart replay (finding F)
Extend `TestAdversarialSubscriberList` in `test_adversarial.py`:
- Owner publishes subscriber list v2 (removing peer C). Victim persists it.
- Simulate victim restart: construct a fresh `SubscriptionManager` over the same
  `Storage` (so `_subscriber_versions` starts empty but the roster is persisted).
- Replay the captured, genuinely-signed v1 list that still contains C.
- Assert the roster is **not** rolled back to include C.

This fails today and passes once the version is persisted and loaded in
`__init__`. Model it on the existing `test_sync_restart.py` restart pattern
(new manager, shared storage).

### 5.4 Timestamp clamp (finding G)
New `TestAdversarialSyncTimestamp` in `test_adversarial.py`:
- Peer P (a member) answers a solicited sync request with one message
  `timestamp = time.time() + 10*365*86400` (far future), valid `message_id`.
- Assert (1) the message is rejected or its timestamp clamped, and (2)
  `get_peer_sync_progress(channel, P)` did **not** jump to the future value —
  i.e. a subsequent request to P still uses a sane `since_ts`. Add a second case
  where the response cites a `message_id` that exists in another channel and
  assert the watermark did not advance (finding N4).

### 5.5 Solicited author-forgery (finding H)
- On an **open-join** channel with no tenure, a solicited responder returns a
  message with `sender_hash = <victim>` and attacker content. Document the current
  behaviour with a test that asserts either rejection (post-fix) or, if kept as a
  known gap, an explicit `xfail`-free test that pins the *documented* limitation —
  preferably by verifying a UI-visible "relayed, unverified author" marker rather
  than silent acceptance.

### 5.6 Inbound image hostile-dimensions (finding E)
`test_adversarial.py` / `test_image.py`:
- Deliver a within-cap (< 900 KB) image whose *header* declares enormous
  dimensions / thousands of GIF frames. Assert it is re-sanitised or dropped on
  receipt (post-fix), not stored raw. Today only the *byte* cap is tested
  (`test_oversized_inbound_image_is_dropped`).

### 5.7 Confirmed-correct-but-untested behaviours (add now; they pass today)
These strengthen the suite immediately without needing any fix:
- **Non-member / kicked reaction is dropped** — `_may_react` negative path
  (`test_adversarial.py`). Code is correct; no test asserts it.
- **`MT_UNSUBSCRIBE` cannot unsubscribe a third party** (`test_subscriptions.py`).
- **Emoji request from a non-member on an open-join channel** — pins finding J's
  expected post-fix behaviour and documents the current bypass.
- **`messaging` fails closed on a missing channel row** (finding N) — once fixed.
- **Quarantine path-request amplification bound** (finding I) — assert repeated
  `SOURCE_UNKNOWN` messages don't issue unbounded `request_path` calls (mock
  `RNS.Transport.request_path` and count).

### 5.8 Process note
The adversarial suite's own history (per `security-improvements.md`) is that gaps
hid because every adversary was a plain member — the admin-level authorization
holes only surfaced once `TestAdversarialAdminSigner` used a *trusted signer
exceeding their permissions*. The same lesson applies to the new gaps: the
API-bridge tests must model an **unauthenticated remote/web caller** (not the
served identity), and the sync/subscriber tests must model a **member misusing a
payload they legitimately control** (not an outsider), or they will re-test
signature validation instead of the authorization/replay/DoS gap in question.

---

## 6. Recommended priority

1. **API bridge (A/B):** session token + WS Origin check + `127.0.0.1` defaults +
   tight CORS. Single highest-impact fix; add §5.1 tests with it.
2. **Image fail-closed (C):** one-line change + §5.2 regression test. Cheapest
   high-value fix; it's a documented-fixed regression.
3. **Subscriber-list version persistence (F):** small storage addition + §5.3 test.
4. **Timestamp clamp (G/N4):** clamp on ingest + §5.4 test.
5. **Inbound image re-sanitisation (E):** route inbound bytes through the bounded
   sanitiser + §5.6 test.
6. **Emoji/quarantine amplification (I/J/K):** throttle/gate + tests.
7. **Encryption-at-rest rework (D):** the larger, deferred versioned-KDF + Scrypt +
   persistent-lockout change; own PR.
8. Low/DiD residue (N–V): fold in opportunistically; each is a small, local change.

None of the CRITICAL/HIGH items involve the Reticulum/LXMF cryptography, which
remains sound. They are the application-layer trust and local-surface gaps that
the network model, by design, leaves entirely to TrenchChat.

---

## 7. What was implemented, and what was left

Items 1–6 above are done except the PIN work, plus most of the Low/DiD residue.
Three things were deliberately *not* done, each because the honest fix is a
decision rather than a patch:

- **D — PIN / encryption at rest.** Needs a versioned KDF marker, a move to a
  memory-hard KDF, removal of the verification oracle, persistent lockout in
  `lockbox.unlock()`, and a re-key migration for existing databases. Its own
  change.
- **H — solicited sync authorship. Now fixed.** Per-message author
  signatures (`F_AUTHOR_SIG`, `core/authorship.py`) bind a message to its
  author, so a relay can neither invent one nor edit the words of one it is
  relaying. See `security-improvements.md`.
- **E, in part.** The exploitable half (decompression and frame bombs) is fixed
  by `inbound_image_is_sane`, a header-only check that decodes no pixels.
  Re-encoding every inbound image through one bounded library is the stronger
  control but is lossy and costs CPU on the low-power hardware Reticulum
  targets, and it would change an end-to-end property several tests pin
  (images arrive byte-identical). That trade belongs to whoever owns the
  product decision, not to a security pass.

Also left, with reasoning recorded in `security-improvements.md`: `INVITE` on
direct member additions (a test pins the current behaviour as intended), and
storage-level size/retention caps.
