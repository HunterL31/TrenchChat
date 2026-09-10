# Plan: a TCP/IP transport for TrenchChat

Status: a plan for work not yet started, kept here so the decisions it records
survive until the work lands. Per `.claude/rules/docs-worth-committing.md`, the
durable reasoning moves next to the code and this file is deleted when it does.

## The ask, and what it costs against the Zen

Build a second backend that keeps everything TrenchChat is (no server, every
peer an equal, end-to-end encrypted, identity-addressed, store-and-forward) but
runs over ordinary IP instead of Reticulum, so the features that make Discord
good (instant delivery, large media, long history, many-participant voice,
typing and read state) stop being priced against a 1.2 kbps radio. Tailscale
is the suggested way to make peers reachable without port forwarding or a
relay of ours.

Checked against `.claude/rules/reticulum-zen.md`:

- **Every byte costs (check 3)** is the one deliberately relaxed. The IP
  transport gets bigger budgets, not no budgets: every ceiling stays, chosen
  against a home uplink instead of LoRa, and it stays a per-transport policy,
  never a global one.
- **Code to the intent, not the medium (check 6)** is why this plan is a seam
  and a second implementation rather than a fork. Managers must not know which
  transport carries them; the transport owns its limits and its reachability.
  The IP transport itself must not branch on Tailscale versus LAN versus a
  public address either: its medium is "a reachable host and port", and how
  that comes about is the user's overlay of choice.
- **No center (check 1)**: Tailscale's coordination server distributes
  WireGuard keys and brokers NAT traversal, and its DERP relays carry
  encrypted packets when direct paths fail. That is a center for reachability,
  not for the application, and it never holds a TrenchChat key or message.
  The app must work identically on a LAN, over plain WireGuard, or with
  Headscale (self-hosted, same local API), so Tailscale is an optional
  integration and never a dependency.
- Checks 2, 4, 5 and 7 hold unchanged: sessions authenticate before any byte is
  read, offline is still the normal case, state is keyed on identity hashes
  with addresses as revocable hints, and nothing phones home.

What is lost on the IP transport, said plainly: LXMF interop (Sideband and
other LXMF clients as the other half of a direct message), Nomad Network
browsing and hosting, propagation nodes, mesh-wide public channel discovery,
and reach over LoRa, packet radio and serial. Those stay on the Reticulum
transport, which is not going anywhere.

## What the codebase already gives us

Findings from reading the code, which shape the design below:

- **The RNS API surface in use is narrow.** Across `trenchchat/`, non-logging
  uses of RNS reduce to a handful of call shapes: `Identity.recall`,
  `Destination.hash`, building an outbound `Destination`,
  `Transport.request_path` and `has_path`, announce handler registration,
  `LXMessage` construction with `DIRECT` and delivery/failed callbacks, and
  `Link` for the three stream planes. `RNS.log` accounts for roughly half of
  all references and works without a running Reticulum.
- **Identity, signing and naming do not need the transport.** `RNS.Identity`
  generates keys, signs, verifies, encrypts and hashes without
  `RNS.Reticulum` ever being constructed, and `RNS.Destination.hash` is a
  static function. Only `Identity.recall` touches the running instance.
  Verified in this repo's venv. So identity hashes, channel hashes
  (`core/naming.py`) and every signed document (member lists, subscriber
  lists, invite tokens, author signatures) are the same bytes on both
  transports. A profile can move between transports; nothing signed has to
  be reissued.
- **The protocol layer is already transport-neutral.** `core/protocol.py`,
  the envelope (`pack_fields`/`unpack_fields`), member-list documents, sync
  ranges (`core/sync_ranges.py`), permissions, authorship signatures, storage
  and all of `core/actions.py` make no transport call at all.
- **The stream planes already have a seam.** `VoiceTransportBase`,
  `FileTransportBase` and `NodeTransportBase` are abstract, with an RNS
  implementation and an in-process fake each (`tests/fake_voice.py`,
  `tests/fake_file_transport.py`, `tests/fake_node.py`). An IP implementation
  is a third subclass, and `VoiceManager`, `FileManager` and
  `NodeBrowserManager` need no change.
