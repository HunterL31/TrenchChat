# TrenchChat: Application-Layer Security

This document records the application-layer security posture of TrenchChat: what
has been hardened, and what is still open. None of it concerns Reticulum or
LXMF cryptography (X25519 + AES-256 for transport, Ed25519 for signing), which
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
`message.source_hash`, but never checked whether that signature actually
validated. Both halves of the problem are in the library's behaviour:

- `LXMessage.unpack_from_bytes` records a failed signature check on the message
  (`signature_validated = False`, `unverified_reason = SIGNATURE_INVALID`) and
  returns it normally. A bad signature is not an error.
- `LXMRouter` then calls the delivery callback **unconditionally**. The only
  pre-dispatch use of `signature_validated` in that path gates ticket handling,
  not delivery.

`source_hash` is attacker-chosen wire data. Setting it to a victim's LXMF
delivery hash (publicly derivable from the identity hash shown in the UI)
makes `RNS.Identity.recall()` return that victim's *real* identity, so
`sender_hex` becomes their identity hash. The signature check fails, and
previously nothing looked.

This defeated every other control in the codebase, because they all
compare against a value derived from `source_hash`.

**Fix** (`network/router.py`): `_on_message_received` authenticates before
dispatching to any callback. Invalid signatures are dropped. `SOURCE_UNKNOWN`
(the sender's identity is not known yet, which is not evidence of forgery) is
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
Reactions bypassed membership and `SEND_MESSAGE` entirely; the only check was
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
  rewrite every role's permissions network-wide, including flipping
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
Tokens are Ed25519 signatures bound to invitee, channel and expiry, unforgeable,
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
  the same stale value and the loser's roster would overwrite the winner's, a
  silent rollback to an older signed document.
- The roster is built before the version is committed. A malformed member entry
  previously raised after the version had advanced, leaving the members table
  stale and permanently wedging the channel.
- `permissions_from_json` no longer raises; it falls back to the most
  restrictive preset. It is called on the GUI thread outside any try/except.
- `load_private_key`'s return value is checked, it returns `False` rather than
  raising, so a corrupt identity file previously surfaced as something unrelated.

### Payload limits
- Inbound `F_IMAGE_DATA` is capped on both the direct and sync paths; avatars
  (16 KB) and emoji (64 KB) were capped, message attachments were not, and those
  bytes are handed to the client's image decoders.
- `Image.MAX_IMAGE_PIXELS` set; GIF frame extraction bounded.
- Image sanitisation no longer fails open, when PIL rejects an image the
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
- The per-channel propagation filter has been removed, along with the config
  keys and both clients' UI for it. A node ingests
  `destination_hash + ciphertext`, so the channel field a filter reads is
  never in the clear: allowlist mode relayed nothing whatever was listed, and
  the wrapper it installed also sat in front of the node's *own* inbound mail,
  dropping messages the propagation node then deleted its copy of. An earlier
  entry here claimed that second half was fixed; it was not. Nothing may wrap
  `lxmf_propagation`, `TestPropagationRelayCannotBeFiltered` in
  `tests/test_actions.py` holds that line.

### Supply chain
Dependencies pinned in `requirements.txt`; release artefacts were previously
built against whatever PyPI served that day. `cryptography` is declared there
rather than relied on transitively via `rns`.

---

## Also fixed: member list bootstrap trust

`_validate_document` anchors a document to, in order: a stored member list, the
channel's `creator_hash`, or an invite this user actively accepted (recorded in
`accepted_invites` when the join request is sent). If none of those exist the
document is **rejected**; there is no longer any fallback to its own signers.

That fallback could not simply be deleted, because an admin adding a member
unilaterally produces a document the recipient has no other way to anchor, and
that is a supported flow. Such a document is now *held* rather than applied:
nothing is written, the channel is not created and not subscribed to, and the
user is prompted through the existing invite bar (`admin… added you to #channel,
join?`). Confirming records the anchor and applies the document; declining
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
  `X-TC-Token` header, or a `?token=` query parameter, the last because a
  browser can set headers on neither a WebSocket handshake nor an `<img>` src.
  Paths served by a mount (the built web client) stay public; the client has to
  load before it can present a token.
