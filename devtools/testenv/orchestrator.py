"""
TrenchChat local N-tester test environment.

Run:
    python devtools/testenv/orchestrator.py [--testers N]

Then visit http://<this-machine's-LAN-ip>:8800/ from any device on the
network (or http://localhost:8800/ locally). You'll get N independent,
fully-isolated TrenchChat identities ("Tester A", "Tester B", ...) each
running as its own real process with its own real Reticulum instance, all
connected to a shared headless transport hub (hub.py) over TCP -- create
channels, invite each other, send messages, exactly as real installations
would, no manual key exchange required (each pane already knows every
other tester's identity hash).

Every action the UI takes calls the same trenchchat.core.actions /
manager entry points the Qt GUI calls (see api.py) -- there is no
separate reimplementation to drift out of sync.

Click "Reset environment" to kill every tester and the hub, wipe their
data directories back to a fresh-install state, and relaunch them.

Ports (fixed, all on this machine):
    8800        orchestrator (this page + /reset)
    8801, 8802, ... one API + WebSocket port per tester, in tag order
    41001       hub's Reticulum TCP listener (every shaper dials this)
    41101, 41102, ... one link shaper per tester, in tag order -- the tester
                dials this, so its link can be given a simulated bandwidth,
                latency and frame loss (see link_profiles.py)
"""

import argparse
import asyncio
import os
import secrets
import shutil
import socket
import string
import subprocess
import sys
import time
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from link_profiles import (
    DEFAULT_PROFILE_NAME, LINK_PROFILES, LinkProfile, resolve as resolve_profile,
)
from link_shaper import ShaperPool

_TESTENV_DIR = Path(__file__).resolve().parent
_DATA_DIR = _TESTENV_DIR / "data"
_WORKER = _TESTENV_DIR / "worker.py"
_HUB = _TESTENV_DIR / "hub.py"

_ORCH_PORT = 8800
_API_PORT_BASE = 8801
_LINK_PORT = 41001
_SHAPER_PORT_BASE = 41101

_DEFAULT_TESTERS = 4
_MAX_TESTERS = 8

_LINK_STATUS_TIMEOUT_SECS = 1.0

_HUB_DATA_DIR = _DATA_DIR / "hub"
_HUB_INSTANCE_NAME = "trenchchat_testenv_hub"

_TESTERS: dict[str, dict] = {}
_processes: dict[str, subprocess.Popen] = {}
_hub_process: subprocess.Popen | None = None
_shapers = ShaperPool("127.0.0.1", _LINK_PORT)

# One token for the whole environment: every tester API accepts it, and the
# page gets it from /config. The testers' APIs drive real identities, so they
# are not left open even on a dev box.
_API_TOKEN = secrets.token_urlsafe(32)
_AUTH_HEADERS = {"x-tc-token": _API_TOKEN}

# Bind address for the orchestrator and the tester APIs it launches.
# Localhost unless deliberately widened -- see main()'s --host.
_BIND_HOST = "127.0.0.1"

# Extra browser origins the tester APIs must accept, for hosts this process
# cannot discover for itself -- see _page_origins.
_EXTRA_PAGE_ORIGINS: list[str] = []


def _tags(n: int) -> list[str]:
    return list(string.ascii_uppercase[:n])


def _build_testers(n: int) -> dict[str, dict]:
    testers = {}
    for i, tag in enumerate(_tags(n)):
        testers[tag] = dict(
            tag=tag, data_dir=_DATA_DIR / f"tester{tag}", display_name=f"Tester {tag}",
            role="client", listen_port=_LINK_PORT, peer_host="127.0.0.1",
            peer_port=_SHAPER_PORT_BASE + i, api_port=_API_PORT_BASE + i,
            instance_name=f"trenchchat_testenv_{tag.lower()}", enable_transport=False,
            link_profile=DEFAULT_PROFILE_NAME, link_params=None,
        )
    return testers


def _tester_profile(tag: str) -> LinkProfile:
    """The profile a tester is currently set to, overrides applied."""
    t = _TESTERS[tag]
    return resolve_profile(t["link_profile"], **(t["link_params"] or {}))


