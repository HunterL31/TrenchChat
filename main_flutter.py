"""
One-command launcher for the Flutter client.

Starts the headless backend over the machine's real profile (~/.trenchchat,
default Reticulum config), serves the API and
the built web client on one local port, then opens the UI: the platform's
Flutter desktop binary when one has been built, otherwise the web client in
the default browser.

Closing the window does not close the node. It drops to a tray icon so
announces, discovery and sync carry on, and reopens from there; quitting
from the tray is what announces offline and shuts the backend down. A
second launch while that is running hands the request over to it instead of
starting a second node over the same profile.

    .venv/bin/python main_flutter.py            # Linux/macOS
    .venv\\Scripts\\python main_flutter.py       # Windows

Run only one instance at a time: two would announce the same identity and
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
from typing import Callable

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

_LOG_DIR = Path.home() / ".trenchchat"
_LAUNCHER_LOG = _LOG_DIR / "launcher.log"


def ensure_std_streams() -> None:
    """Give the process usable stdout/stderr when the OS handed it none.

    A windowed PyInstaller build has no console, so both are None and
    uvicorn's logging setup dies calling isatty() on stdout. A log file
    keeps what the console would have shown, RNS output included.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        stream = open(_LAUNCHER_LOG, "w", buffering=1, errors="replace")
    except OSError:
        stream = open(os.devnull, "w")
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


ensure_std_streams()


_DEFAULT_PORT = 8810

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


class ClientWindow:
    """The client UI as the launcher sees it: opened, closed, opened again.

    Closing it is not quitting -- the node outlives it and the tray brings
    it back -- so the launcher has to be able to start it more than once in
    a run. A browser tab has no close to notice, so only a desktop window
    reports one.
    """

    def __init__(self, url: str, token: str, desktop_binary: Path | None,
                 on_closed: Callable[[], None]):
        self._url = url
        self._token = token
        self._desktop_binary = desktop_binary
        self._on_closed = on_closed
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    @property
    def browser_url(self) -> str:
        """The web client's address, token included."""
        return f"{self._url}/?token={self._token}"

    def open(self) -> None:
        """Show the client, unless a desktop window is already up."""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return
            if self._desktop_binary is None:
                print(f"opening web client: {self.browser_url}")
                webbrowser.open(self.browser_url)
                return
            print(f"opening desktop client: {self._desktop_binary}")
            proc = subprocess.Popen(
                [str(self._desktop_binary)],
                env=dict(os.environ, TC_API_URL=self._url,
                         TC_API_TOKEN=self._token))
            self._proc = proc
        threading.Thread(target=self._watch, args=(proc,), daemon=True,
                         name="ui-watcher").start()

    def close(self) -> None:
        """Take a desktop window down with the backend."""
        with self._lock:
            proc = self._proc
            self._proc = None
        if proc is not None and proc.poll() is None:
            proc.terminate()

    def _watch(self, proc: subprocess.Popen) -> None:
        proc.wait()
        self._on_closed()


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
    parser.add_argument("--no-tray", action="store_true",
                        help="quit when the client window closes instead of "
                             "staying in the tray")
    parser.add_argument("--version", action="store_true",
                        help="print this build's version and exit")
    args = parser.parse_args()

    if args.version:
        from trenchchat.version import app_version
        print(app_version())
        return

    from trenchchat import single_instance, tray

    if not args.no_ui and single_instance.hand_off():
        print("TrenchChat is already running -- opened its window")
        return

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

    # Announce everything, then keep reannouncing.
    from trenchchat.network.router import REANNOUNCE_INTERVAL_SECS

    backend.announce()
    backend.start_heartbeat(interval=REANNOUNCE_INTERVAL_SECS)
    backend.start_presence_pruner()
    backend.start_voice_ticker()
    backend.start_bandwidth_sampler()
    threading.Timer(_STARTUP_SYNC_DELAY_SECS, backend.sync_mgr.request_sync_all).start()

    api_token = generate_token()
    app = create_app(backend, token=api_token)

    url = f"http://127.0.0.1:{args.port}"
    stop = threading.Event()

    notified = False

    def on_window_closed():
        nonlocal notified
        if stop.is_set():
            return
        if background is None:
            stop.set()
            return
        print("client window closed; TrenchChat is still running in the tray")
        if not notified:
            notified = True
            background.notify(tray.BACKGROUND_NOTICE)

    window = ClientWindow(url, api_token, desktop_binary, on_window_closed)
    background = None if (args.no_ui or args.no_tray) else tray.create_tray(
        on_open=window.open, on_quit=stop.set)

    @app.post(single_instance.OPEN_UI_PATH)
    def open_ui():
        """A second launch handing off, rather than starting a second node."""
        window.open()
        return {"ok": True}

    if _WEB_DIR.is_dir():
        from fastapi.staticfiles import StaticFiles
        # Mounted last, so every API route declared above still wins.
        app.mount("/", StaticFiles(directory=str(_WEB_DIR), html=True), name="web-client")

    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=args.port, log_level="warning"))
    server_thread = threading.Thread(target=server.run, daemon=True, name="api-server")
    server_thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        sys.exit(f"error: backend failed to start on {url}")

    single_instance.publish(url, api_token)
    # RNS installed a SIGINT handler that exits the process on the spot,
    # skipping uvicorn's shutdown hook (offline goodbyes, backend close).
    # Replace it so Ctrl+C flows through the graceful path instead.
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    print(f"TrenchChat backend up as {backend.identity.hash_hex} at {url}")
    if args.no_ui:
        print(f"running headless; open {window.browser_url}")
    else:
        window.open()
    if background is None:
        print("press Ctrl+C to quit")
    else:
        print("closing the window leaves TrenchChat in the tray; quit from there")

    def wait_for_quit():
        while not stop.wait(0.5):
            if not server_thread.is_alive():
                return

    if background is None:
        wait_for_quit()
    else:
        # The tray loop owns the main thread (macOS accepts no other) and
        # ends when the wait does.
        background.run(wait_for_quit)

    single_instance.clear()
    window.close()
    # uvicorn's shutdown hook in api.py announces offline and closes the
    # backend; joining the thread lets the goodbyes drain before exit.
    server.should_exit = True
    server_thread.join(timeout=10)
    print("shut down")


if __name__ == "__main__":
    main()