- **The message plane's seam is half built.** `network/router.py` is already
  the single choke point for inbound authentication, envelope unwrapping,
  control-message rate limiting and dispatch, and `tests/conftest.py`'s
  `TestTransport` shim is already a second delivery implementation. What is
  missing: `Router.send` takes a fully built `LXMF.LXMessage`, so nine
  managers (messaging, sync, invite, subscription, reaction, avatar, friends,
  presence, voice signalling) each repeat the same 25-line block
  (`Destination.hash`, `Identity.recall`, `request_path` on a miss,
  `Destination(OUT)`, `LXMessage`, `router.send`), and about a dozen inbound
  handlers resolve `message.source_hash` back to an identity hash the same
  way. Two managers lean on LXMF further: `messaging.py` is the only user of
  per-message delivery and failed callbacks, and `presence.py` polls LXMF
  send states directly to drain a goodbye before shutdown. `sync.py` keeps
  every peer under two keys (identity hash and delivery hash) because RNS
  has both; that duality is LXMF's and should never reach a manager again.
- **The identity model is RNS's, and that is fine.** `authorship.py` verifies
  a synced message with `RNS.Identity.validate` against a key cache that
  checks every key hashes back to the identity claiming it. An IP session
  handshake hands over exactly that key, so the cache fills from HELLO
  instead of from `Identity.recall`, and the verification code is untouched.
- **The Reticulum-only features are a contiguous block.** In
  `devtools/testenv/api.py`: interfaces, discovery, node config, suggested
  defaults, network map, bandwidth, link quality, propagation, Nomad
  browsing and hosting, and the harness's offline toggle. In the client: the
  IFACE, MAP and BROWSE tabs, four dialogs, the micron renderer, seven model
  files, and the Propagation Node settings tab. Five of the twenty-six
  WebSocket event types are Reticulum-specific (`propagation_node`,
  `net_status`, `network_map_changed`, `nomad_node`, `nomad_fetch`). The
  other twenty-one, and every channel, message, member, friend, DM, file,
  reaction, emoji, theme and voice endpoint, are transport-agnostic.
- **The client already models delivery neutrally**: `pending`, `delivered`,
  `failed`. Today `delivered` means "handed to LXMF", not acknowledged; the IP
  transport can make it mean acknowledged for free.
- **The test environment already runs over TCP**: testers dial a hub over
  `TCPClientInterface`, and the link shaper splits the stream on RNS's HDLC
  flag byte. A length-prefixed splitter makes the same shaper shape IP
  sessions.

## Design

### One app, two transports

```
Flutter client  ──HTTP/WS──  api.py  ──  actions.py  ──  core managers
                                                             │
                                                    Router (facade, neutral)
                                                    ┌────────┴────────┐
                                             LXMFTransport      IPTransport
                                             (today's code)     (new)
                                                    │                │
                                              RNS.Reticulum    TLS sessions,
                                              + LXMRouter      UDP voice
```

`trenchchat/network/base.py` defines the seam:

- `InboundMessage`: `source_hex` (already authenticated), `fields` (the
  unwrapped protocol dict, or LXMF's own fields for a foreign direct
  message), `content`, `timestamp`, `hash`, `trenchchat_protocol`. Handlers
  take this instead of `LXMF.LXMessage`; the `Identity.recall` dance every
  handler does today to turn `source_hash` into an identity hash disappears
  with it.
- `Router.send(dest_hex, fields, content="", *, on_delivered=None,
  on_failed=None) -> SendState` where `SendState` is `SENT`, `QUEUED` (the
  transport holds it until the peer is reachable) or `NO_PATH` (the caller
  decides: pending queue, missed-delivery hint, control retry). This replaces
  the `Destination.hash` / `Identity.recall` / `request_path` / `LXMessage`
  sequence at every send site. `Router.can_reach(dest_hex)` and
  `Router.request_path(dest_hex)` cover the two remaining reachability
  questions managers ask.
- Peer events: `add_peer_appeared_callback(cb(peer_hex))` replaces
  `PeerAnnounceHandler`; `add_identity_resolved_callback` replaces
  `PathResponseHandler`; channel, user, node and propagation announces become
  `add_channel_discovered_callback` and friends, each fired by whichever
  transport has an equivalent (the IP transport has none for nodes or
  propagation and simply never fires them).