def _lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _check_port_free(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _preflight_ports() -> None:
    """Bind-test every port this run needs before touching any process or
    data directory. An orphaned worker/hub left over from a crashed run is
    the most likely reason one of these is already taken."""
    ports = ([_ORCH_PORT, _LINK_PORT]
             + [t["api_port"] for t in _TESTERS.values()]
             + [t["peer_port"] for t in _TESTERS.values()])
    busy = [p for p in ports if not _check_port_free(p)]
    if busy:
        print(f"ERROR: port(s) already in use: {busy}")
        print("A leftover orchestrator, tester, or hub process is likely still")
        print("running from a previous session -- stop it and try again.")
        sys.exit(1)


def _launch(tag: str) -> subprocess.Popen:
    t = _TESTERS[tag]
    t["data_dir"].mkdir(parents=True, exist_ok=True)
    t["launched_bitrate"] = _tester_profile(tag).bitrate_bps
    args = [
        sys.executable, str(_WORKER), t["tag"], str(t["data_dir"]), t["display_name"],
        t["role"], str(t["listen_port"]), t["peer_host"], str(t["peer_port"]),
        str(t["api_port"]), t["instance_name"], str(t["enable_transport"]),
        str(t["launched_bitrate"]),
        _API_TOKEN, _BIND_HOST, ",".join(_page_origins()),
    ]
    env = dict(os.environ)
    heartbeat = t.get("heartbeat_secs")
    if heartbeat:
        env["TC_TESTENV_HEARTBEAT_SECS"] = str(heartbeat)
    return subprocess.Popen(args, env=env)


def _page_origins() -> list[str]:
    """Origins the orchestrator's own page can be loaded from.

    The page and each tester API differ by port, so every call it makes is
    cross-origin and needs the tester's CORS policy to name it.

    _lan_ip() only finds the address a default route goes out of, which is not
    the address a user actually reaches this host on when it is served over a
    tunnel or an overlay network -- remote_host.sh's tailnet being the case
    that matters. Those callers pass the real ones with --page-origin;
    without that every tester call from the page fails CORS.
    """
    origins = [f"http://127.0.0.1:{_ORCH_PORT}", f"http://localhost:{_ORCH_PORT}"]
    if _BIND_HOST not in ("127.0.0.1", "localhost"):
        origins.append(f"http://{_lan_ip()}:{_ORCH_PORT}")
    origins.extend(o for o in _EXTRA_PAGE_ORIGINS if o not in origins)
    return origins


def _launch_hub() -> subprocess.Popen:
    _HUB_DATA_DIR.mkdir(parents=True, exist_ok=True)
    args = [sys.executable, str(_HUB), str(_HUB_DATA_DIR), str(_LINK_PORT), _HUB_INSTANCE_NAME]
    return subprocess.Popen(args)


def _stop(tag: str) -> None:
    proc = _processes.pop(tag, None)
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _stop_hub() -> None:
    global _hub_process
    if _hub_process is not None:
        if _hub_process.poll() is None:
            _hub_process.terminate()
            try:
                _hub_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _hub_process.kill()
        _hub_process = None


def _stop_all() -> None:
    for tag in list(_processes.keys()):
        _stop(tag)
    _stop_hub()


def _wipe_data() -> None:
    if _DATA_DIR.exists():
        shutil.rmtree(_DATA_DIR, ignore_errors=True)
    _DATA_DIR.mkdir(parents=True, exist_ok=True)


def _wipe_tester(tag: str) -> None:
    data_dir = _TESTERS[tag]["data_dir"]
    if data_dir.exists():
        shutil.rmtree(data_dir, ignore_errors=True)
    data_dir.mkdir(parents=True, exist_ok=True)


def _wait_hub_ready(timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", _LINK_PORT), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def _start_all() -> None:
    global _hub_process
    _shapers.start({tag: t["peer_port"] for tag, t in _TESTERS.items()})
    _hub_process = _launch_hub()
    if not _wait_hub_ready():
        print("WARNING: hub did not open its listener in time; testers may fail to link.")
    for i, tag in enumerate(_TESTERS):
        if i > 0:
            time.sleep(0.3)
        _processes[tag] = _launch(tag)


def _wait_ready() -> dict[str, bool]:
    timeout = 30.0 + 5.0 * len(_TESTERS)
    ready = {tag: False for tag in _TESTERS}
    deadline = time.time() + timeout
    while time.time() < deadline and not all(ready.values()):
        for tag, t in _TESTERS.items():
            if ready[tag]:
                continue
            try:
                r = httpx.get(f"http://127.0.0.1:{t['api_port']}/me", timeout=1.0,
                              headers=_AUTH_HEADERS)
                if r.status_code == 200:
                    ready[tag] = True
            except httpx.HTTPError:
                pass
        if not all(ready.values()):
            time.sleep(0.5)
    return ready


app = FastAPI(title="TrenchChat test environment")


@app.get("/")
def index():
    return FileResponse(str(_TESTENV_DIR / "static" / "index.html"))


@app.get("/config")
def config():
    return {
        "testers": [
            {"tag": tag, "display_name": t["display_name"], "api_port": t["api_port"],
             "link_profile": t["link_profile"]}
            for tag, t in _TESTERS.items()
        ],
        "hub_port": _LINK_PORT,
        "link_profiles": [p.as_dict() for p in LINK_PROFILES.values()],
        "api_token": _API_TOKEN,
    }


async def _tester_link_online(client: httpx.AsyncClient, tag: str, api_port: int) -> bool | None:
    """Query a tester's own /net/status. None means dead or unreachable."""
    try:
        r = await client.get(f"http://127.0.0.1:{api_port}/net/status",
                             headers=_AUTH_HEADERS)
        if r.status_code == 200:
            return bool(r.json().get("online"))
    except httpx.HTTPError:
        pass
    return None


@app.get("/status")
async def status():
    alive = {
        tag: (_processes.get(tag) is not None and _processes[tag].poll() is None)
        for tag in _TESTERS
    }
    async with httpx.AsyncClient(timeout=_LINK_STATUS_TIMEOUT_SECS) as client:
        results = await asyncio.gather(*(
            _tester_link_online(client, tag, t["api_port"])
            for tag, t in _TESTERS.items() if alive[tag]
        ))
    link_online = dict(zip((tag for tag in _TESTERS if alive[tag]), results))

    testers = {}
    for tag, t in _TESTERS.items():
        profile = _tester_profile(tag)
        testers[tag] = {
            "alive": alive[tag], "api_port": t["api_port"],
            "link_online": link_online.get(tag) if alive[tag] else None,
            "link_profile": t["link_profile"],
            "link_summary": profile.summary(),
            "bitrate_hint_stale": profile.bitrate_bps != t.get("launched_bitrate"),
            "shaper": _shapers.stats(tag),
        }
    hub_alive = _hub_process is not None and _hub_process.poll() is None
    return {"testers": testers, "hub": {"alive": hub_alive}}


@app.post("/reset")
def reset():
    _stop_all()
    _wipe_data()
    for t in _TESTERS.values():
        t["link_profile"] = DEFAULT_PROFILE_NAME
        t["link_params"] = None
    _shapers.reset_profiles()
    _start_all()
    ready = _wait_ready()
    if not all(ready.values()):
        return JSONResponse({"ok": False, "ready": ready}, status_code=503)
    return {"ok": True, "ready": ready}


@app.post("/testers/{tag}/kill")
def kill_tester(tag: str):
    if tag not in _TESTERS:
        return JSONResponse({"ok": False, "error": f"unknown tester {tag}"}, status_code=404)
    _stop(tag)
    return {"ok": True}


@app.post("/testers/{tag}/start")
def start_tester(tag: str):
    if tag not in _TESTERS:
        return JSONResponse({"ok": False, "error": f"unknown tester {tag}"}, status_code=404)
    proc = _processes.get(tag)
    if proc is not None and proc.poll() is None:
        return {"ok": False, "error": "already running"}
    _processes[tag] = _launch(tag)
    return {"ok": True}


@app.post("/testers/{tag}/restart")
def restart_tester(tag: str):
    if tag not in _TESTERS:
        return JSONResponse({"ok": False, "error": f"unknown tester {tag}"}, status_code=404)
    _stop(tag)
    _processes[tag] = _launch(tag)
    return {"ok": True}


@app.post("/testers/{tag}/reset")
def reset_tester(tag: str):
    if tag not in _TESTERS:
        return JSONResponse({"ok": False, "error": f"unknown tester {tag}"}, status_code=404)
    _stop(tag)
    _wipe_tester(tag)
    _processes[tag] = _launch(tag)
    return {"ok": True}


class HeartbeatBody(BaseModel):
    secs: float


@app.post("/testers/{tag}/heartbeat")
def set_heartbeat(tag: str, body: HeartbeatBody):
    """Change how often a tester re-announces, and restart it to apply it.

    Every tester announcing every 10s makes meeting a stranger instantaneous,
    which is the opposite of a real client's 15-minute cadence -- and it is why
    a client being unhearable after startup went unnoticed here. A scenario
    that needs to observe first contact slows one tester down through this.
    """
    if tag not in _TESTERS:
        return JSONResponse({"ok": False, "error": f"unknown tester {tag}"}, status_code=404)
    if body.secs <= 0:
        return JSONResponse({"ok": False, "error": "secs must be positive"},
                            status_code=400)
    _TESTERS[tag]["heartbeat_secs"] = body.secs
    _stop(tag)
    _processes[tag] = _launch(tag)
    return {"ok": True, "heartbeat_secs": body.secs}


class LinkProfileBody(BaseModel):
    profile: str
    bitrate_bps: int | None = None
    latency_ms: float | None = None
    jitter_ms: float | None = None
    loss_pct: float | None = None


@app.post("/testers/{tag}/link_profile")
def set_link_profile(tag: str, body: LinkProfileBody):
    """Retune a tester's simulated link. Shaping applies immediately; the
    matching bitrate hint only reaches RNS when the tester next restarts."""
    if tag not in _TESTERS:
        return JSONResponse({"ok": False, "error": f"unknown tester {tag}"}, status_code=404)

    overrides = body.model_dump(exclude={"profile"}, exclude_none=True)
    try:
        profile = resolve_profile(body.profile, **overrides)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    _TESTERS[tag]["link_profile"] = body.profile
    _TESTERS[tag]["link_params"] = overrides or None
    _shapers.set_profile(tag, profile)
    launched_bitrate = _TESTERS[tag].get("launched_bitrate")
    return {
        "ok": True, "applied": "live", "profile": profile.as_dict(),
        "restart_required_for_bitrate_hint": profile.bitrate_bps != launched_bitrate,
    }


@app.post("/hub/kill")
def kill_hub():
    _stop_hub()
    return {"ok": True}


@app.post("/hub/start")
def start_hub():
    global _hub_process
    if _hub_process is not None and _hub_process.poll() is None:
        return {"ok": False, "error": "already running"}
    _hub_process = _launch_hub()
    return {"ok": True}


@app.post("/hub/restart")
def restart_hub():
    global _hub_process
    _stop_hub()
    _hub_process = _launch_hub()
    return {"ok": True}


def main():
    global _TESTERS, _BIND_HOST, _EXTRA_PAGE_ORIGINS

    parser = argparse.ArgumentParser(description="TrenchChat local N-tester test environment")
    parser.add_argument("--testers", type=int, default=_DEFAULT_TESTERS,
                        help=f"number of testers to launch (default {_DEFAULT_TESTERS}, "
                             f"capped at {_MAX_TESTERS})")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address for the orchestrator and every "
                             "tester API (default 127.0.0.1; pass 0.0.0.0 to "
                             "reach the environment from another host)")
    parser.add_argument("--page-origin", action="append", metavar="ORIGIN",
                        help="extra browser origin the tester APIs accept, e.g. "
                             "http://100.64.0.1:8800 — repeatable. Needed when "
                             "this host is reached on an address it cannot "
                             "discover itself, such as a tailnet IP")
    args = parser.parse_args()
    n = max(1, min(args.testers, _MAX_TESTERS))
    _BIND_HOST = args.host
    _EXTRA_PAGE_ORIGINS = list(args.page_origin or [])
    _TESTERS = _build_testers(n)

    _preflight_ports()
    _wipe_data()
    _start_all()
    print(f"\nTrenchChat test environment starting ({n} testers)...")
    print(f"  Local:   http://127.0.0.1:{_ORCH_PORT}/")
    if _BIND_HOST not in ("127.0.0.1", "localhost"):
        print(f"  Network: http://{_lan_ip()}:{_ORCH_PORT}/")
    print()
    ready = _wait_ready()
    if all(ready.values()):
        print("All testers are up.\n")
    else:
        print(f"WARNING: not all testers came up in time: {ready}\n")

    try:
        uvicorn.run(app, host=_BIND_HOST, port=_ORCH_PORT, log_level="warning")
    finally:
        _stop_all()
        _shapers.stop()


if __name__ == "__main__":
    main()
