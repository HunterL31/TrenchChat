# TrenchChat local test environment

Two fully independent, real TrenchChat backends ("Tester A" and "Tester B"),
each its own OS process with its own standalone Reticulum instance,
connected over a real point-to-point TCP interface -- driven from a
two-pane web UI instead of two physical machines and a manual key
exchange.

Every mutating action in the API (`api.py`) calls the exact same
`trenchchat.core.actions` functions and manager methods that
`trenchchat/gui/main_window.py` calls -- there is no separate
reimplementation of GUI logic to drift out of sync. A bug caught here is
a bug in the real client.

## Setup

```bash
.venv/Scripts/pip install -r devtools/testenv/requirements.txt   # Windows
.venv/bin/pip install -r devtools/testenv/requirements.txt       # Linux/macOS
```

## Run

```bash
.venv/Scripts/python devtools/testenv/orchestrator.py
```

Then visit `http://localhost:8800/` (or `http://<this-machine's-LAN-ip>:8800/`
from another device on the network). Both testers spin up automatically on
a fresh, empty state -- no channels, no membership, brand-new identities.

Create a channel in either pane, click "Invite {other tester}", accept the
invite in the other pane, and send messages back and forth. Nothing is
auto-accepted -- accepting an invite is an explicit click, same as the
real GUI's invite bar.

Click **Reset environment** to kill both testers, wipe their data
directories, and relaunch fresh -- back to two brand-new identities with
no history.

## Smoke test (no web UI)

Proves the underlying two-process real-networking design works, without
the API/UI layer on top:

```bash
.venv/Scripts/python devtools/testenv/smoke_test.py
```

## Ports (fixed)

| Port  | What |
|-------|------|
| 8800  | Orchestrator (the web page + `/reset`) |
| 8801  | Tester A's API + WebSocket |
| 8802  | Tester B's API + WebSocket |
| 41001 | Tester A's Reticulum TCP listener (Tester B dials this) |

## Files

| File | Purpose |
|------|---------|
| `backend_core.py` | Headless backend wiring (Identity/Storage/Router/managers), per-tester Reticulum config generation |
| `api.py` | FastAPI wrapper -- every endpoint calls `trenchchat.core.actions` or a manager method directly |
| `worker.py` | Subprocess entry point: one tester's `Backend` + its `uvicorn` server |
| `orchestrator.py` | Spawns both testers, serves the UI, handles `/reset` |
| `static/index.html` | Two-pane vanilla JS/HTML UI |
| `smoke_test.py` | Headless proof that two real processes can invite/join/message over a real TCP link |

## Known real-app quirks this environment will make you run into

Not harness bugs -- genuine behavior of the production code, worth
knowing before you go looking for a bug in your own changes:

- **Invites/join-requests don't retry.** Unlike chat messages,
  `invite.py`'s `_send_raw()` has no pending-retry queue. If the
  recipient's network path isn't resolved at the exact instant you
  invite/accept, the message is silently dropped. A real human rarely
  hits this (natural delay gives the path time to resolve); a fast
  scripted test can.
- **No delivery-ordering guarantee.** Two independent LXMF sends (e.g. a
  member-list update immediately followed by a chat message) can arrive
  out of order. `messaging.py` drops a chat message if the receiver
  isn't yet marked subscribed/member locally.
- **Invite notifications show a hash fragment, not the channel name**,
  for a never-before-seen invite-only channel. `send_invite()` never
  includes `F_CHANNEL_NAME` in its LXMF fields (the constant exists in
  `protocol.py`, it's just not populated here), so the receiver falls
  back to `channel_hash_hex[:12]`.
