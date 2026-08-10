# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

TrenchChat is a decentralized, end-to-end encrypted group chat application built on the
[Reticulum Network Stack](https://reticulum.network/) (RNS) and [LXMF](https://github.com/markqvist/LXMF).
There is no server: every client is a peer, addressed by a cryptographic identity, and messages are
unicast LXMF packets sent directly to each subscriber (no broadcast/multicast layer). It runs over
whatever transport Reticulum supports (LoRa, packet radio, TCP/IP, serial, etc).

## Commands

```bash
# Setup (creates .venv, installs requirements.txt)
./setup.sh          # Linux/macOS
setup.bat           # Windows

# Run the app
.venv/bin/python main.py            # Linux/macOS
.venv\Scripts\python main.py        # Windows
# -v/--verbose enables TrenchChat debug logging; --rns-debug enables full RNS firehose logging

# Run the full test suite (required after any change to trenchchat/)
.venv/bin/python -m pytest tests/ -v          # Linux/macOS
.venv\Scripts\python -m pytest tests/ -v      # Windows

# Run a single test file / test
.venv/bin/python -m pytest tests/test_sync.py -v
.venv/bin/python -m pytest tests/test_sync.py::test_missed_delivery_hint -v
```

Packaging (PyInstaller + platform installers) lives under `packaging/` and `trenchchat.spec`; not
needed for normal development.

## Non-obvious architecture

### Identity, destinations, and messages

- Every user has a stable Ed25519/X25519 keypair (`trenchchat/core/identity.py`) stored at
  `~/.trenchchat/identity`. The **identity hash** and the **LXMF delivery destination hash** are
  different values: `delivery_dest_hash = RNS.Destination.hash(identity_hash_bytes, "lxmf", "delivery")`.
  `RNS.Identity.recall()` takes a destination hash, not an identity hash — get this wrong and lookups
  silently fail. See `.claude/rules/reticulum-lxmf-guidelines.md` for the exact patterns to follow
  (path requests must never block with `time.sleep`; use fire-and-forget + retry queue instead).
- Channels are addressed by a hash derived from the creator's identity + channel name. Public channels
  are announced on the mesh; invite-only channels are not, and use a versioned, signed member-list
  document instead (`trenchchat/core/invite.py`).
- All LXMF field keys and message-type strings live in **`trenchchat/core/protocol.py`** — the single
  source of truth, deliberately dependency-free to avoid circular imports. Never redefine a field
  constant elsewhere; the field layout docstring at the top of `trenchchat/core/messaging.py` documents
  the registry. Chat messages carry no `F_MSG_TYPE`; control messages always do.

### Core managers (`trenchchat/core/`)

`identity.py`, `channel.py`, `messaging.py`, `subscription.py`, `invite.py`, `sync.py`, `storage.py`
(SQLite, optionally SQLCipher-encrypted), `permissions.py`, `presence.py`, `reaction.py`, `avatar.py`,
`user_directory.py`, `lockbox.py` (PIN-based encryption gate), `link_quality.py`, `image.py`,
`fileutils.py`. GUI code (`trenchchat/gui/`) must never construct LXMF messages or touch protocol
fields directly — it reads `Storage` for display and delegates all mutations to the relevant core
manager. RNS/LXMF callbacks fire on background threads; GUI code must marshal into Qt via signals,
never call widget methods directly from a callback.

### Offline sync — three independent, complementary mechanisms

Full detail in `docs/offline-sync.md`. Summary:
1. **Pending retry** (`messaging.py`) — sender queues messages for a peer whose path is unknown or
   whose delivery timed out; flushed when the peer's presence is detected again (in-memory only).
2. **Missed-delivery hints** (`sync.py` + `missed_deliveries` table) — sender broadcasts a hint to
   currently-reachable subscribers naming which peer missed which message; any of them can serve it
   later via sync.
3. **Timestamp-fallback sync** — on reconnect, a peer requests everything since its last sync
   timestamp; any online member can respond, checking hints first, then falling back to a bounded
   timestamp query (`SYNC_WINDOW_DAYS = 7`).

Peer reconnect is detected via `PeerAnnounceHandler` (`trenchchat/network/announce.py`), which drives
all three mechanisms.

### Permission enforcement — three independent layers, always

Every permission (`SEND_MESSAGE`, `INVITE`, `KICK`, `MANAGE_ROLES`, `MANAGE_CHANNEL`, ...) must be
checked at all three layers, because a gap at any one layer lets a bad client or bug bypass it:

1. **GUI gate** — hide the control (convenience only, never sufficient alone).
2. **GUI outbound guard** — re-check in the action handler before calling core.
3. **Core inbound enforcement** — the core manager rejects the operation regardless of caller; this is
   the only layer that protects against a malicious peer calling in directly.

When adding a new permission, add all three layers plus an adversarial test in
`tests/test_adversarial.py` that calls the core method directly, bypassing the GUI. See
`.claude/rules/permission-enforcement.md` for the full mapping of existing permissions to their
enforcement points.

### Member-list document security (`trenchchat/core/invite.py`)

`_validate_document` must check the signer against the **previously stored** member list (not the
incoming doc's own admin/owner claims — trusting those lets an attacker grant themselves authority).
Fallback order when no stored list exists: stored list → channel's `creator_hash` → (only if no local
record exists at all) the doc's own signers, for first-invite bootstrap. `_accept_document` must verify
`doc["channel_hash"]` matches the target channel before validating signatures. Permissions-only changes
must go through `broadcast_permissions`, not `publish_member_list` (the latter calls `replace_members`,
which wipes display names and can demote the owner). Full rationale in
`.claude/rules/member-list-security.md`.

### Known application-layer hardening gaps

`docs/security-improvements.md` documents three known, not-yet-fixed gaps and design options for each:
unsigned subscriber-list updates on public channels (spoofable), display-name spoofing (self-asserted,
unverified), and no rate limiting on inbound control messages. Reticulum/LXMF's crypto (X25519 +
AES-256, Ed25519 signing) is not in question — these are all application-layer trust gaps. Read this
doc before touching `subscription.py`'s `MT_SUBSCRIBER_LIST` handling or any control-message ingestion
path.