- **CORS is an explicit allowlist**, never `*`. With no credentials to protect,
  a wildcard let any page the user visited read every response, which defeated
  the localhost bind entirely.
- **The WebSocket checks token and Origin** before `accept()`. Browsers apply
  neither CORS nor same-origin policy to a WS handshake, and that socket
  streams every inbound message.
- **Binds default to `127.0.0.1`**: `serve_profile.py` (which serves the *real*
  profile) defaulted to `0.0.0.0`, as did the orchestrator and its workers.
  `--host` still widens them deliberately; `remote_host.sh` passes it, since
  tailnet hosting is its purpose.
- **Image sanitisation fails closed** in the send endpoint. It forwarded the
  original bytes when `prepare_image()` raised (precisely on the inputs the
  re-encode exists to catch) while claiming to mirror the Qt handler, which
  fails closed.

Covered by `tests/test_api_security.py` (token required, CORS, WS origin,
static assets stay public, bind defaults, and a route added to the app after
`create_app` returns, which is how `main_flutter.py` adds `/ui/open`).

**The token is also written to `~/.trenchchat/launcher.json`** (owner-only,
`atomic_write_bytes`) so a second launch can ask the instance in the tray to
open a window instead of starting a second node over the same identity and
database. This adds no exposure: a reader of that file can already read the
identity keypair and the message database beside it. It does mean the token
now outlives a single request, a stale file names a port that answers
nothing, and the launcher starts normally when it does.

## Fixed: replay, amplification and unbounded values

- **Subscriber-list versions persist** (`subscriber_list_versions`). The
  watermark was in-memory only, so a restart re-opened the replay it exists to
  stop: a captured older list stays validly signed forever, and applying one
  resurrects removed subscribers, who are exactly who delivery is aimed at.
  The version check now also commits under the same lock it read, closing a
  rollback race between two concurrent lists.
- **`MT_SUBSCRIBE` only re-broadcasts on an actual change.** Re-subscribing
  turned one inbound control message into one outbound per subscriber.
- **Peer timestamps are bounded** (`protocol.wire_timestamp`). `F_TIMESTAMP` is
  self-asserted; unbounded, a far-future value pinned a message to the top of
  the transcript and, through sync, advanced the requester's persisted
  watermark past history it never received, after which that peer was never
  asked for anything older again. Direct delivery substitutes our own clock;
  sync drops the row, because accepting it would move a watermark.
- **A sync row that stored nothing no longer advances the watermark.**
  `message_id` is globally unique, so a failed insert can mean the message
  belongs to another channel entirely, `Storage.has_message` now decides.
- **Response truncation never splits a timestamp group.** The resume point is a
  bare float and `get_messages_after` filters on a strict `>`, so half a group
  past the cut was skipped by every later sweep.
- **Quarantine path requests are throttled.** They fire before authentication
  on an attacker-chosen `source_hash`, so each unsigned packet became a
  broadcast on the shared mesh. Released messages now also pass the control
  throttle instead of arriving as one burst.
- **Emoji responses must answer a request we made**, and the shared-channel
  check names the requester on open-join channels, it previously returned true
  for anyone whenever we were in any public channel, which made it vacuous.
- **Per-identity throttle maps are capped.** Identities are free to mint, so
  these cannot be bounded by how many peers talk to us.
- **Chat messages fail closed on a missing channel row**, matching
  `reaction.py`'s `_may_react`; there is nothing to authorise against without one.
- **Inbound images are checked against a decode bound**
  (`image.inbound_image_is_sane`). The byte cap bounds the payload, not the
  raster: a file well under it can declare enormous dimensions or thousands of
  frames, and those bytes go to the client's own decoder. Header only, no
  pixel data is decoded. Applies to message images, sync images and avatars.
- **`_signer_may_apply` fails closed** when the stored document will not parse,
  and **standalone channel metadata is creator-bound** the way servers and
  roster entries already were, `creator_hash` arrives unsigned and then serves
  as a trusted-signer fallback.
