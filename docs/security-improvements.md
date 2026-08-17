# TrenchChat — Application-Layer Security

This document records the application-layer security posture of TrenchChat: what
has been hardened, and what is still open. None of it concerns Reticulum or
LXMF cryptography — X25519 + AES-256 for transport, Ed25519 for signing — which
is not in question. Every item here is about how the application *uses* that
crypto.

It supersedes the earlier version of this file, which described three gaps
(unsigned subscriber lists, display-name spoofing, no rate limiting) and
proposed fixes. One of those proposals rested on a false premise; see
"Correction" below.

---

## The root problem: LXMF signatures were never checked

**Status: fixed.**

LXMF signs every message, and TrenchChat read the sender from
`message.source_hash` — but never checked whether that signature actually
validated. Both halves of the problem are in the library's behaviour:

- `LXMessage.unpack_from_bytes` records a failed signature check on the message
  (`signature_validated = False`, `unverified_reason = SIGNATURE_INVALID`) and
  returns it normally. A bad signature is not an error.
- `LXMRouter` then calls the delivery callback **unconditionally**. The only
  pre-dispatch use of `signature_validated` in that path gates ticket handling,
  not delivery.

`source_hash` is attacker-chosen wire data. Setting it to a victim's LXMF
delivery hash — publicly derivable from the identity hash shown in the UI —
makes `RNS.Identity.recall()` return that victim's *real* identity, so
`sender_hex` becomes their identity hash. The signature check fails, and
previously nothing looked.

This defeated essentially every other control in the codebase, because they all
compare against a value derived from `source_hash`.

**Fix** (`network/router.py`): `_on_message_received` authenticates before
dispatching to any callback. Invalid signatures are dropped. `SOURCE_UNKNOWN` —
the sender's identity is not known yet, which is not evidence of forgery — is
held in a bounded quarantine, a path request is issued, and the message is
**re-unpacked from its original bytes** when the identity resolves so LXMF
re-runs the real check. Arrival of a path is not itself evidence of anything.

`tests/conftest.py`'s `TestTransport` now delivers through
`Router._on_message_received` rather than around it, so this gate is exercised
by the whole suite rather than bypassed by it.

---

## Correction to the previous version of this document

The earlier text recommended, for unsigned subscriber lists, comparing
`source_hash` against `channel['creator_hash']`, reasoning that "LXMF already
signs every message, so the sender identity *is* authenticated at the transport
layer. TrenchChat simply doesn't check it."

Two problems: that check was **already implemented**, and the premise was wrong.
LXMF authenticates the sender only if you read `signature_validated`, which
nothing did.

---

## Fixed

### Inbound authentication and rate limiting
- Signature enforcement with quarantine and re-validation (above).
- Per-sender throttle on all inbound **control** messages (`router.py`). Chat
  messages are exempt; a limit there would drop legitimate conversation. Avatars
  and emoji requests have their own throttles. This does not help against Sybil
  attacks.

### Subscriber lists are now signed and versioned
`MT_SUBSCRIBER_LIST` carries an owner Ed25519 signature over
`(channel_hash, version, packed_list)` and a monotonic per-channel version.
Receivers reject unsigned lists, bad signatures, and any version not newer than
what they hold, and discard entries that are not well-formed identity hex. The
subscriber set drives message delivery, so forging it redirected a peer's
outbound traffic and replaying an old one resurrected removed subscribers.

### Sync
- **Unsolicited history injection**: a `MT_SYNC_RESPONSE` writes messages into
  the transcript with the author taken from its own unsigned payload. The
  handler did not receive the sender at all. Responses now only apply in answer
  to a request we issued, consumed on use. Correlation rather than membership is
  the gate deliberately: any reachable peer may serve history by design, and our
  local roster need not list them.
- **Sync requests failing open** when the channel row was missing.
- **Missed-delivery hints** accepted from non-members.

### Reactions and emoji
Reactions bypassed membership and `SEND_MESSAGE` entirely — the only check was
`is_subscribed`, which says nothing about the sender, and the `remove` path let
an outsider delete other people's reactions. Emoji requests had no authorization
and no rate limit, allowing library enumeration and amplification.

