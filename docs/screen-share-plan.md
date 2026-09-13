# Plan: screen share over direct sessions only

Status: a plan for work not yet started, kept here so the decisions it records
survive until the work lands. Per `.claude/rules/docs-worth-committing.md`, the
durable reasoning moves next to the code and this file is deleted when it does.

## Decisions

Four decisions fix the shape of this plan. Everything below follows from them.

1. **A screen share travels over a direct session and over nothing else.**
   Not the frames, not the signalling, not the fact that a share exists. The
   direct session (`network/ip/`, `docs/ip-transport-plan.md`) is the only
   carrier, and there is no mesh plane, no LXMF control message and no
   protocol field for it. A member this node holds no direct session with
   cannot watch, is never told there is anything to watch, and sees in the
   client why: the roster says the peer needs a direct connection, not that
   the share failed. This is stricter than voice, which degrades onto the
   mesh; a share at even one frame a second is tens of kilobytes, orders of
   magnitude above any radio link, and "degrading" it onto one would be a
   lie about the medium (Zen check 3).
2. **A share lives inside a voice session, as it does on Discord.** The
   sharer is in a channel's voice session, and only participants of that
   same session may watch. That gives the feature its audio for free (the
   call), its roster (the voice roster), and its authorisation scope (the
   channel's stored members and permissions), and it means a share ends
   when the call does.
3. **The sharer fans out to every viewer itself.** There is no mixer, no
   selective forwarding unit, no relay: each viewer holds its own session
   with the sharer and gets its own copy. The only peer whose absence ends a
   share is the sharer, which is the content's origin (Zen check 1). The
   cost is upload bandwidth, so viewers are capped and the stream adapts to
   the slowest one.
4. **Nothing is pushed that the viewer has not made room for.** Every hop
   is credit-based: the sharer sends a viewer the next update only when the
   viewer has acknowledged the last, and the backend sends the client the
   next only when the client has painted the last. A slow viewer gets a
   coalesced update covering everything that changed since it last looked,
   never a backlog; a static screen sends nothing at all. It is the VNC
   framebuffer-update model, and it is what keeps memory and bandwidth
   bounded without a bitrate estimator.

## What this costs against the Zen

Checked against `.claude/rules/reticulum-zen.md`:

- **No center (check 1)** holds. Fan-out from the sharer, no relay, no
  mixer; an optional user-run mixer the transport plan once floated is not
  built.
- **Every environment is hostile (check 2)** holds. A watcher is authorised
  against the stored member table and the channel's permissions, on the
  identity the session's HELLO proved. Every update is bounded before it is
  parsed and never decoded before its declared dimensions are checked. A
  new permission gets the three layers and an adversarial test.
- **Every byte costs (check 3)** is why decision 1 is what it is: the mesh
  carries zero bytes of this feature. On the direct path a static screen
  costs nothing, one update per viewer is in flight at a time, and every
  ceiling is a named constant.
- **Store and forward (check 4)** is the one check a live stream cannot
  satisfy, and the plan does not pretend otherwise: a share is ephemeral,
  nothing is queued for an absent viewer, nothing is stored, and "no
  session" is shown as a state rather than an error. What survives a lost
  session is the intent: a viewer whose session comes back is told again
  that the share is on.
- **Identity is not location (check 5)** holds: viewers are keyed on
  identity, and QUIC connection migration carries a watch across a peer
  that changes networks.
- **Code to the intent, not the medium (check 6)**: the plane branches on
  `Router.path_for`, never on an interface type, exactly as the voice plane
  does. Refusing the mesh is a property of the intent (a live screen), not
  of any medium.
- **The tool is not neutral (check 7)** matters most here. A screen is the
  most sensitive thing a user can transmit. The picker shows exactly what
  will be shared before anything is captured, a persistent indicator shows
  while sharing, stopping is one click, the share ends with the call, and
  no frame is ever written to disk. That a share exists is disclosed only
  to peers already holding a direct session with the sharer, and never to
  the mesh, so a transport node learns nothing. The sharer sees who is
  watching; viewers do not see each other.

## What the codebase already gives us

- **The carrier exists and is authenticated.** `IPTransport` holds one QUIC
  session per eligible peer, proven by HELLO, with reliable request streams
  (`send_request` / `set_request_handler`, REQ/RESP on a stream each) and
  1200-byte unreliable datagrams. `Router.path_for(peer)` says whether a
  peer is direct, and `add_path_changed_callback` says when that changes.
  The file plane (`network/ip/file_plane.py`) is a whole plane built on
  REQ/RESP with one operation name; the screen plane is the same shape.
- **Eligibility is already decided.** A direct session only ever exists
  between members of a shared invite-only channel or server
  (`core/upgrade.is_eligible`), so a share can only reach vetted peers
  without this feature checking anything about addresses.
- **Voice is the template.** `VoiceManager` has the session lifecycle, the
  roster, the `voice_chat` permission at three layers, the once-a-second
  re-authorisation sweep that cuts off a kicked participant, the
  `audio_status()` pattern for "the session is up but the device is not",
  and the client surfaces (`voice_panel.dart`, the roster rows, the badges).
  `core/audio/engine.py`'s `_Cadence` and its capture-thread shape carry
  over to a capture loop.
- **Bounds are a habit.** `frames.MAX_FRAME_BYTES`, `MAX_INFLIGHT_REQUESTS`
  per session, the file plane's concurrent-serve ceiling, and `core/image.py`'s
  declared-dimension check are the shapes every limit below copies.
- **The encoder needs no new dependency.** Pillow (JPEG, with libjpeg
  bundled in its wheels) and numpy (frame diffing) are already runtime
  dependencies. The client decodes JPEG natively on desktop and web.
- **Permissions have a compatibility rule already.** `SHARE_FILES` is read
  as granted wherever `send_message` is when a stored blob predates it;
  `permissions.has_permission` is the one place that rule lives, and a
  second one fits beside it.

What is missing, and why it is not a gap in this design: there is no screen
capture library in the tree and no video codec. Capture is one small
dependency (below); a video codec is deliberately not taken in the first cut.

## Design

### Where capture happens

In the backend process, on the machine the node runs on, exactly where the
microphone is captured today. The client is a view over HTTP and WebSocket;
it does not touch devices. A web client served from another host would share
the backend host's screen, which is what it does with the backend host's
microphone already, and is a dev-environment case rather than a user one.

Capture is `mss` (MIT, pure Python over ctypes, no compiled extension:
GDI on Windows, CoreGraphics on macOS, X11 on Linux). It captures monitors
and regions, not windows, and not the cursor; both are follow-ups below. It
does not work on Wayland, where the only route is the desktop portal over
PipeWire, a dependency several times the size of this feature. Wayland is a
deliberate non-fix in this plan: the client is told `unavailable` with the
reason, the way `audio_status()` names a missing library, rather than
capturing a black screen.

### The pipeline

```
sharer                                             viewer
mss capture ─ diff tiles ─ JPEG ─┐               ┌─ bounds check ─ tile store ─ WS ─ client
      (one capture thread,       │  direct        │   (ScreenShareManager,      (stage painter)
       one encode per tick)      ├─ session ──────┤    per share watched)
                                 │  REQ/RESP      │
   per viewer: dirty set,        │  (one op)      │   credit back to the sharer
   credit, last seq  ────────────┘               └── credit back to the backend
```

**Capture and encode** (`core/screen/capture.py`, `core/screen/encoder.py`).
One thread on a `_Cadence` at the configured frame rate grabs the chosen
monitor, scales it to at most the share size (`MAX_SHARE_HEIGHT = 1080`,
`720` for the "smoother" preset), splits it into `TILE_PX = 128` tiles and
compares each tile to the last frame with numpy. Changed tiles are JPEG
encoded once, at one quality, and the encoded bytes are kept as the current
encoded frame: a map from tile index to its latest bytes. When more than
`FULL_FRAME_THRESHOLD = 0.6` of the tiles changed in one tick, the tick
encodes one full-frame JPEG instead, because a whole frame as tiles costs
about twice what it costs as one image. There is one encoder for every
viewer, so the share runs at one size and one quality for all of them, the
smallest any current viewer asked for, which is the rule `VoiceManager`
already applies to its one Opus encoder.

**Fan-out with credit** (`core/screen/share.py`, the sharer side of
`ScreenShareManager`). Each viewer has a dirty set (tile indexes changed
since its last update), a credit count (`VIEWER_WINDOW = 2` updates may be in
flight), and the sequence it last acknowledged. After each capture tick,
every viewer with credit and a non-empty dirty set is sent one update
holding the current bytes of its dirty tiles (or the full frame when a full
frame is newer than its last update), and its dirty set is cleared. A viewer
that has not acknowledged inside `VIEWER_ACK_TIMEOUT_SECS = 10` is dropped.
Nothing is sent to a viewer with no credit; its dirty set keeps growing and
the next update it gets covers all of it. That is the whole adaptation
mechanism: a viewer on a slow uplink gets fewer, larger updates and never a
queue.

**Receiving** (`core/screen/watch.py`, the viewer side). An update from the
sharer is parsed with every bound stated, its images checked against their
declared dimensions (a tile must declare `TILE_PX` square, a full frame the
share size, both at or under the maxima), then stored into the viewer's own
encoded tile store, which is the same structure the sharer keeps: the latest
bytes per tile, or a full frame that clears them. Storing is what
acknowledges the update, which returns credit to the sharer. Nothing is
decoded in the backend.

**The client hop** (`WS /screen/watch/{peer}`). The client opens one binary
WebSocket per watched share, authenticated like `/ws`. The backend sends it
everything dirty in the tile store since the client's last acknowledgement,
as the same binary layout the session carried, and sends the next only after
the client answers with a one-byte ready text. If no client socket is open,
the backend does not watch at all: opening the socket is what subscribes to
the sharer, closing it is what unsubscribes, so a watch can never outlive
the window that wanted it. The backend forwards bytes; it never re-encodes.

**The client** decodes each image with `instantiateImageCodec` bounded to
its declared size, keeps one `ui.Image` per tile plus the latest full frame,
and paints them in a `CustomPainter` (`ScreenStagePainter`). A tile arriving
replaces its entry; a full frame clears the map. Then it sends ready.

### Signalling, on the session and nowhere else

There are no `MT_SCREEN_*` types and no `F_SCREEN_*` fields, and that absence
is the structural guarantee behind decision 1: nothing about a share can be
packed into an LXMF message because there is no key to pack it under, and
`ScreenShareManager` never calls `Router.send`, whose retry falls back onto
the mesh. Everything goes through `IPTransport.send_request`, which returns
`None` when there is no session and retries nothing.

One request operation, `SCREEN_OP = "screen"`, with the action in the
payload, the way the file plane names its operation:

| From | Action | Carries | Answer |
|---|---|---|---|
| sharer → participant | `started` | channel hash, share width and height, tile size, frame rate | ok |
| sharer → participant | `stopped` | channel hash | ok |
| viewer → sharer | `watch` | channel hash, the largest size it wants | ok, or refused with a reason (`not_sharing`, `not_in_voice`, `full`, `forbidden`) |
| viewer → sharer | `unwatch` | nothing | ok |
| sharer → viewer | `update` | one update (below) | ok: the acknowledgement that returns credit |

`started` goes to every peer that is both in the sharer's live voice roster
for that channel and on a direct path; a peer whose session comes up
mid-share (`path_changed`) is sent it then; a peer that joins the voice
session mid-share is sent it when its join lands. `stopped` goes to the same
set, and a lost session is its own `stopped` on the far side. A participant
records at most one share per peer and drops it on `stopped`, on the session
going away, on the peer leaving voice, or on the roster TTL. The client
shows a share it holds as a LIVE badge with a Watch action on that
participant's roster row.

`watch` and `update` are where enforcement lives; see below.

### Enforcement: a new permission, three layers

`SCREEN_SHARE = "screen_share"` joins `ALL_PERMISSIONS`, granted to admin and
member in every preset. Its compatibility rule, next to `SHARE_FILES`'s in
`permissions.has_permission` and nowhere else: a blob that mentions
`screen_share` in no role list grants it wherever `voice_chat` is granted; a
blob that mentions it anywhere is read as written. Open-join channels need no
row, the same reading `voice_chat` has.

| Layer | Sharing | Watching |
|---|---|---|
| Client gate | "Share screen" shown only with the permission, in voice, direct connections on, capture available | Watch shown only on a roster row that is `direct` and holds a `started` |
| Outbound guard | `actions.start_screen_share` re-checks in-voice, the permission, direct connections on | `actions.watch_screen_share` re-checks in-voice and that the share is held |
| Core inbound | the participant's `ScreenShareManager` drops a `started` from a peer without `screen_share` on that channel, or not in its voice roster; a session whose peer stops being eligible is closed by the upgrade sweep already | the sharer refuses `watch` from a peer not in its live voice roster or without `voice_chat`; refuses over `MAX_SCREEN_VIEWERS`; a viewer's `update` is refused by the viewer's core when it is not watching that peer |

The once-a-second sweep `VoiceManager` runs gains one job: a viewer that has
lost `voice_chat`, left voice, or left the channel is dropped from the fan-out
inside a second, and a sharer that loses `screen_share` stops. Adversarial
tests call each core method directly.

### Wire

`network/screen_wire.py`, fixed binary, dependency-free, one layout on both
hops so the backend forwards without re-encoding. Carried on the session as
the `d` bytes field of the `update` request, and on the client socket as one
binary message.

```
SC_UPDATE : u8 version | u32 seq | u16 width | u16 height | u8 tile_shift |
            u8 kind (0 tiles, 1 full) | i16 cursor_x | i16 cursor_y |
            u16 count | count × (u16 tx | u16 ty | u32 len | len bytes JPEG)
```

`tile_shift` names the tile edge as a power of two (7 for 128), a full frame
has one entry at (0, 0) covering the share size, and cursor is `-1, -1` when
unknown. Every reader states its limits: `MAX_UPDATE_BYTES = 4 MB` (under
`frames.MAX_FRAME_BYTES`), count at most the grid, coordinates inside it,
each `len` at most `MAX_TILE_BYTES = 256 KB`, and dimensions at or under
`MAX_SHARE_WIDTH × MAX_SHARE_HEIGHT`.

`started`, `stopped`, `watch` and `unwatch` are plain msgpack maps in the
request payload; every field is bounded by `frames.decode_body` and checked by
type before use.

### Limits

All direct-path constants in `core/screen/`, none in `TransportLimits`: there
is no mesh column to choose between.

| Limit | Value | Why |
|---|---|---|
| Share size | at most 1920 × 1080; "smoother" preset 1280 × 720 | one encoder for every viewer; the session's measured ceiling is the reason a 4K share is downscaled |
| Frame rate | 15 default, 1 to 30 configurable | capture and JPEG cost scale with it; measured in Phase 0 |
| Tile | 128 px, JPEG quality 75 | the spike measures 64 and 128 against header overhead |
| Full frame switch | over 60 % of tiles changed in one tick | a whole frame as tiles costs about twice one image |
| Viewers per share | `MAX_SCREEN_VIEWERS = 4` | the sharer uploads one copy each; a fifth is refused `full` |
| Shares per node | one outbound, one watched at a time | one capture thread, one stage; watching another replaces the first |
| Updates in flight per viewer | 2 | hides one round trip; the third waits and coalesces |
| Viewer acknowledgement timeout | 10 s | a viewer that stops acknowledging is dropped, not queued for |
| Update size | 4 MB | a 1080p full frame at quality 75 is a fraction of it; over it the session is not ended, the update is refused and the viewer re-asks with `watch` |
| Requests per viewer | 60 a minute of `watch`/`unwatch` | the same shape as `VOICE_PACKET_RATE_LIMIT`; updates are bounded by credit instead |
| Client socket | one per watched share, one update in flight | the client's ready text is the credit |

A share with a mesh-only participant in the call is unaffected: that
participant is simply not in the fan-out. Nothing here changes what the
voice session encodes at.

### Client

Additive, following the voice surfaces.

- **Voice panel** gains "Share screen" (gated as above) and, while sharing,
  a red "Sharing: Monitor 2 · 3 watching" line with Stop. The gate's reason
  when hidden is a tooltip: no permission, direct connections off, capture
  unavailable (with the backend's reason, Wayland included).
- **Source picker dialog**: the monitors `GET /screen/sources` lists, each
  with a small thumbnail the backend grabs once at request time, the preset
  ("Clearer" 1080p at 15, "Smoother" 720p at 30), and a one-line note of
  who can watch (participants on a direct connection).
- **Roster rows**: a LIVE badge on a participant whose share this node
  holds, with Watch; the `DirectBadge` already on the row explains why a
  participant without it has no Watch, with the tooltip "Screen share
  needs a direct connection".
- **Stage**: the watched share rendered above the message list in the
  channel column, with expand-to-window and fullscreen toggles, the
  sharer's name, and a Stop watching button. On a lost session the stage
  shows "Direct connection lost" and clears when the share is dropped; it
  never falls back to anything.
- **Settings**: under VOICE, the frame rate and preset defaults; under
  DIRECT CONNECTIONS, the diagnostics panel's per-session row gains the
  bytes a share moved over it, read from `session.stats()`.
- **Events** on `/ws`: `screen_share {peer, channel, state}`,
  `screen_session {state, reason}` (`started`, `stopped`, `error`),
  `screen_viewers {count}`.
- **API**: `GET /screen/sources`, `POST /screen/start {source, preset}`,
  `POST /screen/stop`, `GET /screen/status` (`sharing`, `watching`,
  `available`, `shares`), `WS /screen/watch/{peer}`.

Config under `"screen"` in `config.json`: `fps`, `preset`, `monitor`.

### What the share does not do, on purpose

- **No system or application audio.** Discord captures the shared app's
  sound; that is a per-platform loopback capture (WASAPI loopback on
  Windows, a virtual device on macOS, PulseAudio or PipeWire monitors on
  Linux) and a second mix into the voice pipeline. A follow-up, listed
  below, once the picture works.
- **No window capture and no cursor** in the first cut, for the same
  reason: each is per-platform API. The wire carries a cursor position from
  day one so adding it costs no format change.
- **No video codec.** Tiled JPEG is what Pillow and numpy give without a
  new dependency, and it is what makes a static desktop cost nothing. It is
  poor at full-motion video inside the shared screen, where every tile
  changes every tick: that case degrades to fewer frames, not to no share,
  and the spike measures how few. A VP8 or AV1 encoder (PyAV, tens of
  megabytes per platform, licensing to check per codec) would be a second
  `kind` in `SC_UPDATE` and a second encoder behind the same fan-out; the
  wire and the credit model do not change. Decided after Phase 0's numbers.
- **No recording, no thumbnails kept, no frame on disk.** The picker
  thumbnail is grabbed on request and sent to the client once.

## Phases

Each phase ends with its check passing on the pytest suite and, where one
applies, the scenario suite. Estimates are focused engineer-weeks.

**Phase 0: spike (1 week).** Under `devtools/spikes/screen/`, throwaway like
the upgrade spike. Capture with `mss` on Windows, macOS, X11 and a Wayland
session, recording what each returns; the encoder against three recorded
workloads at 1080p (a desktop with typing, a scrolling document, full-motion
video in a window): milliseconds per tick for grab, diff and encode, bytes
per second on the wire, at tile 64 and 128 and frame rates 15 and 30; the
Flutter decode cost per tile at those rates on desktop and web; and both
against the direct session's measured ceiling. Output: the tile size, the
presets, the full-frame threshold, a yes or no on a video codec, and this
file amended.

**Phase 1: core (3 weeks).** `network/screen_wire.py`; `core/screen/`
(capture over `mss` with a fake source for tests, encoder, the manager's
sharer and viewer halves); the `screen` request handler on `IPTransport`;
`SCREEN_SHARE` at all three layers with the compatibility rule; the sweep;
`actions.py` functions; `Backend` wiring; the endpoints and the watch
socket. Check: the tests under "Testing", and the full suite passing in both
modes (`--direct` and without).

**Phase 2: client (2 weeks).** The panel, the picker, the roster badge and
Watch, the stage painter, the watch socket with its ready credit, Settings.
Check: `flutter analyze && flutter test`, and a person on two machines on
one LAN shares a monitor from one and watches on the other.

**Phase 3: scenarios and tuning (1 week).** The `screen` scenario family
below at `--repeat 5`, the measured numbers into `docs/testenv-scenarios.md`,
and the presets adjusted from what a real session carries.

**Later, each its own change:** cursor position; window capture; system
audio; the video codec if Phase 0 says so; Wayland through the desktop
portal if the dependency is ever worth it; more than one watched share on
the stage.

Phases 0 to 3 are about seven weeks.

## Testing

- `tests/test_screen_wire.py`: the layout packs and unpacks, every bound
  refuses what it should, a full frame and a tile update round-trip.
- `tests/test_screen_encoder.py`: against a scripted framebuffer source
  (numpy arrays, the way `tests/fake_audio.py` scripts a microphone): only
  changed tiles are encoded, the full-frame switch fires at the threshold,
  scaling holds the maxima, a viewer with no credit accumulates a dirty set
  and gets one coalesced update.
- `tests/test_ip_screen_plane.py`: two peers over a real loopback session,
  like `test_ip_voice_plane.py`: `watch` then updates then `unwatch`; a
  viewer that stops acknowledging is dropped at the timeout; a fifth viewer
  is refused `full`; an oversize update is refused without ending the
  session; an update from a peer this node is not watching is refused.
- `tests/test_screen.py`: `ScreenShareManager` over `peer_factory`, direct
  and not: `started` reaches every direct participant and no other; a peer
  whose session comes up mid-share is told; a lost session drops the share
  on the viewer's side and never re-sends over the mesh; the share ends
  with the voice session. And the test that pins decision 1: with the fake
  LXMF transport recording every send, a whole share start to finish puts
  zero messages on it, and `trenchchat/core/protocol.py` contains no screen
  constant (a test that greps it, so the guarantee cannot erode quietly).
- `tests/test_adversarial.py::TestAdversarialScreen`: a `started` from a
  peer without `screen_share` is dropped; a `watch` from a peer not in voice
  or without `voice_chat` is refused; a viewer kicked mid-share is cut off
  by the sweep; a sharer whose `screen_share` is revoked stops within a
  second; each calling the core method directly.
- `tests/test_api_screen.py`: the endpoints and the watch socket against a
  stubbed manager, the token and origin checks on the socket, one update in
  flight until the ready text.
- Flutter: the stage painter composes two synthetic updates (a full frame,
  then two tiles) into the expected image; the panel's gate for each hidden
  reason; the roster badge and Watch.
- Scenarios, a new `screen` family in `scen_screen.py`, three testers:
  - `screen1`: A and B private and direct, both in voice, A shares; B's
    watch socket receives updates and B's backend holds tiles. Strict.
  - `screen2`: C shares only a public channel with A and is in the same
    voice session; C is never told of the share, holds no share, and C's
    tester log shows no screen traffic. Strict; this is decision 1 on a
    real network.
  - `screen3`: `POST /upgrade/close/{peer}` mid-share; B's stage loses the
    share, nothing crosses the mesh while the pair is down, and B is told
    `started` again when the session returns. Strict.
  - `screen4`: B is kicked mid-share and is out of the fan-out within a
    second. Strict.
  - `screen5`: two viewers, one acknowledging slowly through a test hook;
    the slow one receives fewer and larger updates, the fast one is
    unaffected, and the sharer's memory holds one encoded frame. Strict.
  The link shaper does not cover the direct path (recorded in
  `docs/ip-transport-plan.md`), so a shaped row measures nothing here;
  what the direct path affords is the spike's measurement, not the suite's.

## Risks and open decisions

- **CPU on the sharer.** Grab, diff and encode 1080p at 15 in Python is the
  budget question; numpy and libjpeg do the work but the loop is Python.
  Phase 0 measures it; if a tick overruns, the cadence drops frames rather
  than drifting, which is what `_Cadence` already does for audio.
- **Full-motion content.** Tiled JPEG at every-tile-changes is the worst
  case and may land at a few frames a second per viewer at 1080p. The
  preset exists for it, and the codec decision is deferred to numbers rather
  than guessed.
- **Wayland.** Most current Linux desktops. Reported honestly as
  unavailable; a portal route is the only fix and it is large.
- **Four viewers on a home uplink.** Four copies of a busy 1080p share may
  exceed it; the credit model turns that into fewer frames per viewer, not
  into loss, and the cap is a constant to revisit with measurements.
- **The client socket over a tunnel.** A web client served through
  `remote_host.sh` adds a hop the credit model covers but the picture will
  lag; a dev case, noted rather than designed for.
- **Should a share be allowed outside voice?** Decision 2 says no, for the
  roster, the audio and the scope it gives. Revisit only with a concrete
  use.

## Rejected alternatives

- **Any mesh path for any part of it.** A `started` flag on `voice_state`
  would cost one bit on the mesh and tell every transport node who is
  sharing a screen; frames over RNS Links would be a stream that cannot
  work on the medium the project exists for. Nothing of this feature
  touches Reticulum.
- **A relay or mixer.** A center; and the pairs that cannot punch stay on
  the mesh by the transport plan's decision, so they cannot watch either.
- **Capture in the Flutter client.** Per-platform plugins on desktop, a
  browser API on web that then has to ship raw frames to the backend, and
  a second capture stack next to the one the microphone already uses.
- **A long-lived stream with its own frame kinds.** REQ/RESP already has
  per-session in-flight limits, a worker-pool dispatch, a stream per
  exchange so a chat message never waits behind a frame, and a plane built
  on it to copy; a new frame kind would rebuild those.
- **Long-polling the sharer.** Holding a worker of the transport's pool of
  four for up to a second per viewer; credit-driven push from the sharer
  costs nothing while nothing changes and holds no worker.
- **Composing frames in the backend and re-encoding for the client.** A
  decode and an encode per frame per watched share on the viewer's node;
  forwarding the same bytes costs a bounds check.
- **A video codec first.** Tens of megabytes of dependency per platform,
  licensing per codec, and a client that cannot decode it on desktop
  without more; taken later, if Phase 0's numbers say tiles are not enough.
- **msgpack on the client hop.** The client has no msgpack decoder and does
  not need one for a fixed binary layout it already has to parse.
