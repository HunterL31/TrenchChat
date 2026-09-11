# Plan: direct IP sessions brokered over Reticulum

Status: a plan for work not yet started, kept here so the decisions it records
survive until the work lands. Per `.claude/rules/docs-worth-committing.md`, the
durable reasoning moves next to the code and this file is deleted when it does.

## Decisions

Three decisions fix the shape of this plan. Everything below follows from them.

1. **Reticulum stays, and brokers the upgrade.** TrenchChat keeps running
   Reticulum and LXMF for everything it does today. When two peers can also
   reach each other over IP, they use Reticulum to trade the addresses and
   keys needed to open a direct, authenticated IP session between them, and
   the bulk traffic (large files, voice, history) moves onto it. The direct
   session is an upgrade for a pair, never a replacement: when it is down or
   never comes up, the pair is on today's behaviour, not on nothing.
2. **The direct path is visible.** A member list shows which members this
   node is talking to over a direct session, so a user can tell why one
   member's file arrives in seconds and another's in an hour.
3. **Only members of a shared invite-only channel or server are ever offered
   an upgrade.** An offer discloses this node's IP addresses to the peer;
   that is acceptable for someone an admin vetted and invited, and never for
   whoever happened to subscribe to a public channel.

An earlier draft of this plan proposed a standalone IP backend with its own
discovery (signed endpoint records, gossip, an optional Tailscale probe). It
was dropped because it needed a second discovery system, lost LXMF interop
and radio reach, and still had no answer for two peers behind NATs without a
broker. Reticulum is the broker this project already has.

## What this costs against the Zen

Checked against `.claude/rules/reticulum-zen.md`:

- **No center (check 1)** holds fully. The broker is Reticulum's own
  transport nodes: volunteer peers, interchangeable, none of them ours. No
  coordination server, no relay of ours, no DHT.
- **Every byte costs (check 3)** is relaxed on the direct path and nowhere
  else. Budgets are a property of the path a message travels, read by the
  managers from the transport at send time, and the Reticulum path keeps
  every constant it has today. Nothing periodic is added to the mesh: an
  upgrade is offered once per sighting of an eligible peer, under backoff,
  and the offer is one small control message.
- **Code to the intent, not the medium (check 6)** is why the managers do not
  know which path a message took. `Router` picks per peer; a manager asks
  the transport what it may send, not which medium it is on.
- **The tool is not neutral (check 7)** is why decision 3 exists, and why
  path state is local knowledge only: this node knows which of its own
  sessions are direct and never tells a third member who is directly
  connected to whom. Candidates travel inside an LXMF message, encrypted
  end to end, so a transport node relaying the offer never sees an address.
- Checks 2, 4 and 5 hold unchanged: a session authenticates before any byte
  is read, offline delivery stays with sync and propagation nodes exactly as
  it is, and a session is keyed on the peer's identity with addresses as
  hints that expire.

Nothing is lost. LXMF interop, Nomad Network, propagation nodes, public
channel discovery and radio reach all stay as they are, because the
Reticulum path is still every peer's first path.

## What the codebase already gives us

Findings from reading the code, which shape the design below:

- **The RNS API surface in use is narrow.** Across `trenchchat/`, non-logging
  uses of RNS reduce to a handful of call shapes: `Identity.recall`,
  `Destination.hash`, building an outbound `Destination`,
  `Transport.request_path` and `has_path`, announce handler registration,
  `LXMessage` construction with `DIRECT` and delivery/failed callbacks, and
  `Link` for the three stream planes.
- **Identity, signing and naming do not need a second implementation.** The
  direct session authenticates with the same `RNS.Identity` keys, so the
  peer on the other end of a direct session is the same identity hash the
  member list, the author signatures and the permissions tables already
  use. `authorship.py`'s key cache, which checks every key hashes back to
  the identity claiming it, fills from the session handshake as readily as
  from `Identity.recall`.