### Authorization on member list documents
A valid signature proves *who* wrote a document, not that they were allowed to.
The permission gates lived only in `publish_member_list` on the sending side,
which a modified client does not run. `_signer_may_apply` now diffs against
**stored** state and checks the specific signer:

- `MANAGE_CHANNEL` previously had no core enforcement anywhere, and
  `broadcast_permissions` had no check on either side, so any admin could
  rewrite every role's permissions network-wide — including flipping
  `open_join`, which disables the `send_message` gate and the sync membership
  check for every recipient.
- Owner-list mutations were ungated: an admin could add themselves as owner and
  demote the real one. Only an existing owner may change the owner set.
- Member removal requires `KICK`; admin changes require `MANAGE_ROLES`.
- A document that would leave the channel with no admins and no owners is
  rejected; accepting one empties `trusted_signers` and no further update can
  ever validate.
- The `permissions` blob is applied only from v2 documents. The v1 signed
  payload does not cover that field, so a v1 blob is unauthenticated even though
  its signature verifies. Permission sets are shape-validated before storage.

### Invite tokens
Tokens are Ed25519 signatures bound to invitee, channel and expiry — unforgeable,
but bearer credentials. Now:
- **Single use**, via a `spent_invite_tokens` table; the insert is the atomic
  claim, so a replay cannot also win it.
- **Revoked on kick**, so a removed member cannot re-join by resending their
  original request.
- **Bound to the sender**: the join request must come from the identity the
  token names. Previously a third party holding someone's token could force
  that person into a channel.
- `remove_members` now also strips `admins`/`owners`. `trusted_signers` is
  derived from those lists, so a kicked admin previously kept the authority to
  sign themselves back in.

### Correctness and robustness
- `_accept_document`'s version check and apply are serialised. LXMF delivers on
  background threads, so two documents could both pass the version check against
  the same stale value and the loser's roster would overwrite the winner's — a
  silent rollback to an older signed document.
- The roster is built before the version is committed. A malformed member entry
  previously raised after the version had advanced, leaving the members table
  stale and permanently wedging the channel.
- `permissions_from_json` no longer raises; it falls back to the most
  restrictive preset. It is called on the GUI thread outside any try/except.
- `load_private_key`'s return value is checked — it returns `False` rather than
  raising, so a corrupt identity file previously surfaced as something unrelated.

### Payload limits
- Inbound `F_IMAGE_DATA` is capped on both the direct and sync paths; avatars
  (16 KB) and emoji (64 KB) were capped, message attachments were not, and those
  bytes are handed to Qt's C++ image decoders.
- `Image.MAX_IMAGE_PIXELS` set; GIF frame extraction bounded.
- Image sanitisation no longer fails open — when PIL rejects an image the
  original bytes are not forwarded. The re-encode is the only sanitisation in
  the pipeline and it was bypassed precisely on the inputs it exists to catch.
- `avatar_version` is compared before overwrite.
- Wire payloads unpack through `protocol.unpack_wire` with explicit limits.

### Local files
- `secure_file()` now restricts the ACL on Windows via `icacls`. It previously
  OR-ed in `S_IWRITE`, which *clears* the read-only attribute and restricts
  nothing, so the identity file was protected only by the profile ACL.
- Sensitive files are written atomically at 0600 from creation
  (`atomic_write_bytes`), closing both the truncate-on-failure risk to the only
  copy of the private key and the umask window. Applied to the identity, lock
  files and config.
- `ATTACH DATABASE` no longer interpolates a filesystem path into SQL.
- The propagation channel filter no longer drops the node's *own* inbound
  messages; enabling propagation-node mode with the default allowlist silently
  stopped the operator receiving their own mail, invites included.
- `channel_filter_mode` validates with a raise rather than an `assert`, which
  is stripped under `python -O`.

### Supply chain
Dependencies pinned in `requirements.txt`; release artefacts were previously
built against whatever PyPI served that day. `cryptography` is declared there
rather than relied on transitively via `rns`.

---

## Also fixed: member list bootstrap trust