## Test architecture

Tests are integration tests that exercise real `Identity`/`Storage`/`Router`/manager objects, not
mocks. `tests/conftest.py`'s `peer_factory` fixture builds fully-wired `TestPeer`s sharing one
session-scoped `RNS.Reticulum` instance; a `TestTransport` shim intercepts `router.send()` and delivers
directly to the recipient's callbacks (async, via a thread, matching real LXMF timing) instead of
going over the network. `tests/helpers.py` has `wait_for`-style polling helpers for the resulting
eventual-consistency assertions. **Tests are the specification**: never weaken or delete a test to make
it pass — a failing test after a change means the change conflicts with intended behavior; fix the
implementation, or if the behavior change is intentional, replace the test with one covering the new
contract.

## Code conventions

Full detail in `.claude/rules/code-standards.md`. Highlights not obvious from skimming the code:
- Import order: stdlib / third-party / local, each group blank-line separated.
- Type hints on all public signatures; `X | None`, not `Optional[X]`.
- LXMF may deliver string fields as `bytes` depending on msgpack encoding — always coerce with
  `isinstance(value, bytes)` → `.decode(errors="replace")`; extract a helper if this appears more than
  twice in one function.
- Never reach into another class's `_private` attributes; add a public method instead.
- msgpack: pack with `use_bin_type=True`; unpack with `raw=False` **except** stored member-list blobs,
  which use `raw=True` and byte-string keys (`b"members"`, `b"admins"`).
- Logging is always `RNS.log(...)`, never `print`/stdlib `logging`, prefixed `"TrenchChat:"` (or
  `"TrenchChat [subsystem]:"`).
- 100-column line limit.
- Comments are kept to a minimum — only to explain genuinely confusing code, never to justify
  design decisions.

## Working in this repo

- Run the full test suite after any change to `trenchchat/` and don't consider the task done until it
  passes — see `.claude/rules/run-tests-after-changes.md`.
- New functionality needs a test in the matching `tests/test_*.py` file (bug fixes need a regression
  test; new permissions need an adversarial test) — see `.claude/rules/test-coverage-for-new-features.md`.
- Never commit, push, merge, rebase, or tag without an explicit request, and never push directly to
  `main` (protected — always a feature branch + PR) — see `.claude/rules/git-safety.md`.