- `Router.limits -> TransportLimits`: every ceiling that was chosen against a
  slow link, as one object the managers read instead of module constants
  (see "Limits" below).
- `Router.voice_transport()`, `file_transport()`, `node_transport()` hand out
  the plane implementations; `node_transport()` is `None` on IP.

Three rules make the seam hold:

- **Managers see identity hashes only.** The `lxmf.delivery` destination
  hash is `LXMFTransport`'s private alias; it maps in both directions at the
  edge and never hands a delivery hash upward. `sync.py`'s two-key peer
  bookkeeping and `presence.py`'s recall on every inbound message go away
  with it.
- **Send state is the transport's word, not LXMF's.** `presence.py`'s
  goodbye drain waits on a `Router.drain(timeout)` call instead of polling
  `LXMessage.state`; `messaging.py`'s delivery tracking hangs off the
  `on_delivered` and `on_failed` callbacks the seam already carries.
- **Announcing is a transport concern.** `channel.py` stops owning
  `RNS.Destination` objects and registering its own announce handler; it
  hands the transport a signed channel record and gets discovery callbacks
  back. `FirstContactAnnouncer` and the re-announce heartbeat become
  `LXMFTransport` internals.

`Router` keeps its name and constructor position so the nine managers that
take a `router` need no signature change. What is transport-neutral in it
today stays (envelope unwrap, control rate limit, dispatch, outbound
callbacks); what is LXMF's moves into `network/lxmf_transport.py` (the
`LXMRouter`, signature validation, the quarantine and its path-request
budget, announces and their handlers, propagation node mode,
`delivery_hash_for_identity`).

`Backend` builds the transport from `config.transport` (`"rns"` or `"ip"`),
recorded in the profile so a profile is never opened under the wrong one by
accident. `rns` stays a dependency of both for identity, signing and naming;
the IP transport never constructs `RNS.Reticulum`.

### The IP transport: `trenchchat/network/ip/`

**Reachability.** A node listens on one TCP port and one UDP port (same
number, configurable, one fixed default). How a peer reaches it is outside
the app: a tailnet, a LAN, a WireGuard tunnel, a forwarded port. The transport
only ever sees host and port pairs.

**Session.** One TLS 1.3 session per peer pair, from the stdlib `ssl` module
(nothing new to pin, audit or bundle). Each install mints a self-signed
certificate once; it proves nothing by itself. Identity is proven by the
first frame: `HELLO {pub64, ts, sig}` where `sig` is the identity's Ed25519
signature over `own_cert_fingerprint || peer_cert_fingerprint || ts`. The
receiver checks that `sha256(pub64)[:16]` is the hash it expected (or, on the
listening side, learns who called), that the signature verifies under
`pub64`, and that `ts` is within clock skew. Binding the signature to both
certificate fingerprints is what defeats a relay that terminates TLS on each
side with its own certificates. Both sides send HELLO, so every session is
mutually authenticated to an identity hash before any other frame is read;
there is no quarantine on this transport, because there is no such thing as
a message from an unknown sender.

The peer with the lexicographically smaller hash dials; the other dials after
a timeout, the tie-break `RNSVoiceTransport` already uses. Dialing runs a
backoff ladder per peer, the `link_client.py` shape, extended to a persistent
schedule capped at fifteen minutes so a peer that is off for a week costs a
few connection attempts an hour and nothing more. Socket keepalive detects a
dead NAT mapping; there is no application-level heartbeat. Idle sessions stay
up: an idle TLS session costs nothing on IP, and tearing it down would only
buy a handshake later.

The transport runs an asyncio loop on one background thread and fires
manager callbacks from a small worker pool, so the contract every manager
already has (callbacks arrive on background threads; the API layer marshals
them through `EventBus`) is unchanged, and a slow handler cannot stall the
loop that every session shares.

**Frames.** Length-prefixed msgpack, one frame type byte. `MSG` carries an
envelope `{src, dst, ts, content, fields, sig}` where `fields` is exactly the
dict `pack_fields` carries today and `sig` is the sender's Ed25519 signature
over the packed envelope. Signing every message when the session is already
authenticated is redundant for direct delivery and essential for everything
relayed: sync responses, missed-delivery service, and anything a future
mailbox holds. `ACK {hash}` gives `delivered` its honest meaning. `REQ`/`RESP`
carry the stream planes' request-response exchanges with ids, and a response
larger than one frame is sent as a chain so a chat message interleaves with
a file chunk instead of waiting behind it. That is the whole multiplexer.