- **The protocol layer is transport-neutral.** `core/protocol.py`, the
  envelope, member-list documents, sync ranges, permissions, authorship
  signatures, storage and all of `core/actions.py` make no transport call.
  A message is the same bytes on either path, which is what lets a message
  sent over a direct session be served later over Reticulum by any member.
- **The stream planes already have a seam.** `VoiceTransportBase`,
  `FileTransportBase` and `NodeTransportBase` are abstract, with an RNS
  implementation and an in-process fake each. A direct-path implementation
  is a third subclass; `VoiceManager` and `FileManager` need only to pick
  one per peer.
- **The message plane's seam is half built.** `network/router.py` is the
  single choke point for inbound authentication, envelope unwrapping,
  control-message rate limiting and dispatch, and `tests/conftest.py`'s
  `TestTransport` is already a second delivery implementation. What is
  missing: `Router.send` takes a fully built `LXMF.LXMessage`, so nine
  managers (messaging, sync, invite, subscription, reaction, avatar, friends,
  presence, voice signalling) each repeat the same 25-line block
  (`Destination.hash`, `Identity.recall`, `request_path` on a miss,
  `Destination(OUT)`, `LXMessage`, `router.send`), and about a dozen inbound
  handlers resolve `message.source_hash` back to an identity hash the same
  way. `messaging.py` is the only user of per-message delivery callbacks;
  `presence.py` polls LXMF send states to drain a goodbye; `sync.py` keeps
  every peer under two keys (identity hash and delivery hash) because RNS
  has both.
- **Eligibility is one existing query.** `Storage.get_members` and
  `Storage.is_member` already answer "is this peer a member of this
  invite-only channel or server"; servers are always invite-only. The gate
  in decision 3 is a lookup over those tables, and the same re-check loop
  `VoiceManager` runs every second to cut off a kicked participant is the
  shape for tearing down a session whose peer stops being eligible.
- **Unknown control types are ignored.** Every manager's inbound handler
  reads `F_MSG_TYPE` and returns on a type it does not own, so a node that
  predates this feature drops an offer silently and the newer node falls
  back to Reticulum after its timeout. No version negotiation is needed.
- **The client already models delivery neutrally** (`pending`, `delivered`,
  `failed`), and member rows already carry per-member state the roster
  renders. Today `delivered` means "handed to LXMF"; on a direct session it
  can mean acknowledged.

## Design

### One node, two paths

```
Flutter client ──HTTP/WS── api.py ── actions.py ── core managers
                                                        │
                                            Router (facade, picks per peer)
                                            ┌───────────┴───────────┐
                                     LXMFTransport            IPTransport
                                     always on                 upgrade, per peer
                                            │                        │
                                     RNS.Reticulum         QUIC session over the
                                     + LXMRouter           punched UDP path
                                            └──── UpgradeManager ────┘
                                          offers over LXMF, opens IP sessions
```

`trenchchat/network/base.py` defines the seam:

- `InboundMessage`: `source_hex` (already authenticated), `fields` (the
  unwrapped protocol dict, or LXMF's own fields for a foreign direct
  message), `content`, `timestamp`, `hash`, `trenchchat_protocol`, and
  `path` (`"reticulum"` or `"direct"`). Handlers take this instead of
  `LXMF.LXMessage`.
- `Router.send(dest_hex, fields, content="", *, on_delivered=None,
  on_failed=None) -> SendState` where `SendState` is `SENT`, `QUEUED` or
  `NO_PATH`. `Router` sends over the direct session when one is up for that
  peer and over LXMF otherwise; a send that fails on a direct session before
  its `ACK` is retried over LXMF, and the receiver's existing message-id
  dedupe absorbs the rare duplicate. `Router.can_reach` and
  `Router.request_path` cover the two reachability questions managers ask.
- `Router.limits_for(dest_hex) -> TransportLimits`: the budgets for the path
  a message to that peer will take (see "Limits").
- Peer events: `add_peer_appeared_callback` replaces `PeerAnnounceHandler`
  and fires for an announce, an inbound message, or a direct session coming
  up; `add_path_changed_callback(cb(peer_hex, path))` is new and drives the
  member-list indicator.
