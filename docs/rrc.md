# Public chat is RRC, and RRC has a centre

TrenchChat's public chat is [Reticulum Relay Chat](https://rrc.kc1awv.net/),
the protocol `rrcd` speaks, rather than anything this project defines. A
TrenchChat user and an `rrc-gui` user sit in the same room on the same hub,
and nothing TrenchChat-shaped travels on that wire.

This document is the reasoning that the code cannot state: what was traded
away to get there, what a hub can see, and which gaps are deliberate.

## The trade, stated plainly

RRC is hub-and-spoke. Clients open a Link to a hub, the hub routes to the
room's other members and then discards. That fails two of the seven checks
in `.claude/rules/reticulum-zen.md`:

- **Check 1, no center.** Without a reachable hub there is no public chat.
  Ask "which peer's absence breaks this?" and the answer is no longer "none".
- **Check 4, store and forward.** The specification is explicit that a hub
  buffers nothing. An absent peer misses the conversation permanently, and
  no amount of coming back later recovers it.

Both were accepted deliberately, in exchange for public chat that
interoperates with every other RRC client instead of only with TrenchChat.
What the design does to stay as close to the checks as the protocol allows:

- **Any node can be the hub.** `core/rrc_hub.py` is a complete hub, off by
  default and switched on from settings. `rrcd` is one implementation among
  peers rather than the service. A hub is chosen by the client and
  interchangeable, which is the same weakness `core/propagation.py` is
  deliberately built with.
- **Hubs are heard, not looked up.** Discovery is the `rrc.hub` announce
  (`network/announce.py::HubAnnounceHandler`). There is no directory to ask
  and therefore no second centre behind the first (check 5).
- **Nothing durable depends on a hub.** Invite-only channels, servers and
  direct messages are untouched and remain fully peer-to-peer. The centre is
  confined to chat that the protocol itself declares ephemeral.
- **A hub holds no authority.** No moderation, no operator commands, no ban
  list. Those exist as `rrcd` extensions and are deliberately not
  implemented: a hub that can silence people is a centre with power over
  them rather than a relay anyone can replace.

## What a hub can see

This is the part with no equivalent anywhere else in TrenchChat, and the
reason connecting is always an explicit act rather than something a bookmark
does on its own.

A client must identify on the Link, because RRC has no accounts and the Link
is the entire authentication. So the hub operator learns, for every client:

- the identity hash, cryptographically, not as a claim
- every room joined and parted, and when
- the full plaintext of every line, at the application layer
- timing and volume, and therefore who talks to whom

Reticulum still encrypts the Link, so this is the hub operator and nobody
else. But no TrenchChat channel exposes any of it: a channel's messages are
end-to-end between members, and its sync responder was already a member. A
hub is not a member of anything. It is a stranger who sees everything.

The client says so before the first connection to a hub, and never connects
without being asked to. That confirmation is the PUBLIC tab's one gate, and
`public_tab_test.dart` holds it: a hub is never dialled without it, an
`rrc://` link included.

## Deliberate non-fixes

These are gaps left open on purpose. Each is a property of RRC, not a defect
in this implementation, and closing any of them would mean leaving the
protocol and losing the interoperability that motivated the change.

- **No offline delivery.** A message to someone who is not connected is
  gone. There is no propagation node for RRC and no queue.
- **No history, and no backfill.** A client that joins a room late gets
  nothing that was said before it arrived, and a transcript is dropped when
  the session ends. Scenario `rrc3` pins this as behaviour, so nobody
  quietly adds a store to the hub and calls it an improvement.
- **No sync.** RRC rooms use none of the three mechanisms in
  `docs/offline-sync.md`, hold no `subscriptions` row, and never touch
  `messages`. That absence is what keeps them out of sync, presence beacons
  and avatar broadcast, exactly as it does for direct messages.
- **No reactions, files, images or voice.** All four are TrenchChat
  additions with no place in the RRC envelope. Adding them as private
  extensions would create a second dialect that no other client could read,
  which is the thing this change exists to avoid.
