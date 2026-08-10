# TrenchChat — Application-Layer Security

This document records the application-layer security posture of TrenchChat: what
has been hardened, and what is still open. None of it concerns Reticulum or
LXMF cryptography — X25519 + AES-256 for transport, Ed25519 for signing — which
is not in question. Every item here is about how the application *uses* that
crypto.

It supersedes the earlier version of this file, which described three gaps
(unsigned subscriber lists, display-name spoofing, no rate limiting) and
proposed fixes. One of those proposals turned out to rest on a false premise;
see "Correction" below.

---

## The root problem: LXMF signatures were never checked

**Status: fixed.**

LXMF signs every message, and TrenchChat read the sender from
`message.source_hash` — but never checked whether that signature actually
validated. Both halves of the problem are in the library's behaviour:

- `LXMessage.unpack_from_bytes` records a failed signature check on the message
  (`signature_validated = False`, `unverified_reason = SIGNATURE_INVALID`) and
  returns it normally. A bad signature is not an error.
- `LXMRouter` then calls the delivery callback **unconditionally** — there is no
  signature gate anywhere in the delivery path.

`source_hash` is attacker-chosen wire data. Setting it to a victim's LXMF
delivery hash — publicly derivable from the identity hash shown in the UI —
makes `RNS.Identity.recall()` return that victim's *real* identity, so
`sender_hex` becomes their identity hash. The signature check fails, and
previously nothing looked.

This defeated essentially every other control in the codebase, because they all
compare against a value derived from `source_hash`: the `SEND_MESSAGE` gate in
`messaging.py`, the owner check on subscriber lists, the membership check in
sync, reactions, and avatars.

**Fix** (`trenchchat/network/router.py`): `Router._on_message_received` now
authenticates before dispatching to any callback.

- `signature_validated` → dispatch.
- `SIGNATURE_INVALID` → drop and log. This is a forgery attempt, never a
  transient condition.
- `SOURCE_UNKNOWN` → the sender's identity is not known yet, which is not
  evidence of forgery. The message is held in a bounded quarantine, a path
  request is issued, and it is **re-unpacked from its original bytes** when the
  identity arrives so LXMF re-runs the real signature check. Arrival of a path
  is not itself proof the message was genuine.

The quarantine is bounded per-sender and globally, and by age, so it cannot
become its own memory-exhaustion vector.

Regression tests: `tests/test_adversarial.py::TestAdversarialUnauthenticatedDelivery`.
Note that `tests/conftest.py`'s `TestTransport` now delivers through
`Router._on_message_received` rather than around it, so this gate is exercised
by the whole suite rather than bypassed by it.

---

## Correction to the previous version of this document

The earlier text recommended, for unsigned subscriber lists, "compare
`message.source_hash` (resolved to an identity hash) against
`channel['creator_hash']`", reasoning that "LXMF already signs every message, so
the sender identity *is* authenticated at the transport layer. TrenchChat simply
doesn't check it."

Two problems:

1. That check was **already implemented** (`subscription.py`, the
   `MT_SUBSCRIBER_LIST` branch) and had been for some time.
2. The premise was wrong. LXMF authenticates the sender only if you read
   `signature_validated`, which nothing did. Comparing a spoofable
   `source_hash` against `creator_hash` is not an authentication check.

With the router gate above in place, that comparison is now meaningful. It is
still weaker than a signed document — see below.

---

## Fixed

### Unsolicited sync-response injection

A `MT_SYNC_RESPONSE` writes messages straight into the channel transcript with
the author taken from its own unsigned payload. The handler did not receive the
sender at all, let alone check it, so any peer knowing a subscribed channel hash
could inject arbitrary history attributed to arbitrary authors.

Responses are now only applied in answer to a request we actually issued
(`sync.py`, `_claim_pending_request`), and a request is consumed on use so one
solicitation cannot license a stream of injections. Correlation rather than
membership is the gate here deliberately: by design any reachable peer may serve
history — that is what makes store-and-forward work — and our local roster need
not list them. Each message in the payload is still tenure-validated
individually.

### Sync request failing open on an unknown channel

`_handle_sync_request` skipped the membership check entirely when the channel
row was missing (`if channel and not is_open_join(...)`), so a `subscriptions`
row without a matching `channels` row served private history to any requester.
Now fails closed via `_peer_may_participate`.

### Missed-delivery hints from non-members

Hints are written directly to storage and steer which messages we later serve.
An outsider could grow that table without bound, and suppress a peer's history
sync by filling it with message IDs that do not exist. Now requires channel
participation.

