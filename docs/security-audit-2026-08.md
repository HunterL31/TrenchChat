# TrenchChat — Full Security Audit (August 2026)

Scope: the `trenchchat/` core, the network layer, and the active Flutter UI
(`flutter_ui/` + its Python backend `devtools/testenv/api.py`, bundled for real
use by `main_flutter.py`). This audit re-verifies the hardening recorded in
`docs/security-improvements.md`, then looks for what remains.

> **Status: everything except the PIN / encryption-at-rest rework has since
> been implemented.** The findings themselves are not repeated here — each
> fix, and the reasoning behind it, lives in `docs/security-improvements.md`,
> which is the living record. What this document keeps is what that one
> cannot carry: the trust model the findings were judged against, the ground
> confirmed sound, and what was deliberately left undone.

Baseline test run at audit time: `675 passed, 1 skipped` on the non-GUI suite.
The Qt/GUI failures seen alongside it were environmental — a missing
`libEGL.so.1` — and not code defects; with that library installed the whole
suite runs. After this work, rebased onto main (voice, touch UI, link
shaping): **879 passed, 1 skipped, no failures**, GUI tests included.

---

## 1. The Reticulum trust model, and why it shaped every finding

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

Verdicts as found at audit time. The weak ones have since been fixed —
`security-improvements.md` is the current state.

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

The single most serious area was the **API bridge**: it is the one place
where a remote or web attacker needs no membership, no permission, and no crafted
protocol payload — just the ability to reach a port or lure the user to a web
page — to fully impersonate the user's cryptographic identity.

---

## 3. Verified sound (recorded so they are not re-discovered as findings)

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
- **Reaction direct/remove paths** keyed to authenticated `sender_hex` — no
  cross-user deletion, and a directly delivered reaction cannot be spoofed
  (its reactor is the LXMF-authenticated sender, not a payload field). The
  *synced* path is weaker: `_apply_synced_reactions` trusts the payload's
  `reactor` field, authorised only by `may_react` (is that identity a member
  who could react), so a relaying peer can attribute a reaction to any other
  member. Recorded as a known application-layer gap in `security-improvements.md`;
  closing it fully needs per-reaction signatures (a wire-format change).
- **Subscriber-list signatures**: non-owner rejected, unsigned rejected, Ed25519
  verified over `(channel_hash, version, packed_list)`, entries filtered to
  well-formed hex. (The gap was version *persistence*, not the signature.)
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
  and not bypassed by the API (the API bridge's exposure was that the attacker
  *is* the served identity, not that a check is skipped).

---

## 3a. Ground the second pass cleared

A second subsystem-by-subsystem pass over the same tree found the gaps now
recorded in `security-improvements.md`. What it *also* did was clear ground the
first pass never covered — voice most of all, which is absent from the posture
table above because it landed just before that audit. Recorded here for the
same reason as §3: so it is not re-derived as a finding next time.

- **Voice link identity is genuinely authenticated.** `_handle_hello` reads
  `link.get_remote_identity()`, populated only by RNS's signed `identify` and
  never from wire data, and `_peer_may_voice` keys membership off that same
  hash. A `VP_HELLO` arriving before `identify` returns early rather than
  failing open, and the channel binding is checked against the live session, so
  a member of one channel cannot stream into another's. Outbound dials build
  the destination from `Identity.recall`, so the link is encrypted to the
  intended key.
- **`voice_wire`'s parsers are strict and correct**: type byte, exact length
  for HELLO, bounded frame count and per-frame length, truncation check, and a
  no-trailing-bytes check. No offset or integer issue. Opus decode of hostile
  bytes cannot overflow — a packet declaring more samples than the frame size
  raises rather than writing past the buffer.
- **No frame relay and no signalling amplification.** Received frames are never
  forwarded; `send_frames` is driven only by local capture. An inbound
  `voice_join` produces exactly one unicast reply.
- **Third-party voice state cannot be forged.** Signalling upserts only the
  authenticated sender's own roster entry; no field lets a peer mute, remove or
  assert presence for anyone else. Mid-call revocation works: connected peers
  are re-checked against current permissions once per second and torn down.
- **Voice lock ordering is deadlock-free** — the transport never invokes a
  manager callback while holding its own lock, and `get_roster` releases the
  voice lock before calling into the transport.
- **Member-list forgery resistance held under a second look.** Anchoring order,
  signature payload shapes, the owner-set gate, the no-authority lockout check,
  roster and server hash binding, `server_hash`/`creator_hash` being write-once
  in the upsert, and the atomic token claim are all correct. Every finding in
  the second pass was against a *trusted signer* or a *legitimate* protocol
  message — none forges anything.
- **SQL injection: still clean, migrations included.** The only interpolated
  strings are a `_KNOWN_TABLE_RE`-guarded `PRAGMA table_info` and three
  SQLCipher key PRAGMAs whose interpolant is PBKDF2-derived hex. `IN (...)`
  clauses are generated placeholders. No dynamic `ORDER BY`/`LIMIT`.
- **`messages` is insert-only** — no `UPDATE`, no `DELETE`, no
  `INSERT OR REPLACE`. A sync response cannot touch another channel's rows, and
  a watermark never advances over rows withheld, rejected or failed.
- **No peer-controlled filename reaches the filesystem.** All media lives in
  SQLite blobs; `atomic_write_bytes` is only ever called with local paths. No
  subprocess is invoked on peer bytes or names. Avatar cache keys are the
  authenticated sender hash.
- **`authorship.py` is self-certifying and stayed that way** when relayed keys
  were added: a key is cached only if it hashes back to the identity claiming
  it, and the cache is first-write-wins, so a relay cannot overwrite a key
  learned from a genuine announce.
- **The Flutter client still has no injection surface.** No `dart:html`,
  `package:web`, `url_launcher`, webview, or markdown renderer anywhere in
  `lib/`; peer content renders through `Text`/`TextSpan`/`Image.memory` only.
  The one `innerHTML` in the repo is the dev harness page, not the client.

---

## 4. What was implemented, and what was left

Everything found is done except the PIN work, plus most of the Low/DiD
residue. Two things were deliberately *not* done, each because the honest fix
is a decision rather than a patch:

- **PIN / encryption at rest.** Needs a versioned KDF marker, a move to a
  memory-hard KDF, removal of the verification oracle, persistent lockout in
  `lockbox.unlock()`, and a re-key migration for existing databases. Its own
  change.
- **Inbound image re-encoding, in part.** The exploitable half — decompression
  and frame bombs — is fixed by `inbound_image_is_sane`, a header-only check
  that decodes no pixels. Re-encoding every inbound image through one bounded
  library is the stronger control but is lossy and costs CPU on the low-power
  hardware Reticulum targets, and it would change an end-to-end property
  several tests pin (images arrive byte-identical). That trade belongs to
  whoever owns the product decision, not to a security pass.

Also left, with reasoning recorded in `security-improvements.md`: `INVITE` on
direct member additions (a test pins the current behaviour as intended), and
storage-level size/retention caps.
