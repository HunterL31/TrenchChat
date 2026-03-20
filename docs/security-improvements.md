# TrenchChat — Security Improvement Areas

This document describes three application-level security gaps identified during
a review of TrenchChat's use of the Reticulum stack and LXMF. None of these are
cryptographic vulnerabilities — Reticulum and LXMF handle all encryption,
signing, and key exchange correctly. These are hardening items at the
application layer.

---

## 1. Unsigned subscriber list updates (public channels)

### Issue

When a `subscriber_list` control message arrives (`subscription.py`,
`MT_SUBSCRIBER_LIST`), TrenchChat unpacks the msgpack payload and replaces the
local subscriber set without verifying that the message came from the channel
owner.

Any node that knows a channel hash can craft a forged subscriber list and send
it to a participant. The recipient would then use that list to determine who
receives future messages, meaning an attacker could:

- **Add themselves** to the list so they receive messages they shouldn't.
- **Remove legitimate subscribers** so they stop receiving messages (denial of
  service).
- **Replace the list entirely** to redirect all traffic.

LXMF already signs every message with the sender's Ed25519 key, so the sender
identity *is* authenticated at the transport layer. TrenchChat simply doesn't
check it.

### Options

**A. Verify sender is channel owner (recommended — minimal change)**

Before accepting a `subscriber_list` message, look up the channel in storage
and compare `message.source_hash` (resolved to an identity hash) against
`channel["creator_hash"]`. Reject the update if they don't match.

Pros: Simple, no protocol changes, uses data already available.
Cons: Relies on `creator_hash` being correct in local storage (which it is,
because it was set during channel creation or verified announce receipt).

**B. Sign the subscriber list document (mirrors invite member list)**

Adopt the same signed-document pattern used by the invite subsystem: the owner
signs the subscriber list with their Ed25519 key, and receivers validate the
signature before accepting.

Pros: Strongest guarantee; works even if the message is relayed through a
propagation node.
Cons: More protocol complexity, requires versioning and conflict resolution
(already solved in `invite.py` and could be reused).

**C. Move to a pull model**

Instead of the owner pushing subscriber lists, subscribers request the list
directly from the owner over a Reticulum link. The link itself authenticates
the owner.

Pros: No extra signing needed; link-level auth is sufficient.
Cons: Requires both parties to be online simultaneously; doesn't work well with
store-and-forward propagation.

---

## 2. Display name spoofing

### Issue

The `F_DISPLAY_NAME` field (0x02) in every LXMF message is set by the sender
and displayed in the UI without verification. Any node can claim any display
name. While the sender's Reticulum identity hash is cryptographically verified
by LXMF's signature, users see the display name prominently and may not notice
(or understand) the identity hash.

An attacker could impersonate another user by setting the same display name,
enabling social engineering attacks within a channel.

### Options

**A. Always show a truncated identity hash alongside the display name
(recommended — UI-only change)**

Append a short hash badge (e.g. `Alice [a3f1c2d4]`) to every message in the
channel view. Users learn to associate names with hashes and can spot
impersonation.

Pros: Zero protocol changes, easy to implement, educational for users.
Cons: Slightly noisier UI; users may still ignore the hash.

**B. Local contact book with verified names**

Let users assign trusted display names to identity hashes they've verified
out-of-band. Show a "verified" indicator when the sender's hash matches a
contact entry, and a warning when a display name matches a contact but the hash
doesn't.

Pros: Clear visual distinction between verified and unverified names.
Cons: Requires users to manually verify and add contacts; new UI surface.

**C. Channel-level name registry maintained by the owner**

The channel owner publishes an authoritative mapping of identity hash →
display name as part of the member list document (which is already signed).
Receivers use this mapping instead of the self-asserted name.

Pros: Names are authenticated by the owner's signature.
Cons: Only works for invite channels where the owner knows members; adds
protocol complexity; owner becomes a naming authority.

---

## 3. No rate limiting on control messages

### Issue

TrenchChat processes every inbound LXMF control message (subscribe,
unsubscribe, join request, member list update, subscriber list) without any
rate limiting. A malicious node could flood a target with control messages,
causing:

- Excessive CPU usage from signature verification and msgpack parsing.
- Excessive disk I/O from SQLite writes (member list replacements, subscriber
  list updates).
- Network amplification: a single `subscribe` triggers a `subscriber_list`
  broadcast to all subscribers.

### Options

**A. Per-sender rate limiting with a token bucket (recommended)**

Track the last N control messages received from each sender identity hash.
Drop messages that exceed a threshold (e.g. 10 control messages per minute per
sender). This can be implemented as a simple in-memory dict in the router's
delivery callback.

Pros: Simple, effective against single-source floods, no protocol changes.
Cons: Doesn't help against Sybil attacks (attacker using many identities);
requires tuning the threshold.

**B. Proof-of-work on control messages**

Require control messages to include a small proof-of-work (e.g. a nonce such
that `SHA-256(message_id + nonce)` has N leading zero bits). Receivers reject
messages without valid PoW.

Pros: Raises the cost of flooding for all attackers, including Sybils.
Cons: Protocol change; increases latency on legitimate control messages;
PoW difficulty is hard to calibrate across heterogeneous hardware (desktop vs.
embedded LoRa node).

**C. Only accept control messages from known identities**

For owned channels, only process subscribe/unsubscribe from identities that
have been seen in a valid announce. For invite channels, only process join
requests with a valid invite token (already implemented). Drop everything else.

Pros: Very effective for invite channels; leverages existing mechanisms.
Cons: Public channels are open by design, so completely unknown senders need
to be able to subscribe; this limits applicability.

**D. Decouple broadcast from subscribe (mitigate amplification)**

Instead of immediately broadcasting the full subscriber list every time a new
subscriber joins, batch updates on a timer (e.g. once per minute) or only send
the delta. This limits the amplification factor of a subscribe flood.

Pros: Reduces network amplification without rejecting legitimate subscribes.
Cons: Slightly delays subscriber list convergence.