**Discovery: how a hash becomes an address.** There is no broadcast in a
unicast network, so this is where the design has to be deliberate.

- A **signed endpoint record** `{identity, pub64, endpoints[], seq, ts, sig}`
  is the unit of discovery. At most eight endpoints, a newer `seq`
  supersedes, and anyone may relay one because nobody can forge one. A node
  builds its own from its listen port plus the addresses it can see on
  itself (a Tailscale address if present, LAN addresses, and a user-entered
  public host). Records go in a `peer_endpoints` table with the outcome of
  the last attempt per endpoint.
- **Out of band**: the identity card a user shares (text or QR) is their
  hash plus their current endpoint record. This is the bootstrap for a first
  friend or a first channel, the way a hash read off a screen is today.
- **Gossip on session**: when two peers connect they exchange the newest
  records they hold for identities they share a channel or friendship with,
  bounded per message. This is "peers announce; you listen and remember" in
  a network with no broadcast: the announce rides on a connection that was
  going to be made anyway.
- **Tailscale, opt in**: if a `tailscaled` local API is reachable, the node
  can list online tailnet peers and probe the TrenchChat port on ones it
  does not know, rate-limited. A probe that answers HELLO is remembered
  under the identity it proved; one that does not is remembered as not
  running TrenchChat. The tailnet is a trust boundary the user already drew,
  which is why probing inside it is acceptable and probing the internet
  never is. Headscale serves the same API.
- **Public channels** are discovered the same way: the owner's signed channel
  record gossips over sessions, so the discovered list is "channels my peers
  know". The mesh-wide radius is a Reticulum property and does not carry
  over.
- LAN mDNS is a later addition, not part of the first cut.

A session coming up is the "peer appeared" event, so everything
`PeerAnnounceHandler` drives today (sync request, pending flush, presence,
avatar and emoji exchange, control retry) runs unchanged. Presence on IP is a
fact (the session is up) rather than an inference from announces, so the
`PresenceBeacon` never has a reason to fire.

**Offline: store and forward without propagation nodes.** The three channel
mechanisms carry over untouched, with bigger budgets: pending retry keyed on
session availability, missed-delivery hints, and set reconciliation. A
direct message has no third party on either transport; on Reticulum it goes
to a propagation node, and on IP `Router.send` answers `NO_PATH` and
`Messaging` keeps the message in a durable `pending_direct` table, flushed
when the friend's session next comes up. That table lives in `Messaging`,
not the transport, so it also covers a Reticulum node with no propagation
node in earshot; it is one of the transport-independent improvements this
work throws off. It still means a DM between two peers who are never online
at the same time never arrives, recorded here as a deliberate non-fix for
the first cut. The fix that fits the Zen is a **personal always-on node**: a headless
TrenchChat on the user's own NAS or VPS inside their tailnet, holding mail
for its own identity only, which is also the road to history on every
device. It needs a delegation design (a second device acting for one
identity) and is a follow-up, not part of this plan.

**Stream planes.** `IPFileTransport` subclasses `FileTransportBase` over
`REQ`/`RESP` frames, so `FileManager`'s holder selection, chunk verification
and budgets run as they are; its `can_reach` answers from the session table
instead of the path table, and the "a holder must announce" rule that
`files.py` explains disappears, because a member with a session is
reachable whether or not it holds anything. There is no `IPNodeTransport`
in the first cut; Nomad browsing stays a Reticulum feature.

Voice: `IPVoiceTransport` keeps the signalling exactly as `docs/voice.md`
describes (LXMF-shaped control messages, now over `MSG` frames) and
carries frames as UDP datagrams, AEAD-encrypted
(ChaCha20-Poly1305 from `cryptography`) under a per-session key exchanged
over the TLS session at HELLO, with `voice_wire.py`'s packing inside. Where
UDP is blocked, frames fall back onto the TCP session, worse and working,
rather than the transport detecting why.