- `Router.voice_transport_for(peer_hex)` and `file_transport_for(peer_hex)`
  hand `VoiceManager` and `FileManager` the plane for that peer's path.

Three rules make the seam hold:

- **Managers see identity hashes only.** The `lxmf.delivery` destination
  hash is `LXMFTransport`'s private alias, mapped at the edge and never
  handed upward. `sync.py`'s two-key bookkeeping and `presence.py`'s recall
  on every inbound message go away with it.
- **Send state is the transport's word, not LXMF's.** `presence.py`'s goodbye
  drain waits on `Router.drain(timeout)` instead of polling
  `LXMessage.state`; `messaging.py`'s delivery tracking hangs off
  `on_delivered` and `on_failed`.
- **Announcing stays a transport concern.** `channel.py` hands
  `LXMFTransport` a signed channel record and gets discovery callbacks back;
  `FirstContactAnnouncer` and the re-announce heartbeat become
  `LXMFTransport` internals. The direct path never announces anything.

`Router` keeps its name and constructor position so the nine managers that
take a `router` need no signature change. What is transport-neutral in it
stays (envelope unwrap, control rate limit, dispatch, outbound callbacks);
what is LXMF's moves into `network/lxmf_transport.py` (the `LXMRouter`,
signature validation, the quarantine and its path-request budget, announces
and their handlers, propagation node mode, `delivery_hash_for_identity`).

### The upgrade: `core/upgrade.py` and `network/ip/`

The code name is `upgrade` because `core/direct.py` already means direct
messages; the user-facing word is "direct".

**Eligibility, enforced at three layers** (`.claude/rules/permission-enforcement.md`):

- A peer is eligible when it is a current member of at least one invite-only
  channel or server this node is a member of, by the stored members table.
  Open-join channels never qualify, whatever their subscriber list says.
  Accepted friends who share no such channel do not qualify in the first
  cut; whether friendship should also qualify is an open decision recorded
  below.
- Client gate: a "Direct connections" switch in Settings, on by default,
  which stops the node listening and offering.
- Outbound guard: `actions.offer_upgrade` and `UpgradeManager.consider`
  refuse an ineligible peer before anything is sent.
- Core enforcement: `UpgradeManager` drops an inbound offer or answer from an
  ineligible peer, and `IPTransport` closes an inbound session whose `HELLO`
  proves an ineligible identity, before reading any other frame. A
  once-a-second re-check tears down a session whose peer has stopped being
  eligible (kicked, left, or this node left the last shared channel), the
  same sweep `VoiceManager` runs. Adversarial tests call these directly.

**The handshake.** Alice and Bob share an invite-only channel; Alice's hash
is the smaller.

1. Alice sees Bob is online (an announce, or any inbound message from him),
   has no direct session with him, and her backoff for him has expired.
2. She gathers candidates: each local interface address with her listen
   port (a Tailscale or WireGuard interface shows up here on its own, which
   is all the overlay support this design needs), a router-mapped port if
   UPnP-IGD, NAT-PMP or PCP gave her one, and the public address a peer last
   observed her at. At most eight.
3. She sends `MT_UPGRADE_OFFER` to Bob over LXMF: candidates, a 16-byte
   nonce, her session certificate (DER, a few hundred bytes), and the time
   she will start punching. It is authenticated like every control message
   and encrypted to Bob like every LXMF message.
4. Bob checks eligibility, answers `MT_UPGRADE_ANSWER` with his candidates,
   the nonce and his certificate, and starts sending small probe datagrams
   (the nonce) to each of Alice's candidates at once: the outbound probes
   open his NAT mappings.
5. Alice, on the answer, probes each of Bob's candidates. The first
   candidate pair with a probe seen both ways wins; Bob notes the address
   Alice's probes arrived from and reports it in the next exchange as her
   observed address.
