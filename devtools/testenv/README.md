# TrenchChat local test environment

N fully independent, real TrenchChat backends ("Tester A", "Tester B", ...),
each its own OS process with its own standalone Reticulum instance, all
connected through a shared headless transport hub (`hub.py`) over real TCP
-- driven from an N-pane web UI instead of physical machines and a manual
key exchange.

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
.venv/Scripts/python devtools/testenv/orchestrator.py                # 4 testers (default)
.venv/Scripts/python devtools/testenv/orchestrator.py --testers 6    # up to 8
```

Then visit `http://localhost:8800/` (or `http://<this-machine's-LAN-ip>:8800/`
from another device on the network). Every tester spins up automatically on
a fresh, empty state -- no channels, no membership, brand-new identities.

Create a channel in one pane, click "Invite" and pick another tester, accept
the invite in that pane, and send messages back and forth. Nothing is
auto-accepted -- accepting an invite is an explicit click, same as the
real GUI's invite bar.

Click **Reset environment** to kill every tester and the hub, wipe their
data directories, and relaunch fresh -- back to brand-new identities with
no history.

## Taking a tester offline

Each pane has two independent controls, and they simulate two different
kinds of real-world outage:

- **Go offline / Go online** drops or restores that tester's link to the
  hub (`Backend.go_offline`/`go_online` in `backend_core.py`) without
  killing the process. This is the closer analog to a phone losing signal
  or a laptop's Wi-Fi dropping: the process, its in-memory state (pending
  message retry queue, sync status, subscriber cache) all survive. Going
  back online takes 5-15 seconds to actually reconnect -- the pane shows
  "reconnecting..." for that window, and turns visibly `LINK DOWN`
  (tinted header, reduced opacity, badge) while offline.
- **Kill / Start** terminates the OS process outright. Everything in
  memory is gone; only what's on disk survives (the SQLite database and
  the identity file). Restarting relaunches the process against that same
  data directory, so it comes back as the same identity with its full
  message history but a cold cache -- closer to a real app being force-quit
  and reopened.

## Smoke test (no web UI)

Proves the underlying two-process real-networking design works, without
the API/UI layer on top:

```bash
.venv/Scripts/python devtools/testenv/smoke_test.py
```

## Ports

| Port          | What |
|---------------|------|
| 8800          | Orchestrator (the web page, `/config`, `/status`, `/reset`, per-tester/hub controls) |
| 8801, 8802, ... | Each tester's API + WebSocket, one port per tester in tag order (A, B, C, ...) |
| 41001         | The hub's Reticulum TCP listener -- every tester dials this |

`--testers N` (default 4, capped at 8) controls how many testers launch, and
therefore how many API ports are used above 8801.

## Endpoints

### Orchestrator (port 8800)

| Endpoint | Purpose |
|----------|---------|
| `GET /config` | `{"testers": [{"tag","display_name","api_port"}, ...], "hub_port"}` |
| `GET /status` | `{"testers": {tag: {"alive","api_port"}}, "hub": {"alive"}}` |
| `POST /reset` | Kill everything, wipe all data, relaunch fresh |
| `POST /testers/{tag}/kill` | Terminate one tester's process |
| `POST /testers/{tag}/start` | Launch one tester's process (data dir untouched) |
| `POST /testers/{tag}/restart` | Kill then start one tester |
| `POST /testers/{tag}/reset` | Kill, wipe only that tester's data dir, start |
| `POST /hub/kill` / `/start` / `/restart` | Same lifecycle controls for the hub process |

### Per-tester API (ports 8801+)

All the channel/message/invite/permission endpoints described in `api.py`,
plus a link-control group with no `actions.py` counterpart (see the note at
the top of `api.py` -- this is dev-harness process control, not app logic):

| Endpoint | Purpose |
|----------|---------|
| `GET /net/status` | `{"online","detached","interface","rxb","txb"}` |
| `POST /net/offline` | Detach this tester's link to the hub |
| `POST /net/online` | Start reconnecting (takes 5-15s; poll `/net/status` or watch the `net_status` WS event) |

## Files

| File | Purpose |
|------|---------|
| `backend_core.py` | Headless backend wiring (Identity/Storage/Router/managers), per-tester Reticulum config generation, link online/offline control |
| `hub.py` | Standalone headless Reticulum transport node every tester connects through |
| `api.py` | FastAPI wrapper -- every endpoint calls `trenchchat.core.actions` or a manager method directly (except the link-control group -- see above) |
| `worker.py` | Subprocess entry point: one tester's `Backend` + its `uvicorn` server |
| `orchestrator.py` | Spawns the hub and every tester, serves the UI, handles `/reset` and per-tester/hub lifecycle |
| `static/index.html` | N-pane vanilla JS/HTML UI, laid out as a CSS grid so every pane stays visible at once |
| `smoke_test.py` | Headless proof that two real processes can invite/join/message over a real TCP link |

## Adding a new feature

This environment is meant to double as a prototyping ground: build a
feature here first, verify it against a real multi-peer network, then port
it to `trenchchat/gui/main_window.py` with minimal changes -- not a
redesign. That only works if every feature follows the same shape:

1. **Business logic goes in `trenchchat/core/actions.py`**, not in the
   GUI and not in `api.py`. If a feature needs more than one manager call
   (a permission check before a mutation, a computed recipient list, a
   create-then-follow-up-call sequence), it's a plain function in
   `actions.py` taking already-constructed managers as arguments. See
   `send_message`, `create_channel`, `update_membership` for the
   established shape.
2. **`main_window.py`'s `_on_*` handlers call that function.** They keep
   the Qt-specific bits (dialogs, message boxes, widget refreshes) and
   delegate everything else to `actions.py`.
3. **`api.py`'s endpoints call the same function.** Not a parallel
   reimplementation -- literally the same import, same call. This is
   what makes a bug caught here a real bug, and a feature proven here a
   feature ready to port. (Dev-harness process control -- like the
   link-control endpoints above -- is the one deliberate exception.)
4. **New core managers get instantiated in `backend_core.py`'s
   `Backend.__init__`**, mirroring `main.py`'s wiring order exactly
   (identity → storage → router → managers). If `main.py` constructs it
   with `ManagerX(identity, storage, router)`, `Backend` should too.
5. **The frontend (`static/index.html`) is disposable.** It doesn't need
   to match the real GUI's visual design -- it needs to exercise the
   real code paths convincingly enough to prove a feature works before
   it's worth the GUI polish investment in `main_window.py`.

When a feature is missing something the real client has (a manager never
instantiated, an endpoint never written), that's not a design decision --
it just hasn't been ported yet. Check `trenchchat/core/` for the
equivalent manager/action before writing new logic; there's usually
already a correct implementation to call.

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
- **Real network round trips are slow compared to the pytest suite.**
  `tests/` uses a `TestTransport` shim that delivers LXMF messages
  in-process; this environment goes over real RNS Links. A chain like
  invite → join request → member-list update → sync request → sync
  response is four separate hops, not one -- give it several seconds
  before concluding something didn't work.
- **Link drop vs process kill preserve different state.** Going offline
  keeps everything in memory (pending retry queue, sync status,
  subscriber cache) and only tears down the network link; killing the
  process loses all of that and leaves only what made it to the SQLite
  database and identity file on disk. A bug that only reproduces after a
  real restart won't show up from a link drop alone.