`_validate_document` anchors a document to, in order: a stored member list, the
channel's `creator_hash`, or an invite this user actively accepted (recorded in
`accepted_invites` when the join request is sent). If none of those exist the
document is **rejected** — there is no longer any fallback to its own signers.

That fallback could not simply be deleted, because an admin adding a member
unilaterally produces a document the recipient has no other way to anchor, and
that is a supported flow. Such a document is now *held* rather than applied:
nothing is written, the channel is not created and not subscribed to, and the
user is prompted through the existing invite bar (`admin… added you to #channel
— join?`). Confirming records the anchor and applies the document; declining
discards it. A held document that does not name the recipient is dropped
outright.

This closes the last path to unsolicited channel membership.

---

## Fixed: the Flutter API bridge

`devtools/testenv/api.py` is the backend the Flutter client talks to, and
`main_flutter.py` ships it for real use. Every endpoint acts as the identity it
serves, and none of them authenticated anything.

- **Session token required.** `create_app(backend, token=...)` mints one per
  process (`generate_token()`), accepted as `Authorization: Bearer`, an
  `X-TC-Token` header, or a `?token=` query parameter — the last because a
  browser can set headers on neither a WebSocket handshake nor an `<img>` src.
  Paths served by a mount (the built web client) stay public; the client has to
  load before it can present a token.
- **CORS is an explicit allowlist**, never `*`. With no credentials to protect,
  a wildcard let any page the user visited read every response — which defeated
  the localhost bind entirely.
- **The WebSocket checks token and Origin** before `accept()`. Browsers apply
  neither CORS nor same-origin policy to a WS handshake, and that socket
  streams every inbound message.
- **Binds default to `127.0.0.1`** — `serve_profile.py` (which serves the *real*
  profile) defaulted to `0.0.0.0`, as did the orchestrator and its workers.
  `--host` still widens them deliberately; `remote_host.sh` passes it, since
  tailnet hosting is its purpose.
- **Image sanitisation fails closed** in the send endpoint. It forwarded the
  original bytes when `prepare_image()` raised — precisely on the inputs the
  re-encode exists to catch — while claiming to mirror the Qt handler, which
  fails closed.

Covered by `tests/test_api_security.py` (token required, CORS, WS origin,
static assets stay public, bind defaults).

## Fixed: replay, amplification and unbounded values

- **Subscriber-list versions persist** (`subscriber_list_versions`). The
  watermark was in-memory only, so a restart re-opened the replay it exists to
  stop: a captured older list stays validly signed forever, and applying one
  resurrects removed subscribers — who are exactly who delivery is aimed at.
  The version check now also commits under the same lock it read, closing a
  rollback race between two concurrent lists.
- **`MT_SUBSCRIBE` only re-broadcasts on an actual change.** Re-subscribing
  turned one inbound control message into one outbound per subscriber.
- **Peer timestamps are bounded** (`protocol.wire_timestamp`). `F_TIMESTAMP` is
  self-asserted; unbounded, a far-future value pinned a message to the top of
  the transcript and, through sync, advanced the requester's persisted
  watermark past history it never received — after which that peer was never
  asked for anything older again. Direct delivery substitutes our own clock;
  sync drops the row, because accepting it would move a watermark.
- **A sync row that stored nothing no longer advances the watermark.**
  `message_id` is globally unique, so a failed insert can mean the message
  belongs to another channel entirely — `Storage.has_message` now decides.
- **Response truncation never splits a timestamp group.** The resume point is a
  bare float and `get_messages_after` filters on a strict `>`, so half a group
  past the cut was skipped by every later sweep.
- **Quarantine path requests are throttled.** They fire before authentication
  on an attacker-chosen `source_hash`, so each unsigned packet became a
  broadcast on the shared mesh. Released messages now also pass the control
  throttle instead of arriving as one burst.
- **Emoji responses must answer a request we made**, and the shared-channel
  check names the requester on open-join channels — it previously returned true
  for anyone whenever we were in any public channel, which made it vacuous.
- **Per-identity throttle maps are capped.** Identities are free to mint, so
  these cannot be bounded by how many peers talk to us.
- **Chat messages fail closed on a missing channel row**, matching
  `reaction.py`'s `_may_react`; there is nothing to authorise against without one.