6. Over the winning pair, Alice opens a QUIC connection with Bob's
   certificate as the only trust root and hers presented, then both send
   `HELLO {pub64, ts, sig}` with `sig` over both certificate fingerprints
   and `ts`. The certificates already arrived over an authenticated message;
   the HELLO binds them to the identity keys anyway, so a session that
   arrives on a mapped port with no offer behind it is held to the same
   proof.
7. `Router` marks Bob `direct`, fires `path_changed`, and routes to him over
   the session from then on. The session stays up while both are online;
   an idle QUIC connection costs a keepalive every few tens of seconds on
   the direct path and nothing on the mesh.

If no probe pair succeeds within a few seconds, the attempt fails and
Alice's backoff for Bob doubles, from thirty seconds to a day, reset when
either side's candidate set changes. The larger hash initiates only if it
has seen the smaller one online for ten seconds without an offer, which
covers a peer running an older build, the same fallback the voice plane
uses for one-way reachability.

**Session.** QUIC via `aioquic`, confirmed by the Phase 0 spike: one
connection per peer pair over the punched UDP path, reliable streams for
messages, acks and file chunks, unreliable datagrams for voice, one socket for
everything, and connection migration so a peer that moves from Wi-Fi to LTE
keeps its session. Pinning is `QuicConfiguration.cadata` set to the peer's own
certificate, which makes it the connection's sole trust root, with
`server_name` left unset so no hostname is checked; `cadata` takes PEM, so the
DER in `F_UPGRADE_CERT` is re-encoded on the way in.

`aioquic` will not request or expose a client certificate through public API,
so the listener never sees one and step 6's "hers presented" happens inside
the HELLO rather than in the TLS handshake. The listener sends a fresh
sixteen-byte nonce first and both signatures cover
`own_cert_fingerprint || peer_cert_fingerprint || nonce || ts`. Because the
connecting side pinned the listener's certificate, the nonce never leaves the
true pair, so a signature over it proves the identity is live on this
connection and cannot be replayed onto another. The certificate a connecting
node asserts in its HELLO is not proven by anything at the TLS layer: it is a
claim, useful only as the pin for a later connection in the other direction,
and it is stored as a claim. The fallback, TCP with TLS 1.3 from the stdlib
plus AEAD datagrams for voice, is not taken; the measurements behind that are
in `devtools/spikes/upgrade/README.md`.

Frames are the same on either session type: `MSG` carries an envelope
`{src, dst, ts, content, fields, sig}` where `fields` is exactly the dict
`pack_fields` carries today and `sig` is the sender's Ed25519 signature over
the packed envelope, so a message received directly is verifiable when any
member serves it later over sync. `ACK {hash}` gives `delivered` its honest
meaning. `REQ`/`RESP` carry the file plane's exchanges on their own streams,
so a chat message never waits behind a chunk.

The transport runs an asyncio loop on one background thread and fires
manager callbacks from a small worker pool, so the contract every manager
already has (callbacks arrive on background threads; the API layer marshals
them through `EventBus`) is unchanged.

**What the direct path does not do.** It never carries a message the
Reticulum path could not have carried: message-level limits are the same
on both, because a message must be servable by any member over any path.
What the direct path changes is how much can move per exchange and how
fast, which is the domain of the planes and of sync. Large media therefore
always travels as a shared file, with a preview inline and the bytes pulled
from a holder; on a direct path the pull takes seconds, on the mesh it takes
as long as it takes, and a member who never asks pays nothing.

**Stream planes.** `IPFileTransport` subclasses `FileTransportBase` over
`REQ`/`RESP` streams and `FileManager` prefers a holder it has a direct
session with. `IPVoiceTransport` keeps the signalling exactly as
`docs/voice.md` describes and carries frames as QUIC datagrams, with
`voice_wire.py`'s packing inside and bitrate chosen per pair by path; a
session mixes direct and Reticulum pairs, and the roster shows which is
which. Nomad browsing stays on RNS links.

**Offline delivery is unchanged.** Pending retry, missed-delivery hints, set
reconciliation and propagation nodes all work exactly as today; a direct
session is only ever a faster way to do what they do.

