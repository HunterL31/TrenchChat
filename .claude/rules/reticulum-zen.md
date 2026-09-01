# The Zen of Reticulum

<https://reticulum.network/manual/zen.html>

This rule outranks every other rule in `.claude/rules/`. The others say how to write
code here; this one says what the code is allowed to be. When a feature request, a
convenience, or another rule pulls against it, it wins — say so and propose the
design that fits, rather than building the one that doesn't.

TrenchChat is not a chat app that happens to run on Reticulum. It is a Reticulum
application, and the properties below are the reason it exists. A feature that
quietly reintroduces a server, a tracker, or a fat pipe has removed the point of the
project even if every test passes.

## The seven checks

Apply all seven to a design *before* writing it. Each has a failure mode this
codebase has already had to design against, named so the check is checkable.

### 1. No center

There is no server, no coordinator, no privileged peer, no elected leader, no node
whose absence breaks the feature for everyone else. Every peer is equal and
potentially hostile.

- Any online member can answer a sync request; none is *the* one who answers.
- Ask of every new feature: **which peer's absence breaks this?** If the answer
  isn't "none", redesign.
- A propagation node is the one thing that holds data for others, and it is
  deliberately not an authority: it cannot read what it holds, it is chosen by
  the client and interchangeable, and held mail is *pulled, never pushed*
  (`core/propagation.py`). Anything new that stores for others must be that weak.

```python
# ❌ a center, wearing a hat
owner = channel["creator_hash"]        # only the owner can serve history
send_sync_request(owner)

# ✅ whoever is reachable
for peer in online_subscribers(channel_hash):
    send_sync_request(peer)
```

### 2. Every environment is hostile

Assume the link is tapped and the peer on the other end is an adversary. Trust the
math, never the claim.

- Derive what you can from the authenticated sender; accept from the wire only what
  cannot be derived. A DM's conversation address is recomputed from the sender
  (`naming.dm_hash_for`) precisely so a peer cannot inject into a conversation it is
  not half of.
- Validate against **stored** state, never against the incoming document's own
  claims (`.claude/rules/member-list-security.md`).
- Every permission gets all three enforcement layers plus an adversarial test
  (`.claude/rules/permission-enforcement.md`). The client gate is never the check.
- New inbound handling is bounded: size caps, rate limits, and a refusal path that
  logs. An unbounded inbound field is a hostile peer's memory allocator.

### 3. Every byte costs

5 bits per second is a valid speed. A byte is battery on a solar node, airtime
another peer cannot use, and a slice of shared spectrum. Taking only what you need
is stewardship, not micro-optimisation.

Ask of every send: **what is the minimum information that conveys this intent?**
The context the receiver already holds is free; resending it is not.

- No heartbeat, poll, or keepalive where evidence already exists. Presence beacons
  fire only after real silence, and with per-peer jitter so quiet peers don't
  transmit in lockstep (`core/presence.py`) — copy that shape, don't add a timer.
- Every payload that can grow gets a ceiling, and the ceiling is chosen against the
  slowest link, not the fastest (`core/image.py`, `MAX_THEME_BYTES`,
  `sync.MAX_RESPONSE_BYTES`).
- Send a state code, not a JSON object with metadata the peer already has.
- Anything periodic or fan-out shaped gets a scenario run at
  `--link-profile lora_fast` before it is called done. A constant that is
  comfortable at 100 Mbps can be badly wrong at 1 kbps.

### 4. Store and forward, never request/response

Connectivity is a spectrum, not a binary. Offline is the normal case, not an error
state, and "no answer yet" is never "failed".

- Never block waiting for the network. No `time.sleep` on a path request — fire and
  forget, queue for retry (`.claude/rules/reticulum-lxmf-guidelines.md`).
- A message to an unreachable peer is *queued*, and the UI says queued. A spinner
  that becomes an error after five seconds is a lie about the medium.
- Anything that must survive an absent peer needs a path back: pending retry,
  missed-delivery hints, sync, or a propagation node (`docs/offline-sync.md`).
  Decide which one at design time; "they'll just miss it" is a decision too, and
  gets recorded as one.

```
❌  Connect() -> Send() -> Wait() -> error on timeout
✅  Send() -> carry on -> handle it when it arrives
```

### 5. Identity is not location

An address is a hash of who, never a coordinate of where. A peer that moves from
fiber to LoRa mid-conversation is the same peer and nothing may break.

- Key state on identity hashes. Never on an interface, a path, a hop count, or
  anything that changes when a peer moves.
- No directory that has to be *asked*. Peers announce; you listen and remember
  (`network/announce.py`). Adding a lookup service adds a center — see check 1.
- Names are user-assigned labels over a verified hash, never the identity itself.
  A display name is self-asserted and unverified, and code must treat it that way.

### 6. Code to the intent, not to the medium

Transport agnosticism is what makes the app work unchanged on fiber, WiFi, and a
radio in a field.

- Never branch on interface type, and never assume a bandwidth. Write to the RNS
  and LXMF API and let the stack own the medium.
- Where a feature genuinely cannot work on a slow link, degrade it — don't detect
  the transport and special-case it.

### 7. The tool is not neutral

Reticulum's license forbids building systems that harm people, and the reasoning
applies to what gets built on top: a tool is intent, crystallized.

- No telemetry, no analytics, no phone-home, no crash reporting — nothing that
  reports a user's behaviour anywhere they did not choose to send it.
- Nothing that lets one peer track, deanonymize, or coerce another beyond what they
  deliberately share. Metadata a feature leaks to intermediaries counts.
- A known gap left open on purpose is written down as a deliberate non-fix
  (`docs/security-improvements.md`), not left for the next reader to rediscover.

## When a request pulls against this

Say which check it fails, in one or two sentences, and offer the design that
passes. Most conflicts dissolve — the centralized version is usually just the
first thing anyone thinks of, not the thing that was wanted. If the user
reaffirms after that, it's their call: build it, and note the trade.

The one thing not to do is build it quietly and leave the project's reason for
existing to erode a commit at a time.
