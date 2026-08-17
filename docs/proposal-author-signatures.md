# Proposal — per-message author signatures

Status: **awaiting sign-off.** Four decisions in §5 need a call before
implementation starts; everything else here is settled by the existing code.

Closes finding H in `docs/security-audit-2026-08.md`, recorded as open gap 0b in
`docs/security-improvements.md`.

---

## 1. The gap

Live messages are authenticated. A direct chat message carries the author's own
LXMF signature, and `network/router.py` drops anything whose signature does not
validate before it reaches a callback.

History is not. In `SyncManager._handle_sync_response` the stored row's
`sender_hash`, `sender_name`, `content`, `timestamp`, `reply_to`,
`last_seen_id` and `image_data` all come out of the responder's msgpack
payload. The router authenticated the *responder*; nothing authenticates the
*author*, and no author signature is carried, so nothing can.

Three properties make this worth closing rather than documenting and moving on:

- **It contradicts what the UI claims.** Every message header renders
  `Alice [a3f1c2d4]`, and `security-improvements.md` states that the identity
  shown is "authenticated rather than merely claimed". On a synced row it is
  claimed. The strongest anti-spoofing affordance in the product is misleading
  exactly where forgery is possible.
- **Honest peers launder it.** There is no provenance column on `messages`, so
  an accepted forgery is indistinguishable from a directly received message.
  The victim then serves it to other peers, where the sender-tenure check
  passes because the claimed author really was a member. One forgery gains
  credibility from peers who never met the attacker.
- **On open-join channels the bar is subscription.** `has_any_tenure` is false
  there, so tenure constrains nothing and any identity can be named as author,
  including one that was never in the channel. On tenured channels a member can
  only impersonate a co-member who was enrolled at the claimed time.

It is an integrity and deniability break, not a confidentiality one: no
plaintext is disclosed, no permission is escalated, no membership changes.

## 2. Why not reuse the LXMF signature

LXMF signs the whole addressed message. Re-verifying one later needs the exact
signed bytes, which means storing `message.packed` per message — a second full
copy of every message including image payloads — and it does not exist at all
for messages we sent ourselves. Rejected.

An application-level signature over a canonical digest is small, storable, and
verifiable by any peer that can recall the author's identity.

## 3. Mechanism

### 3.1 Canonical digest

New helper in `core/protocol.py` (dependency-free, already the home for wire
concerns):

```python
AUTHOR_SIG_DOMAIN = b"trenchchat-author-v1"

def author_digest(channel_hash_hex: str, message_id: str, timestamp: float,
                  content: str, reply_to: str | None,
                  last_seen_id: str | None, image_data: bytes | None) -> bytes:
    """The bytes an author signs to bind a message to their identity."""
```

Built as a length-prefixed concatenation, hashed with SHA-256:

```
sha256(
    len-prefixed AUTHOR_SIG_DOMAIN ||
    len-prefixed channel_hash (16 raw bytes) ||
    len-prefixed message_id (utf-8) ||
    len-prefixed f"{timestamp:.6f}" (utf-8) ||
    len-prefixed reply_to or "" (utf-8) ||
    len-prefixed last_seen_id or "" (utf-8) ||
    len-prefixed sha256(image_data or b"") ||
    len-prefixed content (utf-8)
)
```

Three choices worth stating:

- **Length prefixes, not separators.** `content` is arbitrary user text and can
  contain any byte; a separator scheme lets one field's contents impersonate a
  field boundary.
- **A domain tag.** The same Ed25519 key already signs invite tokens, member
  list documents and subscriber lists. Without domain separation a signature
  over one structure could potentially be presented as another.
- **`f"{timestamp:.6f}"`, not the raw float.** Matches `_compute_message_id`'s
  existing convention and avoids depending on float encoding agreeing across
  peers and msgpack versions.

`image_data` enters as a digest so the signature covers the attachment without
signing megabytes.

**Deliberately not covered: `sender_name`.** A display name is self-asserted
and mutable — that is the known, accepted residue in
`security-improvements.md` §2, and the hash badge is what addresses it.
Signing it would freeze a name at send time and produce spurious verification
failures on rename. It stays unsigned and untrusted.

### 3.2 Protocol field

```
F_AUTHOR_SIG = 0x60   # bytes[64] — Ed25519 signature over author_digest()
```

`0x50–0x5F` is the sync-status range and an author signature is not sync
status, so this opens a new `0x60–0x6F` "message integrity" range. Per
`.claude/rules/protocol-constants.md` this needs an entry in the registry table
and in the field-layout docstring at the top of `core/messaging.py`.

### 3.3 Where it is produced, stored and checked

| Step | Location |
|---|---|
| Sign at send | `Messaging.send_message` — after `msg_id` is computed |
| Put on the wire | `Messaging._build_lxm`'s `fields` dict |
| **Store the author's own copy** | `Messaging.send_message`'s local `insert_message` at the end |
| Verify on direct receipt | `Messaging._on_lxmf_message` |
| Carry through sync | `SyncManager._row_to_dict` |
| Verify on sync receipt | `SyncManager._handle_sync_response` |

