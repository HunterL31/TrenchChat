---
name: launch-web-clients
description: >
  Launch and host the TrenchChat Flutter web clients and dev environment for
  a person to use, via devtools/testenv/remote_host.sh (Tailscale). Use this
  whenever the user asks to run/start/host/serve/demo the web client(s), the
  dev environment, or a browser-testable build, or says "let me try it" /
  "can I open it" about the Flutter UI — even if they don't say "web",
  "Tailscale", or name the script.
---

# Launching the web clients (remote_host.sh)

`devtools/testenv/remote_host.sh` is the one supported way to stand the web
clients up for a person. It downloads Tailscale (userspace networking — no
root or tun device needed), starts the dev environment (orchestrator +
testers) and a dedicated web-client identity, and serves everything over the
user's tailnet. Do not hand-roll orchestrator + static-server + tunnel
setups instead; per-port HTTP tunnels in particular break the dev
environment page, whose JS calls each tester on the same hostname at a
different port — a property only the shared tailnet IP preserves.

## Start

```
TRENCHCHAT_REMOTE_VENV=$PWD/.venv TESTENV_TESTERS=2 \
    bash devtools/testenv/remote_host.sh start
```

- `TRENCHCHAT_REMOTE_VENV=$PWD/.venv` reuses the repo venv instead of
  building a second one (the script otherwise creates its own under
  `~/.trenchchat-remote`). Make sure
  `devtools/testenv/requirements.txt` is installed in it.
- `TESTENV_TESTERS` defaults to 4; 2 is plenty for a demo.
- The script builds `flutter_ui/build/web` itself only if a `flutter`
  binary is on PATH; otherwise build the bundle first. If the SDK lives
  outside the repo (e.g. a scratchpad install), run
  `git config --global --add safe.directory <sdk-path>` first or every
  flutter command fails with "dubious ownership".

## Tailscale auth

Check the environment for `TS_AUTHKEY` / `TS_AUTH_KEY` before starting —
deployments often inject one, and with it the join is fully non-interactive.
Without a key the script prints a one-time login.tailscale.com URL: relay it
to the user to authorize the node on their tailnet, then run `start` again.
State persists in `~/.trenchchat-remote`, so later restarts reconnect
without re-authenticating and the client identity survives too.

## What comes up, and the URLs to hand over

`status` (also printed after `start`) shows the tailnet URLs:

- `http://<ts-ip>:8899/` — the user's own web client, backed by a dedicated
  persistent identity joined to the testenv mesh.
- `http://<ts-ip>:8800/` — the dev environment page: one pane per tester
  with link-shaping, offline/kill, and voice Tone controls.
- `http://<ts-ip>:8899/?api=http://<ts-ip>:880N` — the same client bundle
  driving tester N's identity (8801, 8802, …), for a second browser tab in
  a two-party demo.

`stop` and `status` subcommands manage the stack. It lives only as long as
the host machine/container does.

## Verifying from inside the host

Userspace tailscaled means the tailnet IP is NOT loopback-reachable from
the host itself — that is normal, not a failure. Verify services on
`127.0.0.1` (`:8800`, `:8899`, tester APIs on `:8801`+) and trust the
tailnet once `status` reports BackendState Running.

## Demoing voice on the hosted stack

1. Create an open channel from one identity; wait for the other side to
   list it under `GET /channels/discovered` before joining (announce
   propagation isn't instant), then give the subscriber broadcast a few
   seconds.
2. Join voice from each client (JOIN VOICE in the UI, or
   `POST /channels/{h}/voice/join`).
3. Device-less hosts have no microphones — "speech" is the built-in tone:
   the Tone button on each tester pane of the :8800 page, or
   `POST /voice/test_tone {"enabled": true}`. A 409 means that identity
   hasn't joined voice yet.
4. Panels showing "NO AUDIO DEVICE — LISTENING ONLY" on a device-less host
   are correct behavior, not a failure; audible audio requires running the
   stack on a machine with real sound devices.
5. Real-network flows are eventual: poll with deadlines (discovery, roster
   convergence) rather than sleeping fixed amounts and hoping.