- **Encrypting or decrypting the database removes the plaintext `-wal`/`-shm`
  sidecars**, which held recently written rows in the clear.

## Fixed: synced messages are bound to their author

A message reaching you through sync came from a peer who usually did not
write it. LXMF authenticates that relay and nothing else, so the author, the
text, the attachment and the threading fields were all just claims in the
relay's payload -- a relay could rewrite the words of a genuine message and
serve them under the original author and id.

`message_id` did not help: it is `sha256(content:sender:timestamp)` but was
never recomputed on receipt, and it is `UNIQUE`, so a tampered copy landing
first took the id permanently and the genuine message was then discarded as a
duplicate. First writer wins on a field anyone could forge.

**Fix.** Authors sign a canonical digest (`protocol.author_digest`) covering
channel, message id, timestamp, content, an image digest and the threading
fields, carried in `F_AUTHOR_SIG` (0x70) and stored alongside the message.
Both ingest paths verify before storing; the signature is checked against the
payload exactly as it arrived, before any of it is stripped. `sender_name` is
deliberately not covered -- it is mutable, and signing it would fail on
rename.

Verification needs the author's public key, not their hash. `core/authorship.py`
keeps a local key cache, and every key is checked to hash back to the identity
claiming it -- self-certifying, which is what makes a key safe to accept from
any source. A key learned once keeps working after that peer goes quiet.

Unsigned rows are refused, and a responder withholds them rather than serving
rows the requester will reject: a rejected row advances nothing, so the
requester would otherwise re-request the same window forever. Refusing an
oversized or bomb image clears the signature with it, so the row stays
readable locally and simply never relays.

Rejection is also reported rather than silent. `SyncStatusTracker` counted a
peer as having answered regardless of whether any of its rows survived, so a
relay serving nothing but tampered history left the channel reading as
SYNCED while the real messages were still missing. Rows refused for failing
verification now mark the channel INCOMPLETE and are counted per peer
(`messages_rejected`); rows withheld by our own tenure checks are not, since
history we are not entitled to is not history we are missing.

Covered by `tests/test_authorship.py` (digest pinned against a committed
vector), `TestAdversarialRelayTampering` (edit, re-thread, id-squat, invented
message, and the status claim, each with a positive control) and
`TestRejectedRowsAreNotCaughtUp`.

## Fixed: the August 2026 second pass

A second audit over the same tree, subsystem by subsystem. The first pass had
hardened *document forgery* (signatures, versioning, trusted-signer
anchoring), and that held: nothing below forges anything. What it found were
four patterns the first pass had not systematically looked for.

**Authority taken from data that is authenticated but not authorized.**

- `KICK` alone deposed the owner. A role is derived from `doc["members"]`, so
  an identity absent from it has no row and therefore no permissions,
  including the owner short-circuit. `_signer_may_apply` gated removals on
  `KICK` and never asked *who* was being removed, so an admin could publish a
  document identical to the stored one except that the owner was dropped from
  `members`, leaving `owners` untouched so its own gate never fired. Removal
  now checks the target: an owner may only be removed by an owner, an admin
  only with `MANAGE_ROLES`.
- An unsigned channel announce wrote the `permissions` column. `upsert_channel`
  protected `creator_hash` and `server_hash` from being overwritten this way
  but not `permissions`, so a demoted creator (still holding the destination)
  could announce their private channel as public and flip `open_join`, after
  which the message handler stops checking membership and `SEND_MESSAGE`
  entirely. A known channel's announce now refreshes only name and
  description, and `creator_hash` comes from the announcing identity rather
  than the payload.
- Tenure was repaired from message timestamps. `_repair_tenure_from_message_history`
  widened a member's join time to cover any older stored message from them,
  but that timestamp is self-asserted and bounded only against the future, so
  one backdated message bought history from before they joined. It now uses
  `received_at`, which is our own clock.
- `kick` and `manage_roles` were grantable to the base member role, where the
  resulting document was rejected by every recipient anyway (scenario
  invite11). Narrowed rather than widened: both are dropped from the member
  role on read and on write. `manage_roles` goes with `kick` because promoting
  yourself is how you would grant yourself `kick`.