- **Inbound images are checked against a decode bound**
  (`image.inbound_image_is_sane`). The byte cap bounds the payload, not the
  raster: a file well under it can declare enormous dimensions or thousands of
  frames, and those bytes go to the client's own decoder. Header only — no
  pixel data is decoded. Applies to message images, sync images and avatars.
- **`_signer_may_apply` fails closed** when the stored document will not parse,
  and **standalone channel metadata is creator-bound** the way servers and
  roster entries already were — `creator_hash` arrives unsigned and then serves
  as a trusted-signer fallback.
- **Encrypting or decrypting the database removes the plaintext `-wal`/`-shm`
  sidecars**, which held recently written rows in the clear.

## Still open

### 0. Tenure filtering is a channel-level switch, so a tenure-blind peer is a hole

`storage.has_any_tenure(channel)` gates the *entire* per-message tenure check,
on both the responder side (`_filter_rows_by_tenure`) and the receiver side
(`_handle_sync_response`). It asks whether the channel has any tenure rows at
all, not whether this particular sender can be vouched for.

A peer that holds a roster for a closed channel but has never recorded tenure
data therefore applies no tenure filtering at all. When that peer is the
*responder*, the requester's own re-check still catches it — that is the
defence, and it is covered by
`tests/test_sync_permissions_inflight.py::TestTenureFailOpenAsymmetry`. When
the *requester* is also tenure-blind, nothing in the exchange checks tenure and
a message from a member kicked elsewhere in the mesh is accepted.

Failing closed on "closed channel with no tenure rows" is **not** the fix: a
roster without tenure is a legitimate state (bootstrapped or seeded peers,
channels predating the feature), and rejecting sync there breaks working
peers — `tests/test_adversarial.py::test_sync_response_cannot_be_replayed`
pins exactly that case. A real fix needs per-identity provenance rather than a
channel-level flag, so that "I cannot vouch for this sender" is distinguishable
from "this channel has no tenure history".

Bounded by: it only affects peers with no tenure data for a channel, and any
accepted member-list document opens tenure intervals, so the window closes as
soon as one arrives.

### 0b. Synced messages carry no author signature

"Unsolicited history injection" above is fixed — a `MT_SYNC_RESPONSE` only
applies against a request we issued. The *solicited* path still trusts the
responder for authorship: `sender_hash`, `sender_name` and `content` come from
the responder's own payload, and the original author's LXMF signature is not
carried through sync, so it cannot be re-checked.

What that costs depends on the channel. With tenure data, a forged author has
to be someone who was a member at the claimed timestamp — a malicious member
can attribute a message to a co-member, not to an outsider. On a public channel
there is no tenure, so a peer we asked can attribute a message to any identity.
The timestamp clamp above bounds *when* they can claim it was said.

Closing this properly means propagating per-message author signatures through
sync so a relayed message is verifiable independently of who relayed it. That
is a protocol change (a new signed field, and a decision about what to do with
pre-existing unsigned history), so it is recorded here rather than patched
around. `docs/proposal-author-signatures.md` works the design through and lists
the four policy decisions it needs. Until then, "a peer relayed this" and "this peer wrote this" are
different claims, and only the former is authenticated on the sync path.

### 1. Encryption at rest is off by default, and the PIN is weak

Without a PIN the private key and the entire message database are stored in
plaintext. That is disclosed in the Settings UI, but it is the default.

When a PIN is set the KDF is PBKDF2-HMAC-SHA256 at 600k iterations with a
16-byte random salt — but the PIN is constrained to **4–8 numeric digits**
(`gui/pin_dialog.py`), a keyspace of 10⁴–10⁸. No iteration count rescues ~13
bits of entropy; a 4-digit PIN falls in about a second on one GPU.
`lock.verify` compounds this: a Fernet token over a hardcoded sentinel sitting
next to the salt, i.e. an offline verification oracle. Lockout exists only in
the GUI dialog, resets after each cooldown, and `lockbox.unlock()` is
unthrottled.

**Recommended:** allow a real passphrase (keep a PIN option but require
meaningful length), move to a memory-hard KDF — `cryptography` already ships
`Scrypt` — remove the oracle, and enforce lockout in `lockbox.unlock()` with a
persistent, escalating counter. Needs a versioned KDF marker and a re-key
migration for existing databases, so it belongs in its own change.