**Limits.** `TransportLimits` fields with the Reticulum value and the
proposed IP value. The IP numbers are the first proposal and are what the
`lora_fast`-style scenario run for the IP transport (a `home_wifi` and a
`mobile_lte` profile) has to confirm before they are called done.

| Limit | Reticulum today | IP proposal |
|---|---|---|
| Inline image (`image.MAX_IMAGE_BYTES`, `MAX_IMAGE_DIMENSION`) | 900 KB, 1200 px, sized under LXMF's 1 MB ceiling | 1 MB, 2048 px, as the inline preview; anything larger travels as a shared file with the preview inline and the bytes pulled on view, which keeps the `messages` table bounded (the security doc's open "images are unbounded" item) |
| Avatar / custom emoji | 16 KB / 64 KB | 256 KB / 256 KB |
| Shared file (`protocol.MAX_SHARED_FILE_BYTES`) | 5 MB, in the database | 200 MB, on disk under the profile, same three LRU budgets (256 MB / 256 MB / 20 MB become 4 GB / 4 GB / 500 MB) |
| Chunk size (`protocol.FILE_CHUNK_BYTES`) | 32 KB | unchanged: it is part of the manifest, so it must be the same on both transports |
| Chunks per request (`FILE_REQUEST_MAX_CHUNKS`) | 16 (512 KB) | 256 (8 MB) |
| Sync response (`sync.MAX_RESPONSE_MESSAGES`, `MAX_RESPONSE_BYTES`) | 50 messages, 1 MB | 500 messages, 8 MB, chained frames |
| Sync description (`sync_ranges.SYNC_DESCRIPTION_BUDGET_BYTES`) | 512 bytes ("4 s of airtime at 1 kbps") | 64 KB |
| Sync window (`SYNC_WINDOW_DAYS`) | 7 days | full history, ranges fingerprinted by year, then month |
| Voice (`voice_wire`, `config.voice.bitrate`) | 16 or 24 kbps Opus, 2 frames per packet, 400-byte packets under a 431-byte MDU, 8 participants | 32 to 64 kbps Opus, 1 frame per packet, 1200-byte packets under the 1280-byte WireGuard MTU, 25 participants (full mesh: about 1.5 Mbps up at the cap) |
| Control messages per sender (`router.CONTROL_RATE_BURST`) | 60 per minute | 600 per minute, which typing indicators need |
| Re-announce (`REANNOUNCE_INTERVAL_SECS`) | 3 hours | none: a session is the announce |
| Wire payload (`protocol.MAX_WIRE_PAYLOAD`) | 4 MB | 16 MB per frame |
| Sessions | n/a | 256 per node, 32 pending handshakes |

### Client

The client stays one codebase, gated by a new `GET /capabilities`:
`{transport, features: {nomad, propagation, network_map, interfaces,
typing, read_receipts, ...}, limits: {...}}`. On IP it hides the IFACE, MAP
and BROWSE tabs and the Propagation Node settings tab, replaces the link
quality meter's hop-count input with session RTT and loss, reads attachment
limits from `limits` instead of constants, and gains one tab:

- **PEERS**: my identity card (hash plus endpoint record, as text and QR),
  the address book (each known peer, its endpoints, last success, session
  state), add-peer-by-card, and the Tailscale panel (detected or not, which
  tailnet peers answered, a switch for probing).

The `mentions.dart` assumption that an identity hash is 32 hex characters
stays true, since hashes are the same on both transports.

### What this unlocks, in the order to build it

Transport-independent features, which land in core and benefit both
transports, are marked with an asterisk. The rest are cheap on IP and
forbidden on radio.

1. Acknowledged delivery: `delivered` means the peer acked. Free with `ACK`.
2. Typing indicators and read receipts: two ephemeral control types, never
   stored, never synced, read receipts opt-in per user (check 7).
3. Rich presence: online, idle, in voice, keyed on the session.
4. Full history on join and unbounded sync window; local search already works
   on `Storage`.
5. Large inline media, GIFs, files in the hundreds of MB, streamed from
   holders as today.
6. Link previews, generated on the sender's side and attached, so a
   receiver's client never fetches a URL it did not choose to open.
7. Message edit and delete, pins, threads*: signed control messages the
   author issues; every transport carries them.