**Local state standing in for distributed state.**

- A kicked member could rejoin through a second admin. The spent-token table
  and the revocation sentinel are written only by the peer that saw the
  redemption or performed the kick; a peer that merely accepted the removal
  document held neither, and the join-request handler is fully automatic. Invite
  tokens now bind their issue time (`F_INVITE_ISSUED_TS`), and any peer holding
  the signed departure refuses a token issued before it. A token with no bound
  issue time predates the field and cannot be dated, so it loses. Fixing this
  also fixed re-inviting: the sentinel was keyed on the invitee, so a fresh
  invite after a kick was refused for the sentinel's whole TTL.

**Wire values used unvalidated in ordering and windowing.**

- A member-list `version` of `float("inf")` was stored and then compared
  greater than every later document forever, no kick, promotion or join could
  be applied to that channel again, and the poisoned peer re-broadcast it.
  Version must now be a bounded int and `published_at` must pass
  `wire_timestamp`.
- `message_id` came off the wire into a globally-UNIQUE column, so a member
  could mint a validly-signed message under an id they had seen elsewhere and
  make the genuine copy a silent duplicate forever. It is recomputed on ingest.
- `F_SYNC_WINDOW_START` is still unbounded, but the row it poisons was split
  (`sync_served`), so it now denies the sender rather than us.

**Caps that counted the wrong thing, or nothing.**

- Voice counted the signalled roster while fan-out is driven by links, so a
  peer that dialled in without signalling was uncounted *and* invisible in the
  participant list. Both now include established links.
- Sync responses were capped at 50 rows with no byte budget: 50 image rows pack
  to ~45 MB against a 4 MB parse limit, so a window holding a few images could
  never sync at all while each attempt put tens of megabytes on the air.
- Emoji was the one image ingestion path that never checked the declared
  decode, and it is the one rendered inline and re-served to whoever asks.
  Fetching also had no membership gate and no throttle; the path is exempt
  from the control rate limit by design, so one message from a stranger bought
  an outbound request per token it named.
- `inbound_image_is_sane` was not header-only: `Image.open` walks a TIFF's
  whole IFD chain and fully decodes an ICO frame. Format is now checked by
  magic bytes first, which also closes the fail-open case for a format Pillow
  cannot read but a browser can.
- Quarantine path requests were throttled per source, keyed on `source_hash`
  (unverified at that point), so rotating it made every bucket fresh. A global
  ceiling is the bound that actually holds.

**Also fixed:** a decoded voice frame of the wrong length reached the mixer and
killed the only thread driving playback; `?api=` accepted any absolute URL, so
a page could load the real client from the user's own origin and take
everything typed into it; the `F_AUTHOR_KEYS` map was unbounded and its pairs
cost a hash rather than a keypair; peer image bytes rendered without a decode
bound; the propagation filter's verdict was returned from LXMF's delivery
callback, whose return value it ignores, so the Settings option governed
nothing; `Host` was unchecked while the socket's same-origin test read its
answer out of that header; and the remaining unbounded per-peer maps are
capped.

## Still open

### 0a. Friend requests are an unsolicited inbound surface

`MT_FRIEND_REQUEST` can be sent by any identity that can reach this node, and
it writes a `pending_in` row plus a UI prompt carrying attacker-chosen text
(`F_FRIEND_NOTE`, capped at 140 characters). What bounds it today: the router's
per-sender control throttle (60/min), a cap of
`MAX_PENDING_FRIEND_REQUESTS` rows evicted oldest-first, and that a request
grants nothing: only the local user accepting does.

What is not bounded: identities are free to mint, so a sender rotating them
can keep the pending queue churning at the throttle's rate, and declining does
not remember the refusal, so the same peer may ask again. A durable blocklist
is the fix if this is abused. See `docs/direct-messages.md` for why it was left
out for now.

Note what an accept cannot do: `MT_FRIEND_ACCEPT` from an identity we never
asked is ignored outright (`FriendsManager._handle_accept`), so the direct-
message gate is never one unsolicited control message away from anybody,
`tests/test_adversarial.py::TestDirectMessageGate` pins this.