Related: enabling a PIN leaves the pre-existing plaintext database recoverable
from free disk sectors, since the old file is unlinked rather than overwritten.

### 2. Display-name spoofing (largely addressed)

`F_DISPLAY_NAME` is self-asserted and never verified. The earlier version of
this document recommended showing a short hash badge alongside every display
name; that is **already implemented** — `gui/channel_view.py` renders every
message header as `Alice [a3f1c2d4]`. Combined with signature enforcement, the
identity hash shown is now authenticated rather than merely claimed.

What remains is optional hardening rather than a gap: a local contact book that
lets a user pin a verified name to an identity hash and warns when a familiar
display name arrives under a different hash.

---

### 3. Minor residue

None of these are exploitable on their own; recorded so they are not
rediscovered as findings.

- 14 `except Exception:` blocks across `core/` and `network/` swallow failures
  silently. Fail-closed where it matters, but a systematic verification failure
  is indistinguishable from a single malformed message.
- macOS builds are unsigned (`trenchchat.spec` sets `codesign_identity=None`) —
  a distribution-trust matter rather than an application one.
- Inbound image bytes that do not parse as an image are stored as-is. They
  cannot be shown to be hostile and are opaque blobs either way, but the
  client's decoder is still what eventually reads them. Re-encoding every
  inbound image through one bounded library would normalise them; it is lossy
  and costs CPU on the low-power hardware Reticulum targets, so it is a product
  call rather than a straight win. `inbound_image_is_sane` covers the
  resource-exhaustion half of this today.
- `INVITE` is not enforced on direct member *additions* in a member-list
  document, only on the token/join-request path — so it does not restrain a
  modified admin client. `tests/test_adversarial.py::
  test_member_without_invite_cannot_approve_join` pins this as intended;
  revisit if a deployment ever relies on withholding `INVITE` from an admin.
- Storage imposes no length cap on message `content` and no retention limit,
  and chat messages are exempt from the control throttle by design, so a member
  can grow the database steadily. Bounded by membership, not by rate.

---

## Test architecture note

Adversarial coverage lives in `tests/test_adversarial.py`. Until recently every
adversary in that file was a plain member, which is why the admin-level
authorization gaps went unnoticed: the tests that appeared to cover
`MANAGE_CHANNEL` passed only because the attacker was not a trusted signer, so
they were testing signature validation rather than the permission.

`TestAdversarialAdminSigner` now covers a trusted signer exceeding their own
permissions, and each new control has a positive control alongside it so a
handler that rejected everything could not pass.

---

## Server rosters are capability claims

A server's member-list document carries a *roster* — a signed list of the
channels in that server. On accept each entry becomes a local channel row
parented under the server, and membership, roles and permissions all resolve up
to that server. Every roster entry is therefore a capability claim.

A malicious roster naming a channel the receiver is already in would hand the
server's members that channel's membership and history. Four independent
defences apply:

1. `channels.server_hash` is write-once — absent from `upsert_channel`'s
   `ON CONFLICT` clause, so no upsert can re-parent an existing channel.
2. Any roster hash already known locally under a different `server_hash` is
   refused outright.
3. Each entry must be hash-bound: `channel_hash_for(creator, name)` has to
   equal the claimed hash. `RNS.Destination.hash()` accepts a raw identity
   hash, so this is computable offline, and preimage resistance means an
   attacker cannot produce a `(creator, name)` pair for a hash they did not
   mint. The same binding is applied to the server itself before
   `upsert_server`, blocking impersonation via the unsigned name/creator fields.
4. Adding a channel to a roster requires `CREATE_CHANNEL`, checked in
   `_signer_may_apply` against the signer's role in the *previously stored*
   document.

The anchor check runs strictly before the server row is created, so an
unanchored document cannot mint the very anchor it would be checked against.

Two things this used to say are now handled by the member-list bootstrap work
above: the trust tier that fell back to a document's own signers is gone, and
the durable `accepted_invites` anchor replaced an in-memory map that did not
survive a restart.

