"""
Headless TrenchChat service entry point.

    python -m trenchchat.service.main [--host HOST] [--port PORT]

Builds a ServiceBackend (production wiring: the real data dir, the user's
real Reticulum config, production presence timeouts, propagation-node sync
on startup) and serves it over the same FastAPI app
devtools/testenv/api.py defines for the dev harness -- reusing the route
definitions rather than re-implementing them, so a bug fixed (or
introduced) there is a bug fixed everywhere this API is served from.

Note: fastapi/uvicorn are declared in devtools/testenv/requirements.txt,
not the top-level requirements.txt -- this module needs them as real
runtime dependencies, which requirements.txt does not yet reflect.
"""

import argparse
import sys
from pathlib import Path

import RNS

from trenchchat.service.backend import ServiceBackend

_TESTENV_DIR = Path(__file__).resolve().parents[2] / "devtools" / "testenv"


def _load_create_app():
    """Import devtools/testenv/api.create_app.

    api.py's own `from backend_core import Backend` is a bare import that
    assumes devtools/testenv is already on sys.path -- mirrors how
    devtools/testenv/worker.py sets this up for the dev harness's own
    entry point.
    """
    if str(_TESTENV_DIR) not in sys.path:
        sys.path.insert(0, str(_TESTENV_DIR))
    from api import create_app
    return create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="TrenchChat headless service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8800)
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable TrenchChat debug logging (RNS stays at NOTICE level)",
    )
    parser.add_argument(
        "--rns-debug", action="store_true",
        help="Enable full Reticulum debug logging (very verbose)",
    )
    args = parser.parse_args()

    if args.rns_debug:
        rns_loglevel = RNS.LOG_DEBUG
    elif args.verbose:
        rns_loglevel = RNS.LOG_INFO
    else:
        rns_loglevel = RNS.LOG_NOTICE

    create_app = _load_create_app()

    backend = ServiceBackend(rns_loglevel=rns_loglevel)
    backend.start()

    import uvicorn
    app = create_app(backend)
    try:
        uvicorn.run(app, host=args.host, port=args.port)
    finally:
        backend.close()


if __name__ == "__main__":
    main()
