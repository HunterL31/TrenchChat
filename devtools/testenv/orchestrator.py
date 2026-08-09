"""
TrenchChat local two-tester test environment.

Run:
    python devtools/testenv/orchestrator.py

Then visit http://<this-machine's-LAN-ip>:8800/ from any device on the
network (or http://localhost:8800/ locally). You'll get two independent,
fully-isolated TrenchChat identities ("Tester A" and "Tester B") each
running as its own real process with its own real Reticulum instance,
connected to each other over a local TCP interface -- create channels,
invite each other, send messages, exactly as two real installations
would, no manual key exchange required (each pane already knows the
other's identity hash).

Every action the UI takes calls the same trenchchat.core.actions /
manager entry points the Qt GUI calls (see api.py) -- there is no
separate reimplementation to drift out of sync.

Click "Reset environment" to kill both testers, wipe their data
directories back to a fresh-install state, and relaunch them.

Ports (fixed, all on this machine):
    8800  orchestrator (this page + /reset)
    8801  Tester A's API + WebSocket
    8802  Tester B's API + WebSocket
    41001 Tester A's Reticulum TCP listener (Tester B dials this)
"""

import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

_TESTENV_DIR = Path(__file__).resolve().parent
_DATA_DIR = _TESTENV_DIR / "data"
_WORKER = _TESTENV_DIR / "worker.py"

_ORCH_PORT = 8800
_A_API_PORT = 8801
_B_API_PORT = 8802
_LINK_PORT = 41001

_TESTERS = {
    # enable_transport=True on A: its TCPServerInterface is the side a 3rd
    # party client (e.g. the real main.py client, pointed at 127.0.0.1:41001)
    # would plug into, so A needs to relay for that client's traffic to reach
    # B to be routable beyond a direct A<->client link.
    "A": dict(tag="A", data_dir=_DATA_DIR / "testerA", display_name="Tester A",
             role="server", listen_port=_LINK_PORT, peer_host="127.0.0.1",
             peer_port=_LINK_PORT, api_port=_A_API_PORT, instance_name="trenchchat_testenv_a",
             enable_transport=True),
    "B": dict(tag="B", data_dir=_DATA_DIR / "testerB", display_name="Tester B",
             role="client", listen_port=_LINK_PORT, peer_host="127.0.0.1",
             peer_port=_LINK_PORT, api_port=_B_API_PORT, instance_name="trenchchat_testenv_b",
             enable_transport=False),
}

_processes: dict[str, subprocess.Popen] = {}


def _lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _launch(tag: str) -> subprocess.Popen:
    t = _TESTERS[tag]
    t["data_dir"].mkdir(parents=True, exist_ok=True)
    args = [
        sys.executable, str(_WORKER), t["tag"], str(t["data_dir"]), t["display_name"],
        t["role"], str(t["listen_port"]), t["peer_host"], str(t["peer_port"]),
        str(t["api_port"]), t["instance_name"], str(t["enable_transport"]),
    ]
    return subprocess.Popen(args)


def _stop_all() -> None:
    for tag, proc in list(_processes.items()):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        _processes.pop(tag, None)


def _wipe_data() -> None:
    if _DATA_DIR.exists():
        shutil.rmtree(_DATA_DIR, ignore_errors=True)
    _DATA_DIR.mkdir(parents=True, exist_ok=True)


def _start_all() -> None:
    # Server role first so its TCPServerInterface is listening before the
    # client role dials it.
    _processes["A"] = _launch("A")
    time.sleep(1.5)
    _processes["B"] = _launch("B")


def _wait_ready(timeout: float = 30.0) -> dict[str, bool]:
    ready = {"A": False, "B": False}
    deadline = time.time() + timeout
    while time.time() < deadline and not all(ready.values()):
        for tag, t in _TESTERS.items():
            if ready[tag]:
                continue
            try:
                r = httpx.get(f"http://127.0.0.1:{t['api_port']}/me", timeout=1.0)
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
        "a_api_port": _A_API_PORT,
        "b_api_port": _B_API_PORT,
    }


@app.get("/status")
def status():
    alive = {tag: (proc.poll() is None) for tag, proc in _processes.items()}
    return alive


@app.post("/reset")
def reset():
    _stop_all()
    _wipe_data()
    _start_all()
    ready = _wait_ready()
    if not all(ready.values()):
        return JSONResponse({"ok": False, "ready": ready}, status_code=503)
    return {"ok": True, "ready": ready}


def main():
    _wipe_data()
    _start_all()
    ip = _lan_ip()
    print(f"\nTrenchChat test environment starting...")
    print(f"  Local:   http://127.0.0.1:{_ORCH_PORT}/")
    print(f"  Network: http://{ip}:{_ORCH_PORT}/\n")
    ready = _wait_ready()
    if all(ready.values()):
        print("Both testers are up.\n")
    else:
        print(f"WARNING: not all testers came up in time: {ready}\n")

    try:
        uvicorn.run(app, host="0.0.0.0", port=_ORCH_PORT, log_level="warning")
    finally:
        _stop_all()


if __name__ == "__main__":
    main()