8. Voice at Discord bitrates with many participants, per-peer quality from
   the UDP loss counters `frame_stats` already tracks.
9. Screen share and video: a codec question (VP8 or H.264 through `aiortc`
   or a bundled ffmpeg) and a bandwidth question, and the one item on this
   list that may justify an optional user-run SFU on the user's own tailnet.
   Not in this plan.

## Phases

Each phase ends with its check passing, on both the pytest suite and the
scenario suite where one applies. Estimates are focused engineer-weeks.

**Phase 0: spike and decisions (1 week).** Two processes on loopback
complete the TLS plus HELLO handshake with certificate-fingerprint binding
and reject a relaying third process; a `tailscaled` local API probe is
read from Python on Linux, macOS and Windows; `aioquic` is evaluated
against the same handshake as the alternative (see "Rejected alternatives");
PyInstaller bundles whatever is chosen. Output: the decisions in this file
confirmed or amended.

**Phase 1: the seam (3 to 4 weeks).** `network/base.py`, `InboundMessage`,
`Router.send` and the peer-event callbacks; `LXMFTransport` split out of
`Router`; every manager stops importing `LXMF` and stops calling `RNS`
beyond `RNS.log` and the identity and naming primitives; announce handlers
become transport callbacks; `TransportLimits` replaces the slow-link
constants; `TestTransport` becomes an implementation of the interface
instead of a monkeypatch of `router.send`. Check: the full suite and the
scenario matrix pass unchanged, with no test edited. This phase is worth
doing on its own: it deletes nine copies of the same send block and a dozen
inbound sender resolutions, the extraction `code-standards.md` already asks
for when a pattern repeats.
It is also the riskiest, because `invite.py` and `sync.py` are the two most
security-sensitive modules and the two most coupled; the adversarial suite
is the guard.

**Phase 2: IP message plane (3 weeks).** Listener, dialer with the backoff
schedule, session and HELLO, frames, `MSG` and `ACK`, the `peer_endpoints`
table, identity cards and manual add, `config.transport`, `Backend` under
`"ip"`. `peer_factory(transport="ip")` wires test peers over real loopback
sockets. Check: the whole manager suite passes under both transports, and
the adversarial tests for the session layer (relay rejection, unauthenticated
frames dropped, oversize frames, session floods, stale HELLO replay) pass.

**Phase 3: discovery and reconnect (2 weeks).** Signed endpoint records and
their gossip, the reconnect schedule, the Tailscale panel, public channel
records, presence from sessions, durable `pending_direct`. Check: a
three-process scenario where C learns B's address only from A, B moves to a
new address mid-run and is found again, and a DM sent while B is off arrives
when B returns.

**Phase 4: stream planes (3 weeks).** `IPFileTransport` with the raised file
limits and the on-disk store, `IPVoiceTransport` over UDP with the TCP
fallback, voice at the new bitrates and participant cap. Check: existing
file and voice test suites pass with the IP transports substituted; the
voice quality tests hold at `home_wifi` and `mobile_lte` profiles; a
`--repeat 5` scenario run on each.

**Phase 5: client and packaging (2 weeks).** `/capabilities`, the PEERS tab,
hiding the Reticulum tabs, limits from the API, acknowledged delivery,
`--transport` on `main_flutter.py`, firewall notes in the installers.
Check: `flutter analyze && flutter test`, and a person runs two installs
across a tailnet from the identity card alone.

**Phase 6: the Discord list (ongoing).** Items 2 through 8 above, each with
its own tests and, for anything periodic, a shaped scenario run.

Parity with today's feature set on IP is the sum of phases 0 to 5: about
fourteen to sixteen weeks. Everything after is the reason for doing it.

## Testing

- **The seam is proven by the existing suite.** 2,020 tests and 128 scenario
  rows already specify the managers; Phase 1 passes them without edits or it
  is wrong.
- **Both transports run the same manager tests.** `peer_factory` takes a
  transport parameter; the IP variant uses real loopback sockets, which puts
  real handshakes and real ordering under every existing test rather than a
  shim, at a few milliseconds a peer. The shim variant gets simpler too:
  today it monkeypatches `router.send` and still needs a live
  `RNS.Reticulum` for `Identity.recall` and `Destination` registration, with
  a teardown that hand-clears RNS's global tables. After Phase 1 it is a
  `FakeTransport` implementing the interface, and the fixture can drop the
  Reticulum instance.