The third row is easy to miss and load-bearing: the author must persist its own
signature locally, or its own messages become unsignable the moment it is asked
to serve them, and the whole scheme fails for exactly the peer with the
strongest claim to authorship.

Signing and verification reuse `identity.sign` / `identity.validate`, the same
primitives behind subscriber lists (`core/subscription.py`'s `_sign`/`_verify`)
and invite tokens.

### 3.4 Storage

```sql
ALTER TABLE messages ADD COLUMN author_sig BLOB
```

Follows the established `_has_column` + `ALTER TABLE` migration pattern in
`Storage._migrate_permissions`. `insert_message` and `_row_to_dict` grow one
optional parameter each. A NULL signature means "unsigned", which is the
correct reading for every row that already exists.

Whether a separate persisted verification state is needed depends on decision
**D2** below. If signatures are re-checked on read, `author_sig` alone is
enough; if verification is recorded once at insert, a second column is needed.
Re-checking on read is cheaper to reason about and cannot go stale, at the cost
of an Ed25519 verify per rendered message.

### 3.5 Author identity lookup

Verification needs the *author's* identity, not the responder's:

```python
RNS.Identity.recall(delivery_hash_for_identity(bytes.fromhex(sender_hash)))
```

That `Destination.hash(x, "lxmf", "delivery")` + `recall` pair is currently
open-coded in six places (`sync.py` ×3, `presence.py`, `invite.py`,
`subscription.py`). This change should add one `recall_identity(hex)` helper
next to `delivery_hash_for_identity` in `network/router.py` and use it; folding
the existing six call sites into it is optional cleanup, better kept out of
this diff.

## 4. Verification states

| State | Meaning | Produced when |
|---|---|---|
| `verified` | Signature validates against the claimed author's identity | Signature present, author recallable, digest matches |
| `unsigned` | No signature accompanied the message | Legacy row, or a peer running an older build |
| `unknown_author` | Signature present, author's identity not recallable yet | Author has not announced within our horizon |
| `invalid` | Signature present and does not validate | Forgery, or corruption |

`invalid` is always a rejection. The other three are governed by D1–D3.

**Invariant: trust is never upgraded in transit.** A row stored `unsigned` is
re-served unsigned; a row stored with a signature is re-served with that same
signature. Nothing ever fabricates or re-signs a signature on someone else's
behalf. This is what stops the laundering described in §1 — a relayed forgery
cannot acquire verified status by passing through an honest peer.

## 5. Decisions needed

### D1 — What happens to unsigned messages

Every row in every existing database is unsigned, and so is everything an
un-upgraded peer sends. Rejecting unsigned rows discards all existing history
and breaks sync against older peers.

- **(a) Accept, mark unsigned, surface it in the UI.** Nothing breaks; the
  guarantee is opt-in and visible. The forgery described in §1 remains possible
  against a peer willing to accept unsigned history — which, during the
  transition, is every peer.
- **(b) Reject unsigned.** Closes the gap immediately and breaks
  interoperability and existing transcripts. Not viable as a first step.
- **(c) (a) now, with a configurable strict mode, and a later release that
  flips the default.** *Recommended.* Gives the guarantee to users who want it
  today and a credible path to (b).

### D2 — What happens when the author cannot be recalled

Normal for old history whose author has since gone quiet. Not evidence of
forgery.

- **(a) Hold and re-verify when the identity resolves.** There is precedent:
  `router.py`'s bounded quarantine plus `release_quarantined`, driven by
  `PeerAnnounceHandler`. Costs a second quarantine and its bounds. Messages sit
  invisible until an announce arrives, which for a departed author may be
  never.
- **(b) Store as `unknown_author`, re-verify lazily on read.** *Recommended.*
  The message is visible immediately and correctly labelled as unproven; a
  later announce upgrades it with no extra machinery. Pairs naturally with
  re-checking signatures on read (§3.4).

### D3 — Whether strictness varies by channel type

The exposure is much worse on open-join channels (§1), and those are also the
ones where requiring signatures is least disruptive, since there is no long
tenured history to invalidate.

- **(a) One global policy.** Simplest to explain and to test.
- **(b) Require signatures on open-join channels first, tenured channels
  later.** Targets the worst case first. Adds a second policy axis to every
  test matrix, and a channel's access mode can change under it.

Recommendation: **(a)**, with D1(c)'s strict mode as the single knob. Two
interacting policies is where correctness goes to die in this codebase's
sync paths.

### D4 — UI treatment

The guarantee is worthless if the client does not distinguish the states. This
is a Flutter change to the message header
(`flutter_ui/lib/screens/main_window/message_list.dart`)
plus a decision on the legacy Qt client.

- **(a) Badge only verified messages.** Quiet, but absence of a badge is easy
  to miss.
- **(b) Mark unverified messages.** Honest, but during the transition it marks
  essentially all history, which trains users to ignore it.
- **(c) Show the state only where it is actionable: mark a synced row whose
  author is named but unproven, leave verified and directly-received rows
  unadorned.** *Recommended.* Needs a wording pass with whoever owns the UI.

The Qt client is legacy per CLAUDE.md; leaving it unchanged is defensible, but
it then displays forged history with no indication, so it should at minimum not
claim verification it cannot substantiate.

## 6. Edge cases the current code forces

Two of these come from hardening already on this branch and are easy to get
wrong.

**Timestamp clamping conflicts with signing.** `Messaging._on_lxmf_message` now
substitutes local time when `wire_timestamp` rejects a peer's value. The
timestamp is inside the digest, so clamping a signed message guarantees the
stored row no longer matches its signature. Proposed rule: a **signed** message
with an implausible timestamp is **dropped** — the author signed that claim, so
it is not a clock quirk to paper over. Unsigned messages keep today's clamp.

**Stripping an image invalidates the signature.** Both ingest paths set
`image_data = None` when the payload is oversized or fails
`inbound_image_is_sane`. The image digest is inside the signed digest, so the
stored row would no longer verify. Proposed rule: when the image is stripped,
**clear `author_sig` as well** and store the row as `unsigned`. Downstream then
sees an honestly unsigned row rather than one that looks tampered with, and the
§4 invariant holds.

**`message_id` remains unverified, and that is fine.** It is
`sha256(content:sender:timestamp)` but is never recomputed on receipt, and an
attacker computes a self-consistent one anyway. Once `message_id` is inside the
signed digest the signature is what binds it; recomputing it adds nothing.

**`reply_to` and `last_seen_id` must be in the digest.** Leaving them out lets
a relay re-thread a genuine message — attaching real, signed words to a
different conversation.

**Sender-side re-encoding is already stable.** `prepare_image` runs before both
the local insert and the wire send, so the author's stored bytes and the bytes
it signs are identical. Inbound images are not re-encoded (only header-checked),
so a relayed image's digest still matches.

## 7. Test plan

Per `.claude/rules/test-coverage-for-new-features.md` and
`.claude/rules/permission-enforcement.md`.

`tests/test_protocol.py` (new):
- `author_digest` is stable against a committed golden vector — this is the
  compatibility contract; if it drifts, every peer disagrees silently.
- Digest changes when any covered field changes; does not change when
  `sender_name` changes.

`tests/test_messaging.py`:
- A sent message carries `F_AUTHOR_SIG` and stores it locally.
- A received signed message verifies and stores as `verified`.
- A signed message with an implausible timestamp is dropped (§6).
- An oversized or bomb image strips both the image and the signature (§6).

`tests/test_sync.py`:
- **The relay test.** A authors a message, B receives it directly, C syncs it
  from B, and C verifies against **A's** identity rather than B's. This is the
  whole point of the change.
- An `unsigned` row re-served by B arrives at C as `unsigned`, never `verified`.

`tests/test_adversarial.py`:
- A responder that rewrites `sender_hash` on a signed row is rejected.
- A responder that re-signs with its own key while claiming another author is
  rejected.
- A responder that strips the signature yields `unsigned`, not `verified`.
- A responder that alters `content`, `reply_to` or `image_data` under a
  genuine signature is rejected.
- Positive control alongside each, so a handler that rejected everything could
  not pass — the failure mode `security-improvements.md` records from the
  earlier admin-authorization work.

`tests/test_sync_restart.py`:
- Signature and state survive a restart.

Two-peer verification in `devtools/testenv/` before the client work, per
`.claude/rules/feature-development-workflow.md`: this is a protocol change, and
the pytest suite's `TestTransport` delivers instantly and in order, which is
exactly what hides identity-recall timing bugs.

## 8. Effort

| Area | Size |
|---|---|
| `protocol.py` digest + field constant | ~40 lines |
| `messaging.py` sign, store, verify | ~50 lines |
| `sync.py` carry and verify | ~50 lines |
| `storage.py` column, migration, plumbing | ~30 lines |
| `router.py` recall helper | ~10 lines |
| Tests | ~300 lines |
| Flutter state + header treatment (D4) | ~60 lines Dart + widget test |

Roughly a day of implementation. The schedule risk is not the code — it is
agreeing D1–D4, because each one is a promise to users about what a name next
to a message means.

Wire and storage cost: 64 bytes per message. At `MAX_RESPONSE_MESSAGES = 50`
that is ~3.2 KB per sync response against a 1 MB LXMF budget, and negligible
beside any image.

## 9. Out of scope

- **Tenure gap 0 remains.** A tenure-blind peer still applies no tenure
  filtering. Author signatures make impersonation infeasible, which shrinks
  what that gap is worth, but it needs the per-identity provenance work
  described in `security-improvements.md` §0.
- **Display-name spoofing remains** by design (§3.1).
- **No repudiation or deletion.** A signature proves authorship; it gives an
  author no way to retract a message they did send.
- **Reactions are unaffected.** They travel on `MT_REACTION` and are not
  carried by sync, so they have no relay path to forge. If reaction sync is
  ever added it needs the same treatment.