### Wire additions

Per `.claude/rules/protocol-constants.md`, the next unused range:

| Key | Constant | Carries |
|---|---|---|
| `0xA0` | `F_UPGRADE_CANDIDATES` | list of `(host, port, kind)`, `kind` one of `lan`, `mapped`, `observed`, at most 8 |
| `0xA1` | `F_UPGRADE_NONCE` | 16 bytes |
| `0xA2` | `F_UPGRADE_CERT` | the session certificate, DER, at most 2 KB |
| `0xA3` | `F_UPGRADE_PUNCH_AT` | unix timestamp |
| `0xA4` | `F_UPGRADE_OBSERVED` | the address this node last saw the peer's probes arrive from |

Message types `MT_UPGRADE_OFFER = "upgrade_offer"` and
`MT_UPGRADE_ANSWER = "upgrade_answer"`, owned by `core/upgrade.py`. Both are
control messages under the router's existing per-sender rate limit, and
`UpgradeManager` bounds them further: one outstanding offer per peer, a
nonce that is single-use, and a `punch_at` no further than a minute out.

### Limits

`TransportLimits` fields, by path. The Reticulum column is today's value and
does not change. The direct column is the first proposal, to be confirmed by
scenario runs at the `home_wifi` and `mobile_lte` profiles before it is
called done.

| Limit | Reticulum path | Direct path |
|---|---|---|
| Inline message payload (`image.MAX_IMAGE_BYTES`, preview size) | 900 KB | unchanged; a message must be servable on either path |
| Large media | as a shared file, pulled | as a shared file, pulled; same manifest, same message |
| Shared file (`protocol.MAX_SHARED_FILE_BYTES`) | 5 MB | 200 MB on both paths, since bytes are pulled and never pushed; file bytes move from the database to disk under the profile, the three LRU budgets grow (256 MB / 256 MB / 20 MB to 4 GB / 4 GB / 500 MB), and a mesh-only member pulls at its own pace or not at all |
| Chunk size (`protocol.FILE_CHUNK_BYTES`) | 32 KB | unchanged: part of the manifest |
| Chunks per request (`FILE_REQUEST_MAX_CHUNKS`) | 16 (512 KB) | 256 (8 MB) |
| Sync response (`sync.MAX_RESPONSE_MESSAGES`, `MAX_RESPONSE_BYTES`) | 50 messages, 1 MB | 500 messages, 8 MB |
| Sync description (`sync_ranges.SYNC_DESCRIPTION_BUDGET_BYTES`) | 512 bytes | 64 KB |
| Sync window (`SYNC_WINDOW_DAYS`) | 7 days | full history, fingerprinted by year, then month |
| Voice (`voice_wire`, `config.voice.bitrate`) | 16 or 24 kbps Opus, 2 frames per packet, 400-byte packets under the 431-byte MDU | 32 to 64 kbps Opus, 1 frame per packet, 1200-byte datagrams; the participant ceiling stays 8 until Phase 6 measures a mixed session, since one mesh-path participant in a large session is exactly the case check 3 is about |
| Ephemeral control (typing, read receipts, presence detail) | never sent | sent |
| Control messages per sender (`router.CONTROL_RATE_BURST`) | 60 per minute | 600 per minute |
| Sessions | n/a | 128 per node, 16 pending handshakes, 32 candidate probes in flight |

### Client

Additive only; nothing existing moves or hides.

- `GET /channels/{h}/members` rows gain `path`: `direct`, `reticulum` or
  `offline`. A `path_changed` WebSocket event `{peer, path, since}` updates
  it live. The member list renders a small "direct" badge on `direct` rows,
  with the tooltip "Connected directly over IP; files and voice with this
  member take the fast path". The voice roster shows the same badge per
  participant.
- `GET /upgrade/sessions` lists this node's direct sessions for a Settings
  diagnostics panel: peer, since, which candidate kind won (`lan`,
  `mapped`, `observed`), round trip, bytes each way, and the last failure
  reason per eligible peer with no session. This is where a user learns
  that a pair is stuck behind symmetric NAT.