- **New unit and adversarial tests** for the session layer, frames, endpoint
  records (a stale `seq` is refused, a record signed by the wrong key is
  refused, a relayed record verifies) and the discovery budget.
- **The scenario runner grows `--transport ip`**: no hub, testers dial each
  other, the shaper splits on the length prefix, and a UDP shaper is added
  for voice. Anything periodic or fan-out shaped is run at `home_wifi` and
  `mobile_lte` before it is called done, the IP transport's equivalent of
  the `lora_fast` rule.
- **Tailscale itself** is tested by a person, with `remote_host.sh`, since
  the runner cannot create a tailnet.

## Risks and open decisions

- **TLS with stdlib `ssl` versus QUIC (`aioquic`).** QUIC would collapse the
  TCP session and the UDP voice plane into one connection with streams,
  unreliable datagrams and connection migration (a peer whose address
  changes keeps its connection, which is check 5 at the transport layer).
  Against it: a new dependency with a C extension to pin and bundle on three
  platforms, and no public API for pinning a peer certificate, which the
  HELLO binding would have to work around. This plan recommends the stdlib
  route and asks Phase 0 to make the call on evidence.
- **The seam refactor's blast radius.** Around forty send sites and every
  inbound handler in the nine LXMF-using managers. Mitigation: no test is
  edited, the adversarial suite runs on every step, and the refactor lands
  as a series of small commits, one manager at a time.
- **A doubled test matrix.** Every manager test runs twice. Acceptable at the
  current suite's runtime; watch it.
- **Multi-transport nodes.** A node running both transports at once needs no
  relay role: it delivers to each recipient over whichever transport reaches
  them, and sync already lets any member serve a gap to any other. The
  catch is limits: an 8 MB image sent on IP cannot be served to a LoRa
  member, so a sync response has to degrade to the serving transport's
  limits. Deferred; the seam is designed so it can be added without a new
  design.
- **Windows and macOS listeners** prompt for firewall permission on first
  launch; the installers need a note, and the tray app needs a clear state
  for "listening" versus "blocked".
- **Two identities of one person on two transports** are two identities.
  Migration is a profile copy; there is no bridging of a Reticulum friendship
  to an IP one beyond sharing the same hash.

## Rejected alternatives

- **A fork or a new project.** Loses twenty-six thousand lines of manager
  logic, the three-layer permission enforcement and its adversarial suite,
  and the scenario matrix. The seam costs three to four weeks; a rewrite
  costs the rest of the year and reintroduces every bug those tests found.
- **WebRTC for everything (`aiortc`).** Brings ICE, DTLS, SRTP, Opus and
  data channels, and would let a browser connect as a peer. It also brings
  ffmpeg-sized dependencies, needs a signalling channel that this plan
  builds anyway, and puts SCTP under the message plane. Kept as the candidate
  for screen share and video only.
- **A DHT, libp2p or Matrix.** `py-libp2p` is not production grade; Matrix
  needs homeservers, which are a center; a DHT is unnecessary inside a
  tailnet and a metadata leak outside one.
- **Noise protocol libraries.** The Python implementations are unmaintained.
  TLS 1.3 from the stdlib is audited, bundled and already in every build.
- **Certificates minted from the identity key.** Ed25519 certificates work
  in TLS 1.3 but tie the design to OpenSSL's support for them in every
  bundled build. Signing both fingerprints in HELLO proves the same thing
  with any certificate.
- **LXMF's packed message format on the IP wire.** `unpack_from_bytes` needs
  a running Reticulum to recall the source, and carrying LXMF's semantics
  onto IP keeps the constraints this transport exists to shed.
- **A relay or SFU for voice.** A center. Full mesh with a participant cap
  instead; an optional SFU the user runs on their own tailnet, like the
  personal always-on node, is the only shape worth revisiting.
- **Fetching link previews on the receiver.** Every member's client would
  fetch every URL anyone posts, which is a tracker any poster can plant.
  Previews are made by the sender and travel as an attachment.
