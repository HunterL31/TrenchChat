# TrenchChat

A decentralized, encrypted group chat application built on the [Reticulum Network Stack](https://reticulum.network/) and [LXMF](https://github.com/markqvist/LXMF). TrenchChat works across any transport Reticulum supports (LoRa, packet radio, TCP/IP, serial links, and more) without a central server.

## Features

- **Serverless**: every client is a peer; no accounts, no servers, no phone numbers
- **End-to-end encrypted**: all messages are encrypted by Reticulum using X25519 + AES-256
- **Public and invite-only channels**: open channels anyone can join; invite-only channels with cryptographically-signed member lists
- **Offline sync**: messages sent while you were offline are delivered when you reconnect; see [Offline Sync](docs/offline-sync.md)
- **Propagation node support**: optionally designate a node as a store-and-forward relay
- **Terminal-styled Flutter client**: runs as a desktop app or in the browser

## Requirements

- Python 3.10+
- Dependencies listed in `requirements.txt`

```
rns
lxmf
msgpack
```

## Setup

**Linux / macOS**

```bash
./setup.sh
```

One command, end to end: virtual environment and all Python dependencies,
the system libraries voice chat needs (libopus + PortAudio, offered via your
package manager), a Flutter SDK download if you don't have one, the client
build, and launch. Re-run it any time, each step skips what's already done.

On SteamOS it detects whether the root filesystem is unlocked: unlocked
decks get the normal pacman offer; read-only decks are pointed at the
Distrobox route (`distrobox create --name trench --image ubuntu:24.04`,
`distrobox enter trench`, re-run `./setup.sh` inside) or at
`sudo steamos-readonly disable`.

**Windows**

```bat
setup.bat
```

Creates the virtual environment and installs Python dependencies. The
client additionally needs a one-time build (requires the
[Flutter SDK](https://docs.flutter.dev/get-started/install)):

```bat
cd flutter_ui && flutter build web        # or: flutter build windows
```

## Running

```bash
# Linux / macOS
source .venv/bin/activate
python main_flutter.py

# Windows
.venv\Scripts\activate
python main_flutter.py
```

One command starts the backend and the client: the native desktop binary when one is built,
otherwise the web client in your default browser. `--port` moves the local port, `--browser`
forces the browser, `--no-ui` runs the backend headless.

Closing the window does not close TrenchChat. It leaves a tray icon and keeps the node on
the mesh, so announces, discovery and sync carry on and messages sent while you were away
are still there; open a window again or quit from the tray. Launching it again while it sits
there reopens that window rather than starting a second node.

The tray needs a menu to quit from, so TrenchChat uses one only where that exists: Windows
and macOS always, Linux only with an AppIndicator or GTK status icon (PyGObject, so from
source rather than the packaged build, and not on GNOME, which stopped drawing status
icons in 3.26). X11's own tray takes a click and shows no menu, so it is treated as no tray
at all. Where there is none, closing the window quits as it always did, and `--no-tray` asks
for that everywhere.

## Development

`devtools/testenv/` runs two fully independent, real TrenchChat backends
("Tester A" and "Tester B") as separate processes over a real Reticulum
link, driven from a two-pane web UI, no second physical machine or manual
key exchange needed to test multi-peer behavior (invites, channel
discovery, sync, permissions) end-to-end. Every action it takes calls the
same `trenchchat.core.actions` entry points the real client does, so a bug
caught there is a bug in the real client. See
[devtools/testenv/README.md](devtools/testenv/README.md).

```bash
.venv/Scripts/python devtools/testenv/orchestrator.py   # Windows
.venv/bin/python devtools/testenv/orchestrator.py        # Linux/macOS
```

Then visit `http://localhost:8800/`.

Run the test suite after any change to `trenchchat/`:

```bash
.venv/Scripts/python -m pytest tests/ -v   # Windows
.venv/bin/python -m pytest tests/ -v       # Linux/macOS
```

## How It Works

TrenchChat assigns every user a stable cryptographic identity derived from an Ed25519/X25519 keypair stored locally at `~/.trenchchat/identity`. Channels are addressed by a hash derived from the creator's identity and the channel name. Messages are unicast LXMF packets sent directly to each subscriber; there is no broadcast or multicast layer.

### Channels

| Type | Discovery | Membership |
|------|-----------|------------|
| Public | Announced on the mesh; anyone can join | Subscriber list maintained by channel owner |
| Invite-only | Not announced publicly | Versioned, signed member-list document circulated among members |

### Propagation Nodes

Any client can be designated as a propagation node (`Settings → Propagation`). Other clients can point their *outbound propagation node* at it to receive messages buffered while they were offline. This is an infrastructure-level supplement to the built-in offline sync.

## Offline Sync

When a message cannot be delivered because a channel member is offline, TrenchChat uses a three-part mechanism to ensure they receive it when they reconnect.

> See [docs/offline-sync.md](docs/offline-sync.md) for a full technical description.

In brief:
1. **Pending retry**: the sender queues the message and retries delivery when the peer reappears on the mesh.
2. **Missed-delivery hints**: the sender notifies all currently-online members that a specific peer missed a specific message, so any of them can serve it later.
3. **Timestamp-fallback sync**: on reconnect, a peer requests all messages it missed since it was last seen; any online member can respond.

## Project Layout

```
main_flutter.py             Entry point: backend + Flutter client in one command
requirements.txt
setup.sh / setup.bat
flutter_ui/                 Flutter client (the active UI): desktop + web
  lib/api/                  HTTP/WS client and models
  lib/app_state.dart        Client-side state, fed by the WebSocket event stream
  lib/screens/              Main window, tabs, and dialogs
  test/                     Widget, layout, and golden tests (flutter test)
trenchchat/
  config.py                 Configuration (data dir, propagation settings)
  tray.py                   Tray icon the app drops to when its window closes
  single_instance.py        Hands a second launch to the instance already running
  core/
    identity.py             Keypair management
    channel.py              Channel creation and announce
    messaging.py            Send / receive chat messages
    subscription.py         Subscribe / unsubscribe, subscriber list sync
    invite.py               Invite token flow and signed member-list documents
    storage.py              SQLite persistence
    sync.py                 Offline sync (missed-delivery hints + gap fill)
  network/
    router.py               LXMFRouter lifecycle and propagation node
    announce.py             Reticulum announce handlers
devtools/
  testenv/                  Two-process local dev/test environment (see its README);
                            also the FastAPI backend the Flutter client talks to
docs/
  offline-sync.md           Offline sync design and implementation detail
  security-improvements.md  Application-layer security hardening notes
tests/                      pytest suite; run after any change to trenchchat/
```

## Data Storage

All application data is stored under `~/.trenchchat/`:

| Path | Contents |
|------|----------|
| `identity` | Ed25519/X25519 keypair |
| `storage.db` | SQLite: channels, messages, subscriptions, members, missed-delivery hints |
| `messagestore/` | LXMF propagation node message store (if enabled) |
| `launcher.log` | Console output from an installed build, which runs without a console |
<<<<<<< HEAD
| `launcher.json` | Where the running instance's API is, so a second launch can hand over to it |
=======

## License

TrenchChat is released under the [MIT License](LICENSE).

It depends on [Reticulum](https://github.com/markqvist/Reticulum) and
[LXMF](https://github.com/markqvist/LXMF), which are distributed under the
Reticulum License, MIT-style, with restrictions on use in systems designed
to harm people and on use in AI/ML training datasets. Those terms travel
with the bundled copies of `rns` and `lxmf` in binary distributions.
>>>>>>> 7fdd23c (Remove the legacy Qt client and license the project MIT)
