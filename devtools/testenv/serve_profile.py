"""
Serve the machine's real TrenchChat profile over the tester API, plus the
built Flutter web client on the same port, so the client can be tested from
a browser against real channels and the real mesh.

    python devtools/testenv/serve_profile.py
    # then open http://127.0.0.1:8810/  (or http://<host>:8810/ remotely)

Uses ~/.trenchchat and the default Reticulum config, wired exactly like
main.py. Close the desktop client first: both processes would announce the
same identity and contend for the same database. PIN-locked profiles are
refused -- there is no headless unlock path yet.
"""

import argparse
import sys
import threading
from pathlib import Path

_TESTENV_DIR = Path(__file__).resolve().parent
if str(_TESTENV_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTENV_DIR))
_REPO_ROOT = _TESTENV_DIR.parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_DEFAULT_WEB_DIR = _REPO_ROOT / "flutter_ui" / "build" / "web"

# Clear of the dev environment's range -- orchestrator on 8800, tester APIs
# on 8801-8808 -- so a real-profile server can run alongside it.
_DEFAULT_PORT = 8810

# Mirrors main.py's minute reannounce and main_window.py's startup sync delay.
_REANNOUNCE_SECS = 60.0
_STARTUP_SYNC_DELAY_SECS = 3.0


def main():
    parser = argparse.ArgumentParser(
        description="Serve the real TrenchChat profile + web client over HTTP")
    parser.add_argument("--host", default="0.0.0.0",
                        help="bind address (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=_DEFAULT_PORT,
                        help=f"port for the API and web client (default {_DEFAULT_PORT}, "
                             "clear of the dev environment's 8800-8808)")
    parser.add_argument("--rns-configdir", default=None,
                        help="Reticulum config directory (default: the "
                             "machine's usual one, ~/.reticulum)")
    parser.add_argument("--web-dir", default=str(_DEFAULT_WEB_DIR),
                        help="built web client to serve at / (default "
                             "flutter_ui/build/web; skipped if missing)")
    args = parser.parse_args()

    import uvicorn
    from fastapi.staticfiles import StaticFiles

    from api import create_app
    from backend_core import Backend

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
    threading.Timer(_STARTUP_SYNC_DELAY_SECS, backend.sync_mgr.request_sync_all).start()

    app = create_app(backend)

    web_dir = Path(args.web_dir)
    if web_dir.is_dir():
        # Mounted last, so every API route declared above still wins.
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web-client")
        web_note = f"web client:  http://127.0.0.1:{args.port}/"
    else:
        web_note = ("web client:  not found -- run `flutter build web` in "
                    "flutter_ui/ to serve it from this port too")

    print(f"\nServing profile {backend.identity.hash_hex} "
          f"({backend.config.display_name})")
    print(f"  api:         http://127.0.0.1:{args.port}")
    print(f"  {web_note}")
    print("  NOTE: close the desktop client while this runs -- same identity, "
          "same database.\n")

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