- Settings gains the "Direct connections" switch, the listen port, and a
  "Try now" per peer for the diagnostics panel. Config keys under
  `"upgrade"` in `config.json`: `enabled`, `listen_port`.
- `delivered` on a message sent over a direct session means acknowledged.

Path state is what this node knows about its own sessions. It is never sent
to another member and never inferred about pairs this node is not part of.

### What this unlocks, in the order to build it

Items marked with an asterisk are transport-independent and land in core for
both paths. The rest are sent only over a direct session, which is how a
feature that check 3 forbids on radio is allowed on IP.

1. Acknowledged delivery, free with `ACK`.
2. Typing indicators and read receipts: two ephemeral control types, never
   stored, never synced, read receipts opt-in per user.
3. Rich presence: online, idle, in voice, keyed on the session.
4. Full history on join from any directly connected member.
5. Large media as pulled files with an inline preview*, which also bounds the
   `messages` table growth the security doc records as open.
6. Link previews, made by the sender and attached*, so a receiver's client
   never fetches a URL it did not choose to open.
7. Message edit and delete, pins, threads*: signed control messages the
   author issues.
8. Voice at Discord bitrates between direct pairs, per-peer quality from the
   loss counters `frame_stats` already tracks, and the participant ceiling
   revisited with measurements.
9. Screen share and video: a codec question and a bandwidth question, and
   the one item that may justify an optional mixer a user runs on their own
   machine. Not in this plan.

## Phases

Each phase ends with its check passing on the pytest suite and, where one
applies, the scenario suite. Estimates are focused engineer-weeks.

**Phase 0: spike and decisions (1 week).** Two processes complete the QUIC
plus HELLO handshake with certificate pinning through the public API and
reject a relaying third process; the same handshake bundles under
PyInstaller on Linux, macOS and Windows; a UDP punch succeeds between two
Linux network namespaces behind separate NATs and fails as expected behind
symmetric NAT; UPnP-IGD and NAT-PMP mapping is read from Python against a
home router. Output: the QUIC decision confirmed or the TCP fallback chosen,
and this file amended.

**Phase 0 results.** QUIC via `aioquic` 1.3.0 is confirmed, and the spike that
proves it is `devtools/spikes/upgrade/`. Pinning works through the public
`cadata`, a relaying third process is refused at the TLS handshake before any
application byte, and that relay can neither replay a captured HELLO nor forge
one. The one gap is that `aioquic` cannot request or expose a client
certificate through public API, so the connecting node's certificate rides
inside its HELLO against a nonce the listener sends first, which proves the
identity live on the connection and leaves the certificate itself an asserted
claim. On loopback the handshake took about 16 ms and 50 MB moved at 9.8 to 14.5
MB/s for about 3.2 CPU seconds, against 700 to 1040 MB/s for stdlib TCP with
TLS 1.3: fifty times slower, still far above any home uplink, and the reason the
fallback stays designed but unbuilt. The namespace harness punched in 200 ms
through port-restricted cone NATs and failed as intended through symmetric
ones, five runs of five, and turned up one thing Phase 3 needs: an unsolicited
probe reaching a NAT first can poison the port its own mapping wanted, which
makes the observed-address exchange a recovery path rather than a nicety. Two
checks did not happen here and stay open: UPnP-IGD and NAT-PMP are written and
unit-tested but have never seen a real router, and the PyInstaller bundle is
proven on Linux only.

**Phase 1: the seam (3 to 4 weeks).** `network/base.py`, `InboundMessage`,
`Router.send` and the peer-event callbacks; `LXMFTransport` split out of
`Router`; every manager stops importing `LXMF` and stops calling `RNS`
beyond `RNS.log` and the identity and naming primitives; announce handlers
become transport callbacks; `TransportLimits` replaces the slow-link
constants; `TestTransport` becomes an implementation of the interface
instead of a monkeypatch of `router.send`. Check: the full suite and the
scenario matrix pass unchanged, with no test edited. This phase stands on
its own: it deletes nine copies of the same send block and a dozen inbound
sender resolutions. It is also the riskiest, because `invite.py` and
`sync.py` are the two most security-sensitive modules and the two most
coupled; the adversarial suite is the guard, and it lands one manager per
commit.

