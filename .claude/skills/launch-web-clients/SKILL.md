---
name: launch-web-clients
description: >
  Launch, host, or demo the TrenchChat Flutter web clients and the testenv dev
  environment — locally, headless-in-a-container, or hosted to a human over
  Tailscale via remote_host.sh. Use this whenever the user asks to run/start/
  host/serve/demo the web client(s), the dev environment, the orchestrator, a
  browser-testable build, a two-client voice demo, or says "let me try it" /
  "can I open it" about the Flutter UI — even if they don't say "web" or name
  a script. Also use it before driving the web UI with Playwright.
---

# Launching the TrenchChat web clients

Three launch modes, one shared foundation. Pick by who needs to reach the UI.

## Prerequisites (all modes)

- A web bundle at `flutter_ui/build/web/` (gitignored). If missing:
  `cd flutter_ui && flutter build web`. If the Flutter SDK lives outside the
  repo (e.g. a scratchpad install), `git config --global --add safe.directory
  <sdk-path>` first or every flutter command fails with "dubious ownership".
- Testenv server deps in the venv:
  `.venv/bin/pip install -r devtools/testenv/requirements.txt`
  (fastapi/uvicorn are not in the main requirements.txt).

How a client finds its backend (`flutter_ui/lib/main.dart` `resolveBaseUrl`):
dart-define → web `?api=` query param → the page's own origin → desktop
`TC_API_URL`. The `?api=` param is what lets one served bundle drive any
number of backend identities.

## Mode 1 — local dev environment (same machine)

```
.venv/bin/python devtools/testenv/orchestrator.py --testers 2
```

Serves the harness page on :8800 and one API per tester on :8801, :8802, …
over a real inter-worker Reticulum TCP link. Serve the web bundle with any
static server (e.g. `python -m http.server 8090 -d flutter_ui/build/web`) and
open one client per tester: `http://localhost:8090/?api=http://localhost:8801`.

For a single client on the machine's real `~/.trenchchat` profile instead,
use `main_flutter.py` (bundles backend + client, opens the browser itself).

## Mode 2 — hosted for a human over Tailscale (containers, remote boxes)

`devtools/testenv/remote_host.sh` does everything: downloads Tailscale
(userspace networking — no root/tun needed), starts the orchestrator and a
dedicated web-client identity, and serves it all on the tailnet.

```
TRENCHCHAT_REMOTE_VENV=$PWD/.venv TESTENV_TESTERS=2 \
    bash devtools/testenv/remote_host.sh start
```

- Auth: non-interactive when `TS_AUTHKEY`/`TS_AUTH_KEY` is set (check the
  environment first — deployments often inject one); otherwise it prints a
  one-time login.tailscale.com URL to relay to the user, then re-run `start`.
  State persists in `~/.trenchchat-remote`, so restarts reconnect silently.
- Point `TRENCHCHAT_REMOTE_VENV` at the repo's `.venv` to skip a duplicate
  dependency install.
- It prints the URLs when up: `http://<ts-ip>:8899/` (the user's own client
  identity) and `http://<ts-ip>:8800/` (the dev environment page). A second
  browser tab as a tester: `http://<ts-ip>:8899/?api=http://<ts-ip>:880N`.
  All ports share the tailnet IP, which keeps the harness page's
  same-hostname-different-port JS working — don't substitute per-port HTTP
  tunnels for this reason.
- `stop` / `status` subcommands; the stack lives only as long as the host.
- Userspace tailscaled means the tailnet IP is NOT loopback-reachable from
  inside the container itself — verify services via `127.0.0.1`, and trust
  the tailnet for external reachability once `status` shows Running.

## Mode 3 — headless verification (Claude driving the UI itself)

Playwright + the preinstalled Chromium. The executable is versioned:
use the binary matching `/opt/pw-browsers/chromium-*/chrome-linux/chrome`
(the bare `/opt/pw-browsers/chromium/` path does not exist).

Flutter web renders to a canvas — there is no DOM to assert on and
programmatic clicks are unreliable. Verify by driving state through the
tester APIs (the same endpoints the UI calls) and reading screenshots;
give the first paint ~10 s. A worker pair can be launched directly without
the orchestrator, mirroring `devtools/testenv/worker.py`'s argv (see
`devtools/testenv/smoke_test.py` for the role/port wiring).

## Demoing voice

1. Create an open channel from one identity, wait for the other side to see
   it under `GET /channels/discovered` before `POST /channels/{h}/join`
   (announce propagation isn't instant), then give the subscriber-list
   broadcast a few seconds.
2. Join voice from each client (JOIN VOICE in the UI, or
   `POST /channels/{h}/voice/join`).
3. "Speech" in device-less environments is the built-in tone: the Tone
   button on each tester pane of the :8800 page, or
   `POST /voice/test_tone {"enabled": true}`. A 409 means that identity
   hasn't joined voice yet — join first.
4. Containers have no sound devices, so panels showing
   "NO AUDIO DEVICE — LISTENING ONLY" is correct behavior, not a failure;
   audible audio needs the backend host to have real devices.

## Known quirks worth not rediscovering

- There is no `GET /channels/{h}/presence` or `/link_quality` endpoint —
  the client composes those from members + the network map (see the
  "Phase B seams" section at the bottom of `flutter_ui/lib/api/client.dart`).
- The `▾` section-marker glyph renders as a box on web (bundled fonts lack
  it); the ONLINE and VOICE section labels are both affected. Cosmetic.
- Real-network flows are eventual: poll with deadlines (discovery, roster
  convergence, sync) rather than sleeping fixed amounts and hoping.
