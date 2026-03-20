# TrenchChat

A decentralized, encrypted group chat application built on the [Reticulum Network Stack](https://reticulum.network/) and [LXMF](https://github.com/markqvist/LXMF). TrenchChat works across any transport Reticulum supports — LoRa, packet radio, TCP/IP, serial links, and more — without a central server.

## Features

- **Serverless** — every client is a peer; no accounts, no servers, no phone numbers
- **End-to-end encrypted** — all messages are encrypted by Reticulum using X25519 + AES-256
- **Public and invite-only channels** — open channels anyone can join; invite-only channels with cryptographically-signed member lists
- **Offline sync** — messages sent while you were offline are delivered when you reconnect; see [Offline Sync](docs/offline-sync.md)
- **Propagation node support** — optionally designate a node as a store-and-forward relay
- **Dark-themed Qt6 GUI**

## Requirements

- Python 3.10+
- Dependencies listed in `requirements.txt`

```
rns
lxmf
PyQt6
msgpack
```

## Setup

**Linux / macOS**

```bash
./setup.sh
```

**Windows**

```bat
setup.bat
```

Both scripts create a virtual environment and install all dependencies.

## Running

```bash
# Linux / macOS
source .venv/bin/activate
python main.py

# Windows
.venv\Scripts\activate
python main.py
```

Pass `-v` / `--verbose` to enable detailed Reticulum and TrenchChat debug logging.

## How It Works

TrenchChat assigns every user a stable cryptographic identity derived from an Ed25519/X25519 keypair stored locally at `~/.trenchchat/identity`. Channels are addressed by a hash derived from the creator's identity and the channel name. Messages are unicast LXMF packets sent directly to each subscriber — there is no broadcast or multicast layer.

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
1. **Pending retry** — the sender queues the message and retries delivery when the peer reappears on the mesh.
2. **Missed-delivery hints** — the sender notifies all currently-online members that a specific peer missed a specific message, so any of them can serve it later.
3. **Timestamp-fallback sync** — on reconnect, a peer requests all messages it missed since it was last seen; any online member can respond.

## Project Layout

```
main.py                     Entry point
requirements.txt
setup.sh / setup.bat
trenchchat/
  config.py                 Configuration (data dir, propagation settings)
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
    prop_filter.py          Propagation allowlist filter
  gui/
    main_window.py          Main Qt window
    channel_view.py         Per-channel message display
    compose.py              Message compose widget
    invite_dialogs.py       Invite and member management dialogs
    settings.py             Settings dialog
docs/
  offline-sync.md           Offline sync design and implementation detail
  security-improvements.md  Application-layer security hardening notes
```

## Data Storage

All application data is stored under `~/.trenchchat/`:

| Path | Contents |
|------|----------|
| `identity` | Ed25519/X25519 keypair |
| `storage.db` | SQLite: channels, messages, subscriptions, members, missed-delivery hints |
| `messagestore/` | LXMF propagation node message store (if enabled) |