### 0a-bis. Messages from unaccepted senders are held, on a path with no throttle

A direct message from someone not accepted is no longer dropped: it is held as a
message request, because a client speaking only plain LXMF cannot send
`MT_FRIEND_REQUEST` and so had no way to reach anyone at all. See
`docs/direct-messages.md` for why that mattered.

The surface this adds is the same shape as 0a, with one difference that matters:
**a direct message carries no `F_MSG_TYPE`, so it is deliberately exempt from
the router's per-sender control throttle**; a limit there would drop
conversation. Friend requests are paced by that throttle and by
`MAX_PENDING_FRIEND_REQUESTS`; this queue has only the caps, so all of them are
enforced where the row is written rather than by any caller:
`MAX_REQUEST_BODY_CHARS` on the body, `MAX_HELD_PER_SENDER` per sender,
`MAX_HELD_MESSAGES` in total, `MESSAGE_REQUEST_TTL_SECS` by age, and the
`pending_in` cap above them, evicted oldest-first and taking held messages with
it. `tests/test_adversarial.py::TestAdversarialMessageRequests` pins each one,
including under rotating identities.

Attachments are not held at all, so nothing from an unaccepted sender is stored
that a person did not choose to receive as text. Holding grants nothing: the
sender stays unaccepted, no conversation exists, and no reply can pass until the
user accepts, the same property 0a records for a request.

The missing throttle is therefore deliberate and compensated, not an oversight.

### 0b. A propagation node learns who talks to whom

Direct messages to an offline friend go through an LXMF propagation node,
which sees both endpoints' delivery addresses, the message size and the timing,
though never the content. This is inherent to storing a message for an absent
recipient, not a defect in the implementation, but it is a metadata exposure
channels do not have, since a channel's sync responder was already a member.
Preferring the fewest-hop node keeps it as local as the mesh allows.

### 0. Tenure filtering is a channel-level switch, so a tenure-blind peer is a hole

`storage.has_any_tenure(channel)` gates the *entire* per-message tenure check,
on both the responder side (`_filter_rows_by_tenure`) and the receiver side
(`_handle_sync_response`). It asks whether the channel has any tenure rows at
all, not whether this particular sender can be vouched for.

A peer that holds a roster for a closed channel but has never recorded tenure
data therefore applies no tenure filtering at all. When that peer is the
*responder*, the requester's own re-check still catches it; that is the
defence, and it is covered by
`tests/test_sync_permissions_inflight.py::TestTenureFailOpenAsymmetry`. When
the *requester* is also tenure-blind, nothing in the exchange checks tenure and
a message from a member kicked elsewhere in the mesh is accepted.

Failing closed on "closed channel with no tenure rows" is **not** the fix: a
roster without tenure is a legitimate state (bootstrapped or seeded peers,
channels predating the feature), and rejecting sync there breaks working
peers, `tests/test_adversarial.py::test_sync_response_cannot_be_replayed`
pins exactly that case. A real fix needs per-identity provenance rather than a
channel-level flag, so that "I cannot vouch for this sender" is distinguishable
from "this channel has no tenure history".

Bounded by: it only affects peers with no tenure data for a channel, and any
accepted member-list document opens tenure intervals, so the window closes as
soon as one arrives.

### 1. Encryption at rest is off by default, and the PIN is weak

Without a PIN the private key and the entire message database are stored in
plaintext. That is disclosed in the Settings UI, but it is the default.

When a PIN is set the KDF is PBKDF2-HMAC-SHA256 at 600k iterations with a
16-byte random salt, but the PIN is constrained to **4–8 numeric digits**
(`gui/pin_dialog.py`), a keyspace of 10⁴–10⁸. No iteration count rescues ~13
bits of entropy; a 4-digit PIN falls in about a second on one GPU.
`lock.verify` compounds this: a Fernet token over a hardcoded sentinel sitting
next to the salt, i.e. an offline verification oracle. Lockout exists only in
the GUI dialog, resets after each cooldown, and `lockbox.unlock()` is
unthrottled.

