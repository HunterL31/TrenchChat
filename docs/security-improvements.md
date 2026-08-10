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

## Still open

### 1. Member list bootstrap trust (narrowed, not closed)

`_validate_document` anchors the first document for a channel to, in order: a
stored member list, the channel's `creator_hash`, or an invite this user
actively accepted (recorded in `accepted_invites` when the join request is
sent). If none of those exist it falls back to the document's own signers — the
self-granted authority the function's own docstring warns about.

That branch is now narrowed to documents that **name us as a member**, so it
cannot be used to inject channels the user has nothing to do with, and it logs a
warning. It cannot simply be removed: an admin adding a member unilaterally
produces a document the recipient has no other way to anchor, and that is a
supported flow.

**Recommended:** surface such a document as a *pending invite* the user
confirms, rather than auto-creating and auto-subscribing to the channel. That is
a UI change, which is why it is not done here. It is the last remaining path to
unsolicited channel membership.

### 2. Encryption at rest is off by default, and the PIN is weak

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

### 3. Display-name spoofing

`F_DISPLAY_NAME` is self-asserted and shown unverified. The sender's identity
hash *is* now authenticated, so this is a UI-presentation problem rather than a
protocol one.

**Recommended:** show a short hash badge alongside every display name. A local
contact book with verified names is a stronger follow-up.

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