### Reactions bypassing membership and SEND_MESSAGE

A reaction is a write into the channel attributed to the sender, but the only
check was `is_subscribed` — which says nothing about the sender. A non-member,
or a member whose `SEND_MESSAGE` was revoked, could attach reactions to any
message on any channel whose hash they knew, and the `remove` path let them
delete other people's reactions. Now mirrors the gate in `messaging.py`.

### Emoji library enumeration and amplification

`MT_EMOJI_REQUEST` had no authorisation and no rate limit, and each small
request pulls up to 64 KB back out. Now served only to peers sharing a channel,
at a bounded rate.

### Authorisation by "is a trusted signer" rather than by permission

A valid signature proves *who* wrote a member-list document, not that they were
allowed to write it. `_accept_document` checked only that some trusted signer
had signed, then applied whatever the document contained. The permission gates
lived solely in `publish_member_list` on the sending side — which a modified
client simply does not run.

Consequences, all fixed in `invite.py::_signer_may_apply`, which diffs the
incoming document against **stored** state and checks the specific signer:

- **`MANAGE_CHANNEL` had no core enforcement at all.** `broadcast_permissions`
  had no permission check on the sending side either. Any admin could rewrite
  every role's permission set network-wide, including flipping `open_join`,
  which in turn disables the `SEND_MESSAGE` gate and the sync membership check
  for every recipient. Now gated on both sides.
- **Owner-list mutations were entirely ungated.** An admin could add themselves
  as owner and demote the real owner. Only an existing owner may now change the
  owner set; `MANAGE_ROLES` is deliberately not sufficient.
- Member removal now requires `KICK`; admin-set changes require `MANAGE_ROLES`.
- A document that would leave the channel with no admins and no owners is
  rejected. Accepting one empties `trusted_signers`, after which no future
  update can ever validate — permanently bricking the channel.

Note the deliberate exception: when there is no stored document to diff
against, these checks pass, because there is no prior state to authorise
against. The signer-trust rules in `_validate_document` are the only control
in that case — which is why the bootstrap gap below still matters.

### Concurrent member-list documents racing

`_accept_document`'s version check and its apply were not atomic. LXMF delivers
on background threads, so two documents for the same channel could both pass the
version check against the same stale value, and the loser's roster and
permissions would overwrite the winner's — a silent rollback to an older signed
document. Now serialised under a lock, with callbacks fired outside it.

### Payload limits

- Inbound `F_IMAGE_DATA` had no size cap, unlike avatars (16 KB) and emoji
  (64 KB), while those bytes are stored and later handed to Qt's C++ image
  decoders. Now capped at `MAX_IMAGE_BYTES` on both the direct and sync paths.
- `Image.MAX_IMAGE_PIXELS` is set, and GIF frame extraction is bounded.
- Image sanitisation no longer fails open: when PIL rejects an image the
  original bytes are **not** forwarded to subscribers. The re-encode is the only
  sanitisation in the pipeline, and it was previously bypassed precisely on the
  inputs it exists to catch.
- Avatar `avatar_version` is now compared before overwrite; it was documented as
  monotonic but never checked, so a replayed or older update replaced a newer
  avatar.
- Wire payloads are unpacked through `protocol.unpack_wire`, which states the
  msgpack limits explicitly rather than relying on a library default.

### Supply chain

All runtime dependencies are pinned in `requirements.txt`; release artefacts
were previously built against whatever PyPI served that day. `cryptography` is
now declared there rather than being satisfied transitively via `rns` — the PIN
feature must not depend on another package continuing to pull it in.

---

## Still open

### 1. Subscriber lists are not signed

`MT_SUBSCRIBER_LIST` is accepted on a `source_hash`-derived owner check with no
version and no signature, replacing the set wholesale. With the router gate in
place the sender is now genuinely authenticated, so the trivial forgery is
closed — but there is still no version, so an old list can be replayed to
resurrect removed subscribers or drop current ones, and the check does not
survive relaying through a propagation node.

**Recommended:** adopt the signed-document pattern already implemented for
member lists — reuse `invite.py`'s `_sign`/`_verify` and its version
monotonicity — so subscriber lists carry an owner signature and a version.
Validate that each element is well-formed hex before use.

### 2. Member-list bootstrap trust is fail-open