**Phase 2: the direct session plane (3 weeks).** `IPTransport`: listener,
QUIC session, HELLO, frames, `MSG` and `ACK`, per-peer path selection and
LXMF fallback in `Router`, `path_changed`, `TransportLimits` per path.
`peer_factory(direct=True)` wires test peers with sessions opened straight
over loopback, no punch, so the whole manager suite runs over direct
sessions too. Check: the manager suite passes with and without direct
sessions, and the session adversarial tests pass (relay rejected,
unauthenticated frames dropped, oversize frames, session floods, stale
HELLO replay, an inbound session from an ineligible identity closed).

**Phase 3: the upgrade handshake (3 weeks).** `UpgradeManager`: eligibility
at all three layers, candidate gathering, port mapping, offer and answer
messages, the probe exchange, the backoff schedule, observed-address
learning, and the once-a-second eligibility sweep. Check: adversarial tests
(an offer from a non-member, from a public-channel co-subscriber, with too
many candidates, with a reused nonce, with a `punch_at` an hour out; a
kicked member's session torn down within a second); scenarios where two
testers upgrade over real RNS signalling on loopback, a third tester who
shares only a public channel is never offered one, and a session dropped
mid-conversation loses no message; the namespace NAT harness from Phase 0
run against the real handshake.

**Phase 4: planes over the direct path (3 weeks).** `IPFileTransport` with
per-path chunk limits and the on-disk file store; `IPVoiceTransport` over
datagrams with per-pair bitrate; sync limits by path. Check: the file and
voice suites pass with the direct transports substituted; the voice quality
tests hold at `home_wifi` and `mobile_lte`; a mixed voice session (two
direct pairs, one mesh pair) holds; `--repeat 5` on each scenario.

**Phase 5: client (1 to 2 weeks).** The member and voice roster badges,
`path_changed`, the diagnostics panel, the Settings switch and port, the
firewall note in the installers. Check: `flutter analyze && flutter test`,
and a person runs two installs on different home networks and sees the
badge appear from an invite alone.

**Phase 6: the list (ongoing).** Items 2 through 8 above, each with its own
tests and, for anything periodic, a shaped scenario run.

Phases 0 to 5 are about fourteen to sixteen weeks. Everything after is the
reason for doing it.

## Testing

- **The seam is proven by the existing suite.** 2,125 tests and 128 scenario
  rows already specify the managers; Phase 1 passes them without edits or it
  is wrong.
- **Every manager test runs on both paths.** `peer_factory` takes a `direct`
  flag; the direct variant opens real loopback sessions under every
  existing test, which puts real handshakes and real ordering under the
  managers at a few milliseconds a peer. The shim variant gets simpler too:
  after Phase 1 it is a `FakeTransport` implementing the interface, and the
  fixture no longer needs a live `RNS.Reticulum` or its hand-cleared global
  tables.
- **Adversarial tests** for eligibility at every layer, the session layer,
  and the offer's bounds, in `tests/test_adversarial.py`.
- **A NAT harness** under `devtools/testenv/` using Linux network namespaces
  and nftables masquerading, so the punch is tested against real address
  translation, including the symmetric case that must fail cleanly. Linux
  only; documented as such.
- **A `upgrade` scenario family** in the runner: sessions come up over real
  RNS signalling, fall back when a listener is stopped, tear down on kick,
  and carry a conversation across a drop. The shaper grows a UDP mode so the
  direct path can run at `home_wifi` and `mobile_lte`, the direct path's
  equivalent of the `lora_fast` rule.

## Risks and open decisions

- **Should accepted friends qualify?** A mutual friendship is a stronger tie
  than shared membership, and direct messages with large attachments would
  benefit. Left out of the first cut so the gate is one rule; extending it
  is a one-line change to `UpgradeManager.eligible` plus its tests.
- **Punch success rate.** Cone NATs punch; symmetric NAT and most CGNAT do
  not, and both sides symmetric never will. Those pairs stay on Reticulum,
  which is recorded as a deliberate non-fix: the alternative is a relay, and
  a relay of ours is a center. The diagnostics panel says which case a pair
  is in rather than leaving it mysterious.
- **`aioquic` as a dependency.** Settled: `aioquic==1.3.0`, BSD-3-Clause,
  every wheel `cp310-abi3`, so one wheel per platform covers CPython 3.10
  through 3.13 on win_amd64, macOS x86_64 and arm64, and manylinux x86_64 and
  aarch64. Its compiled extension links OpenSSL statically and bundles under
  PyInstaller on Linux with no hook and no hidden import, for about 6.8 MiB on
  top of what the release already ships; the macOS and Windows one-file builds
  are still unbuilt and are the residual risk. It pulls `pyopenssl` and
  `service-identity`, both of which float ahead of the pinned `cryptography`,
  so those three versions now move together. The cost that remains is speed:
  50 MB moved at 9.8 to 14.5 MB/s on loopback for one saturated core, against
  700 to 1040 MB/s for stdlib TCP with TLS 1.3, which is worth re-measuring on
  a laptop in Phase 4.
- **The seam refactor's blast radius.** Nine send sites, a dozen inbound
  handlers, the two most sensitive modules. Mitigation: no test edited, the
  adversarial suite on every commit, one manager per commit.
- **Ordering across paths.** A message sent over Reticulum and the next over
  a session that just came up can arrive out of order. The codebase already
  makes no ordering promise between two LXMF sends, and message ids plus
  `last_seen_id` threading already cope; a test pins it.
- **Per-path limits in sync.** A response served over a direct session
  carries messages that must also be servable over the mesh. Because
  message-level limits are the same on both paths, only the batch size
  differs; the design depends on keeping it that way.
- **Windows and macOS** prompt for firewall permission on the first listen;
  the installers need a note, and the diagnostics panel needs a clear
  "blocked" state so a user does not read a firewall as symmetric NAT.

## Rejected alternatives

- **A standalone IP backend** (the earlier draft). Needed its own discovery
  system, lost LXMF interop and radio reach, and had no broker for two
  NATed peers without a server or a DHT.
- **Tailscale as an integration.** Its coordination server is a center for
  reachability, and a dependency on it is a dependency on a company. A
  tailnet still helps without any integration: its interface address is
  gathered as an ordinary candidate, and the punch over it always succeeds.
- **A public DHT for rendezvous** (BitTorrent Mainline, BEP 44). No
  infrastructure of ours, but publishing where an identity is under its key
  makes "where is this person" a global lookup, and the bootstrap nodes are
  a soft center. Reticulum already reaches the peer without either.
- **WebRTC for everything (`aiortc`).** Brings ICE, DTLS, SRTP and Opus, and
  ffmpeg-sized dependencies, and still needs the signalling this plan
  builds. Kept as the candidate for screen share and video only.
- **A relay or mixer of ours for pairs that cannot punch.** A center. Those
  pairs stay on the mesh.
- **Upgrading with public channel co-subscribers.** Anyone can subscribe to
  a public channel, so an offer to a co-subscriber hands an address to
  anyone who asks.
- **Telling members who is directly connected to whom.** Costs bytes on the
  mesh and leaks which members share a network. Each node shows its own
  sessions and nothing more.
- **LXMF's packed message format on the direct wire.** `unpack_from_bytes`
  needs `Identity.recall`, and it carries LXMF's size ceilings onto the path
  that exists to raise them.
- **Fetching link previews on the receiver.** Every member's client would
  fetch every URL anyone posts, a tracker any poster can plant. Previews are
  made by the sender and travel as an attachment.