**Recommended:** allow a real passphrase (keep a PIN option but require
meaningful length), move to a memory-hard KDF (`cryptography` already ships
`Scrypt`), remove the oracle, and enforce lockout in `lockbox.unlock()` with a
persistent, escalating counter. Needs a versioned KDF marker and a re-key
migration for existing databases, so it belongs in its own change.

Related: enabling a PIN leaves the pre-existing plaintext database recoverable
from free disk sectors, since the old file is unlinked rather than overwritten.

### 2. Display-name spoofing, largely addressed

`F_DISPLAY_NAME` is self-asserted and never verified. The earlier version of
this document recommended showing a short hash badge alongside every display
name; that is **already implemented**, `gui/channel_view.py` renders every
message header as `Alice [a3f1c2d4]`. Combined with signature enforcement, the
identity hash shown is now authenticated rather than merely claimed.

What remains is optional hardening rather than a gap: a local contact book that
lets a user pin a verified name to an identity hash and warns when a familiar
display name arrives under a different hash.

---

### 3. Reactor spoofing on the synced path

A directly delivered reaction cannot be spoofed: `reaction.py` keys the reactor
to the LXMF-authenticated sender and ignores any payload-supplied identity. The
synced path is weaker. `sync.py::_apply_synced_reactions` trusts the payload's
`reactor` field, authorised only by `may_react` (is that identity a member who
*could* react), not that the identity actually did. So a relaying peer serving a
sync response can attribute a reaction to any other member. The blast radius is
small (a forged reaction decorating an existing message; no message forgery, no
deletion), which is why it is filed here rather than as a fix.

`test_adversarial.py::test_a_reaction_from_a_real_member_is_kept` currently pins
third-party reaction backfill as *intended* (a member's reaction propagates
even when relayed by someone else), so this gap is the price of that feature as
built. Closing it without losing backfill needs per-reaction signatures (the
reactor signs `(channel_hash, message_id, emoji, reactor, at)`, verified on
sync-apply): a wire-format addition, forward-compatible but a protocol change.
The cheaper alternative (trust a synced reaction only when the relayer is the
reactor) closes the gap but drops offline backfill of other members' reactions.
Left open pending that product/protocol call. The earlier
`security-audit-2026-08.md` wrongly listed this as closed; corrected there.

---

### 3-bis. A relayed message is not checked against its author's permissions

The direct path drops a message from a member whose role lacks `SEND_MESSAGE`,
and drops a file manifest from one whose role lacks `SHARE_FILES`
(`Messaging._on_lxmf_message`). The sync path does not: a relayed row is checked
for an author signature, a message id that hashes its own content, and tenure at
its timestamp, and then stored. So a message its author was not permitted to send
can still arrive as history, from any member who kept a copy.

This predates file sharing (`SEND_MESSAGE` has always had the gap) and
`SHARE_FILES` now shares it. It is narrower than it sounds: the author is
cryptographically bound to the row, so the relay cannot forge one, and a role
change is not retroactive, a message sent while permitted is legitimate history
that a later demotion should not erase. What is missing is the case where the
author never had the permission at all.

