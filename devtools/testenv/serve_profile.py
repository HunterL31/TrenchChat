"""
Serve the machine's real TrenchChat profile over the tester API, plus the
built Flutter web client on the same port, so the client can be tested from
a browser against real channels and the real mesh.

    python devtools/testenv/serve_profile.py
    # then open the printed http://127.0.0.1:8810/?token=... URL

Binds to localhost by default. The API drives the served identity, so a wider
bind puts that identity on the network with only the token in front of it --
pass --host deliberately, not by habit.

Uses ~/.trenchchat and the default Reticulum config. Close the desktop
client first: both processes would announce the same identity and contend
for the same database. PIN-locked profiles are refused -- there is no
headless unlock path yet.
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

# Give peer links a moment to come up before the startup sync.
_STARTUP_SYNC_DELAY_SECS = 3.0


def main():
    parser = argparse.ArgumentParser(
        description="Serve the real TrenchChat profile + web client over HTTP")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default 127.0.0.1; binding wider "
                             "exposes this identity to the whole network)")
    parser.add_argument("--port", type=int, default=_DEFAULT_PORT,
                        help=f"port for the API and web client (default {_DEFAULT_PORT}, "
                             "clear of the dev environment's 8800-8808)")
    parser.add_argument("--rns-configdir", default=None,
                        help="Reticulum config directory (default: the "
                             "machine's usual one, ~/.reticulum)")
    parser.add_argument("--web-dir", default=str(_DEFAULT_WEB_DIR),
                        help="built web client to serve at / (default "
                             "flutter_ui/build/web; skipped if missing)")
    parser.add_argument("--page-origin", action="append", default=[], dest="page_origins",
                        help="extra browser origin allowed to reach this API "
                             "(repeatable); required for any non-localhost address "
                             "the client is served on, e.g. http://100.x.y.z:8899")
    args = parser.parse_args()

    import uvicorn
    from fastapi.staticfiles import StaticFiles
    from starlette.middleware.gzip import GZipMiddleware

    from api import create_app, generate_token
    from backend_core import Backend

    try:
        backend = Backend.for_real_profile(rns_configdir=args.rns_configdir)
    except RuntimeError as e:
        sys.exit(f"error: {e}")

    from trenchchat.network.router import REANNOUNCE_INTERVAL_SECS

    # Same startup sequence as main_flutter.py: announce everything, then
    # keep reannouncing.
    backend.announce()
    backend.start_heartbeat(interval=REANNOUNCE_INTERVAL_SECS)
    backend.start_presence_pruner()
    backend.start_voice_ticker()
    backend.start_bandwidth_sampler()
    threading.Timer(_STARTUP_SYNC_DELAY_SECS, backend.sync_mgr.request_sync_all).start()

    api_token = generate_token()
    app = create_app(backend, token=api_token, allowed_origins=args.page_origins)
    # The Flutter bundle is ~10 MB of JS and wasm. Served raw over a slow or
    # relayed link the browser gives up mid-load, so compress it.
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    web_dir = Path(args.web_dir)
    if web_dir.is_dir():
        # Mounted last, so every API route declared above still wins.
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web-client")
        web_note = (f"web client:  http://127.0.0.1:{args.port}/?token={api_token}")
    else:
        web_note = ("web client:  not found -- run `flutter build web` in "
                    "flutter_ui/ to serve it from this port too")

    print(f"\nServing profile {backend.identity.hash_hex} "
          f"({backend.config.display_name})")
    print(f"  api:         http://127.0.0.1:{args.port}")
    print(f"  {web_note}")
    print(f"  token:       {api_token}")
    print("  NOTE: close the desktop client while this runs -- same identity, "
          "same database.")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"  WARNING: bound to {args.host} -- this identity is reachable "
              "from other hosts; the token is all that protects it.")
    print()

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