`_validate_document` falls back to trusting *the document's own* admins and
owners when there is no local record for the channel — the exact attack its own
docstring warns against three lines above. An unsolicited
`MT_MEMBER_LIST_UPDATE` for a channel hash the victim has never seen gets the
victim auto-subscribed to an attacker-defined channel and records the attacker
as its sole trusted signer, after which documents from the real owner are
rejected. `upsert_channel`'s `ON CONFLICT` does not update `creator_hash`, so
whoever writes first pins that authority.

**Recommended:** require an explicit, user-accepted invite before a first
document is trusted, and never auto-subscribe from an unsolicited document.

### 3. Invite tokens are multi-use and never revoked

The token is an Ed25519 signature over
`invitee_hash ‖ channel_hash ‖ expiry` — unforgeable, correctly bound to both
the channel and the invitee, and expiry cannot be extended by the submitter. But
there is no consumption ledger and no revocation, so it stays valid for its full
7-day TTL. A kicked member replays their original token to re-join, with no user
confirmation.

Separately, `remove_members` strips only the `members` list: a kicked *admin*
remains in `admins` in the stored document and therefore remains a trusted
signer, able to sign themselves back in. Kicking an admin without a matching
`remove_admins` is a no-op security-wise.

Also, the join-request handler takes the invitee from `F_INVITEE_HASH` and never
binds it to the LXMF sender, so a third party holding someone's token can force
that person into a channel without their participation.

**Recommended:** a consumed-token table keyed by token hash; invalidate on kick;
make `remove_members` also strip from `admins`/`owners`; bind the join request's
sender to the invitee.

### 4. Encryption at rest is off by default, and the PIN is weak

Without a PIN, the private key and the entire message database are stored in
plaintext. That is disclosed in the Settings UI, but it is the default for an
app whose threat model is protecting a mesh identity.

When a PIN is set, the KDF is PBKDF2-HMAC-SHA256 at 600k iterations with a
16-byte random salt — but the PIN is constrained to **4–8 numeric digits**
(`gui/pin_dialog.py`), a keyspace of 10⁴–10⁸. No iteration count rescues ~13
bits of entropy; a 4-digit PIN falls in about a second on one GPU. `lock.verify`
compounds this: it is a Fernet token over a hardcoded sentinel sitting next to
the salt, i.e. a perfect offline verification oracle. Lockout exists only in the
GUI dialog, resets to zero after each cooldown, and `lockbox.unlock()` itself is
unthrottled.

**Recommended:** allow a real passphrase (keep a PIN option but require
meaningful length), move to a memory-hard KDF — `cryptography` already ships
`Scrypt` — remove the known-plaintext oracle, and enforce lockout in
`lockbox.unlock()` with a persistent, escalating counter. Note this needs a
versioned KDF marker and a re-key migration for existing databases.

### 5. Windows has no at-rest file protection

`fileutils.secure_file()` is a no-op on Windows: it ORs in `S_IWRITE`, which
*clears* the read-only attribute and never restricts anything. The identity
file, database, and lock salt are protected only by the user-profile ACL, so any
process running as the same user can read the private key. There is no DPAPI
use. Since Windows is a primary target platform, this is the practical at-rest
story for most users.

Related, lower severity: every sensitive file is written non-atomically and
chmod'd *after* creation, leaving a brief window at the process umask; and
enabling a PIN leaves the pre-existing plaintext database recoverable from free
disk sectors, since the old file is unlinked rather than overwritten.

### 6. Display-name spoofing

`F_DISPLAY_NAME` is self-asserted and shown unverified. The sender's identity
hash *is* now authenticated, so this is a UI-presentation problem rather than a
protocol one: users see the name prominently and may not notice the hash.

**Recommended:** show a short hash badge alongside every display name, so users
associate names with hashes and can spot impersonation. A local contact book
with verified names is a stronger follow-up.

### 7. No general rate limiting on control messages

Per-sender throttles now exist for avatars and emoji requests, but there is no
general limiter across all control-message types, so a single peer can still
drive signature verification and msgpack parsing at will. A shared per-sender
token bucket in the router, applied to every control message, would close this;
note it does not help against Sybil attacks.

---

## Test architecture note

Adversarial coverage lives in `tests/test_adversarial.py`. Until recently every
adversary in that file was a plain member, which is exactly why the admin-level
authorisation gaps above went unnoticed for so long: the tests that appeared to
cover `MANAGE_CHANNEL` passed only because the attacker was not a trusted
signer, so they were testing signature validation rather than the permission.
`TestAdversarialAdminSigner` now covers a trusted signer exceeding their own
permissions, and each new control has a positive control alongside it so a
handler that rejected everything could not pass.