The fix is a permission check beside the tenure check in `_handle_sync_response`,
resolved against the author's role rather than the relayer's. It is not made here
because the two questions ("was this permitted when sent" and "is it permitted
now") need the same tenure record that the tenure check uses, and answering the
wrong one silently drops real history.

### 4. Minor residue

None of these are exploitable on their own; recorded so they are not
rediscovered as findings.

- 14 `except Exception:` blocks across `core/` and `network/` swallow failures
  silently. Fail-closed where it matters, but a systematic verification failure
  is indistinguishable from a single malformed message.
- macOS builds are unsigned (`trenchchat.spec` sets `codesign_identity=None`),
  a distribution-trust matter rather than an application one.
- Inbound image bytes of a recognised format that do not parse are stored
  as-is. Bytes of *no* recognised format are now refused outright, so this is
  narrower than it was. Re-encoding every inbound image through one bounded
  library would normalise the rest; it is lossy and costs CPU on the low-power
  hardware Reticulum targets, so it is a product
  call rather than a straight win. `inbound_image_is_sane` covers the
  resource-exhaustion half of this today.
- `INVITE` is not enforced on direct member *additions* in a member-list
  document, only on the token/join-request path, so it does not restrain a
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

A server's member-list document carries a *roster*, a signed list of the
channels in that server. On accept each entry becomes a local channel row
parented under the server, and membership, roles and permissions all resolve up
to that server. Every roster entry is therefore a capability claim.

A malicious roster naming a channel the receiver is already in would hand the
server's members that channel's membership and history. Four independent
defences apply:

1. `channels.server_hash` is write-once, absent from `upsert_channel`'s
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

---

## Shared files in invite-only channels: the trust model

A shared file never travels with the message. The message carries a manifest
(name, size, SHA-256, chunk root) under 200 bytes, and the bytes move only when
a member asks a holder for a range of chunks over the file plane
(`network/file_transport.py`, aspect `files`). Invite-only channels only: an
open-join channel has no member list to authorise a serve against, so
`actions.file_share_refusal` refuses a manifest there.

**What a member learns.** Every manifest, because it rides the message. A holder
also learns which member asked it for which file, and when. Members already hold
each other's identities through the member list, so this adds "who fetched what"
inside a set that already knows who everyone is, and nothing outside it.

**What a non-member learns.** Nothing. The request path is hashed on the wire,
the manifest never leaves the channel's encrypted messages, and the answer to an
unauthorised request is silence rather than a refusal code, so a probe cannot
tell "not a member" from "no such file".

**What a holder can do.** Refuse, stall, or serve wrong bytes. The first two move
the requester to the next holder at the same chunk index; the third is caught by
the chunk hash on arrival, and that holder is skipped for the rest of the
download, at the cost of one chunk of airtime. A holder cannot forge a file into
a channel: the author's signature covers the manifest, chunk root included, so
every chunk is verified against a list the signature anchors, and the assembled
file against the signed hash.

**What a sender can do.** Share a manifest whose bytes nobody can serve. Members
see the download park as `unavailable` until a holder appears. That is the same
outcome as a message nobody received and is not treated as an attack.

**The serve gate**, in `FileManager`, in this order: the requester must have
identified on the link; the file must be held locally; some channel it was
shared in must be invite-only with that identity in the **stored** member list
(never a claim from the request); and the concurrent-serve cap must have a slot.
Every failure returns `None`, which RNS answers with nothing at all, and logs a
warning naming the identity. This is the core layer of the three, the one that
holds when a peer calls in directly rather than through a client.

**Presence orders holders and never gates them.** Presence is evidence of having
*heard* a peer, so a quiet member is not a known-absent one: a transport node
damps repeat announces and the liveness beacon informs only its receiver. Asking
a quiet member costs one dial that fails; not asking cost the whole download, and
did, until the scenario suite found it.

**Bounds.** Inbound: manifest field caps, `max_response_size` derived from the
chunk count so an oversized answer is refused before it is buffered, a stall
timeout per request, one request in flight per download and one download per
node, a partial TTL, and an LRU store budget. Outbound: per-link serve rate
limit, inbound link cap, concurrent serve cap, served-size cap. Nothing a peer
sends grows the store: downloads start on the user's click, and admission
evicts before a fetch rather than pruning after it.

### Costs left in deliberately

- **A refusal costs the asker a full stall timeout.** RNS sends nothing for a
  `None` response, and a request packet that is proven and then never answered
  has no failure callback, so only the plane's own sweep ends it (measured at
  120.1s and 127.5s in scenario files4). Silence is the point for a non-member;
  the price is paid by a member refused for a reason that would have passed,
  such as the concurrent-serve cap. Worth revisiting only with an answer that
  cannot become an oracle for a prober.
- **A member holding nothing costs the same timeout.** A node announces on the
  `files` aspect only while it holds something to serve, so a member with
  nothing has no path to dial and the dial ladder cannot tell a cold path from
  an absent destination. Ordering candidates by `Transport.has_path` would fix
  it; not done on one radio profile's evidence, and it costs a slow download
  rather than a failed one.