- **Nicknames are not identity.** `K_NICK` is advisory, unverified and may
  collide. It is a label over an authenticated hash, the same standing a
  display name has everywhere else in TrenchChat.
- **A hub can lie about a message it relays.** It sets `K_SRC` itself, so it
  can attribute a line to anyone connected, or drop one silently. There is
  no signature in the RRC envelope to prevent it. The bound is that the hub
  is chosen and replaceable, not that it is trustworthy.

## Rejected alternative: keep TrenchChat's own public channels alongside RRC

The obvious smaller change was to add RRC as a second public-chat system and
leave open-join channels where they were. It was rejected for three reasons:

1. Two public-chat systems is two things to explain, two places to look for
   a conversation, and two sets of bugs.
2. Open-join channels were the weak half of the channel design. They had no
   member document, so every rule had to be disabled for them, which is why
   `is_open_join()` had to be consulted in eleven core modules. Keeping them
   keeps that branching.
3. The subscriber-list protocol existed only to serve them. Removing them
   removed an entire signed wire protocol and its replay defence, along with
   the attack surface both carried.

## Where the code is

| Concern | File |
|---|---|
| Envelope, constants, validation | `trenchchat/core/rrc_wire.py` |
| Links, session handshake, hosting plane | `trenchchat/network/rrc_transport.py` |
| Client: hubs, rooms, transcripts | `trenchchat/core/rrc.py` |
| Hub: sessions, rooms, forwarding | `trenchchat/core/rrc_hub.py` |
| Hub discovery | `network/announce.py::HubAnnounceHandler` |
| Endpoints | `devtools/testenv/api.py`'s `/rrc/*` |
| Client surface | `flutter_ui/lib/screens/main_window/public_tab.dart` |

`rrc_wire.py` is the interop contract and is not TrenchChat's to change; its
numbers come from specification document 3 and rrcd's `EX1-RRCD.md`, and
`tests/test_rrc_wire.py` asserts them one by one so a drift is caught here
rather than by a user whose messages stopped arriving.

## Capabilities: only claim what is implemented, and spell it both ways

Both halves advertise `CAP_ACTION` and nothing else. A hub reads an
advertised capability as permission to use it, so claiming
`CAP_RESOURCE_ENVELOPE` or `CAP_DIRECT_NOTICE` without acting on them would
cause real dropped messages rather than a graceful degradation. They join
the advertised set when the code behind them exists, not before.

Specification document 3 leaves the shape of the capability field open and
its own wording describes string keys and arrays of strings, while rrcd
numbers them. A peer may therefore send either. `advertise_capabilities`
puts both spellings in one map and `capabilities_of` reads all four shapes
(map or list, numbered or named), so neither kind of peer sees us as
supporting nothing. Both ends must ignore what they do not recognise anyway,
which is the forward-compatibility rule the specification states outright.
It costs eight bytes on a `HELLO` and eight on a `WELCOME`, each sent once
per session, which is the cheapest place in the protocol to spend them.

## What has and has not been checked against the specification

`tests/test_rrc_wire.py` pins every number, and these were re-read from the
published specification rather than trusted from memory:

- **Confirmed.** Envelope keys 0 to 7, the 43-byte fixed overhead and the
  422-byte worst-case budget, every core message type (1, 2, 10 to 13, 20 to
  22, 30, 31, 40), the `HELLO` and `WELCOME` body keys 0 to 3, and the five
  hub-limit names, which are the only string keys in the protocol.
- **Not confirmed, because it lives in rrcd's own extension document rather
  than the published specification**: `K_DST = 8`, `T_RESOURCE_ENVELOPE = 50`
  and the capability numbers. Both of the first two sit inside ranges
  document 3 reserves for the core (envelope keys below 50, message types
  below 64), so either rrcd is squatting there deliberately or these numbers
  drifted on the way in. Matching rrcd is the whole point, so they stay where
  they are, but this is the place a real interop run should look first.

None of the three unconfirmed numbers is ever sent: this node advertises
neither capability behind them, never emits a `K_DST` notice or a resource
envelope, and refuses both inbound. A wrong number there costs us nothing
we currently do; it would matter the day either capability is implemented.
