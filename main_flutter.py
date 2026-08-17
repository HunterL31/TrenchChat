"""
One-command launcher for the Flutter client, the counterpart of main.py.

Starts the headless backend over the machine's real profile (~/.trenchchat,
default Reticulum config -- the same wiring as main.py), serves the API and
the built web client on one local port, then opens the UI: the platform's
Flutter desktop binary when one has been built, otherwise the web client in
the default browser. When the desktop window closes, the backend announces
offline and shuts down with it.

    .venv/bin/python main_flutter.py            # Linux/macOS
    .venv\\Scripts\\python main_flutter.py       # Windows

Close the Qt client first: both would announce the same identity and
contend for the same database.
"""

import argparse
import os
import platform
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent
_FROZEN = getattr(sys, "frozen", False)
if _FROZEN:
    # PyInstaller bundle: the testenv backend modules are frozen in, the web
    # build is collected by trenchchat.spec, and the desktop client is staged
    # into the bundle by the release workflow.
    _BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", _REPO_ROOT))
    _WEB_DIR = _BUNDLE_ROOT / "flutter_web"
    _DESKTOP_ROOT = _BUNDLE_ROOT / "flutter_client"
else:
    _TESTENV_DIR = _REPO_ROOT / "devtools" / "testenv"
    for p in (str(_TESTENV_DIR), str(_REPO_ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)
    _WEB_DIR = _REPO_ROOT / "flutter_ui" / "build" / "web"
    _DESKTOP_ROOT = None

_DEFAULT_PORT = 8810

# Mirrors main.py's minute reannounce and main_window.py's startup sync delay.
_REANNOUNCE_SECS = 60.0
_STARTUP_SYNC_DELAY_SECS = 3.0

_DESKTOP_BINARIES = {
    "Windows": "build/windows/x64/runner/Release/flutter_ui.exe",
    "Linux": "build/linux/x64/release/bundle/flutter_ui",
    "Darwin": "build/macos/Build/Products/Release/flutter_ui.app/Contents/MacOS/flutter_ui",
}

_FROZEN_DESKTOP_BINARIES = {
    "Windows": "flutter_ui.exe",
    "Linux": "flutter_ui",
    "Darwin": "flutter_ui.app/Contents/MacOS/flutter_ui",
}


def find_desktop_binary() -> Path | None:
    """Path to this platform's built Flutter desktop binary, or None."""
    system = platform.system()
    if _DESKTOP_ROOT is not None:
        rel = _FROZEN_DESKTOP_BINARIES.get(system)
        candidate = _DESKTOP_ROOT / rel if rel else None
    else:
        rel = _DESKTOP_BINARIES.get(system)
        candidate = _REPO_ROOT / "flutter_ui" / rel if rel else None
    return candidate if candidate is not None and candidate.is_file() else None


def main():
    parser = argparse.ArgumentParser(
        description="Launch the TrenchChat backend and Flutter client together")
    parser.add_argument("--port", type=int, default=_DEFAULT_PORT,
                        help=f"local port for the API and web client "
                             f"(default {_DEFAULT_PORT})")
    parser.add_argument("--rns-configdir", default=None,
                        help="Reticulum config directory (default: the "
                             "machine's usual one, ~/.reticulum)")
    parser.add_argument("--browser", action="store_true",
                        help="open the web client in the browser even if a "
                             "desktop binary is built")
    parser.add_argument("--no-ui", action="store_true",
                        help="start only the backend; open the printed URL "
                             "yourself")
    args = parser.parse_args()

    import uvicorn

    from api import create_app, generate_token
    from backend_core import Backend

    desktop_binary = None if args.browser else find_desktop_binary()
    if desktop_binary is None and not _WEB_DIR.is_dir():
        sys.exit(
            "error: no UI to launch -- build one first:\n"
            "  cd flutter_ui && flutter build web    (any platform)\n"
            "  cd flutter_ui && flutter build windows|linux|macos"
        )

    try:
        backend = Backend.for_real_profile(rns_configdir=args.rns_configdir)
    except RuntimeError as e:
        sys.exit(f"error: {e}")

    # Same startup sequence as main.py: announce everything, pull from the
    # propagation node if one is configured, then keep reannouncing.
    backend.announce()
    backend.router.sync_from_propagation_node()
    backend.start_heartbeat(interval=_REANNOUNCE_SECS)
    backend.start_presence_pruner()
    backend.start_voice_ticker()
    threading.Timer(_STARTUP_SYNC_DELAY_SECS, backend.sync_mgr.request_sync_all).start()

    api_token = generate_token()
    app = create_app(backend, token=api_token)
    if _WEB_DIR.is_dir():
        from fastapi.staticfiles import StaticFiles
        # Mounted last, so every API route declared above still wins.
        app.mount("/", StaticFiles(directory=str(_WEB_DIR), html=True), name="web-client")

    url = f"http://127.0.0.1:{args.port}"
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=args.port, log_level="warning"))
    server_thread = threading.Thread(target=server.run, daemon=True, name="api-server")
    server_thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        sys.exit(f"error: backend failed to start on {url}")

    # RNS installed a SIGINT handler that exits the process on the spot,
    # skipping uvicorn's shutdown hook (offline goodbyes, backend close).
    # Replace it so Ctrl+C flows through the graceful path instead.
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    client_url = f"{url}/?token={api_token}"
    print(f"TrenchChat backend up as {backend.identity.hash_hex} at {url}")
    if args.no_ui:
        print(f"running headless; open {client_url}")
        print("press Ctrl+C to quit")
    elif desktop_binary is not None:
        print(f"opening desktop client: {desktop_binary}")
        proc = subprocess.Popen(
            [str(desktop_binary)],
            env=dict(os.environ, TC_API_URL=url, TC_API_TOKEN=api_token))
        threading.Thread(target=lambda: (proc.wait(), stop.set()),
                         daemon=True, name="ui-watcher").start()
    else:
        print(f"opening web client: {client_url}")
        webbrowser.open(client_url)
        print("press Ctrl+C to quit")

    while not stop.wait(0.5):
        if not server_thread.is_alive():
            break

    # uvicorn's shutdown hook in api.py announces offline and closes the
    # backend; joining the thread lets the goodbyes drain before exit.
    server.should_exit = True
    server_thread.join(timeout=10)
    print("shut down")


if __name__ == "__main__":
    main()