- **A relayed file message is not checked against its author's permissions**
  (see "3-bis" above). `SEND_MESSAGE` already had this gap and `SHARE_FILES`
  now shares it.

### Database growth, which the code cannot state

Three budgets bound the file tables: complete received files
(`FILE_STORE_MAX_BYTES`), unfinished downloads (`PARTIAL_STORE_MAX_BYTES`) and
this node's own uploads (`OWN_FILE_STORE_MAX_BYTES`, never auto-pruned because
the sender has to stay a holder). Worst case is about 530 MB of file rows plus
overhead. What the budgets do not bound is the file on disk:

- **Deleting rows never shrinks the database.** Freed pages go on the freelist
  and later writes reuse them, so the file stays at the largest size the budgets
  ever reached. Only `VACUUM` returns the space, and it rewrites the whole
  database, needs free disk equal to its size, and re-encrypts every page under
  SQLCipher. It is not run automatically. `auto_vacuum=INCREMENTAL` can only be
  set before a database has tables, so it would apply to new profiles only; a
  "compact database" action is the honest fix and is not built.
- **Setting or removing the PIN copies the whole database.** `encrypt_to` and
  `export_to_plaintext` build a second copy through `sqlcipher_export`, so both
  need free disk equal to the database, files included; `rekey` is in place and
  does not. This gap predates file sharing, which makes it larger.
- **Message images are still unbounded.** Up to 900 KB each, stored in
  `messages`, with no prune at all: only deleting a conversation removes them.
  File sharing arrives bounded where images are not. Putting images under the
  same LRU is the follow-up.

### Alternatives rejected, and why

- **Push the file, raising LXMF's delivery limit.** A push sends every byte to
  every member whether they want it or not, and the receiver cannot decline a
  resource the router already agreed to.
- **An inline tier for small files.** Files under a few hundred KB carried in
  the message, as images are, so they would ride hints and sync for free.
  Rejected: the size at which storing bytes you did not ask for stops being
  acceptable is not the sender's to decide. The manifest rides hints and sync
  anyway, so only the bytes wait for a request.
- **Propagation nodes for files.** Channel messages are never propagated, nodes
  cap a message at 256 KB, and a node holding channel files for members is
  exactly the storing-for-others role that has to stay weak.
- **"I hold this" announcements.** One control message to every member per
  download, to save the requester one failed handshake. Presence order plus
  remembering the last holder gets most of it for nothing.
- **Files on disk under `~/.trenchchat/`.** Faster for large files and outside
  both the SQLCipher lockbox and the one prune policy. Revisit only if the 5 MB
  ceiling grows past what a blob column is happy with.
- **Whole-file requests, resume by restarting.** An RNS response resource does
  not survive a link change, so every drop restarts a transfer that takes hours
  on LoRa.
- **Chunking with the file hash only, no chunk root.** Saves 32 bytes per
  message and lets one hostile member spoil a whole transfer with a single bad
  chunk discovered at the end, with no way to say which holder did it.
- **Per-file request paths**, nomad's `/file/<name>` shape. Needs a register and
  deregister on every store and prune; one path with the hash in the request
  data needs neither and is no less private, since paths are hashed anyway.
- **Existing tools.** `rncp` (in RNS) is the closest reference and the shape the
  file plane copies, but its allow list is one static per-process identity list,
  its address is a filesystem path, and a transfer is one whole-file resource
  with no resume. Nomad node file serving has the same three limits. LXMF's
  `FIELD_FILE_ATTACHMENTS` is a push. `RNS.Channel`/`Buffer` is a stream bound to
  one link, so offsets, verification and holder switching would all still have to
  be built on top. None offers membership-scoped authorisation, several sources,
  or resume across links.
- **Fetching from several holders at once.** The chunk scheme allows it and it
  is faster on a good mesh, but it doubles what one download can take from a
  shared link. Sequential first, measure before adding.
- **`rncp` fetch compatibility** is a live follow-up rather than a rejection:
  registering an `rncp.receive` destination that reads the path as a file hash
  and applies the same membership check would let a member pull a channel file
  with the stock CLI. Whole-file only, fetch only, and it needs the user's
  identity in a form `rncp -i` can load.
