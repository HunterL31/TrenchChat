"""
Subprocess entry point for one tester: builds a Backend and serves its
API over uvicorn. Launched by orchestrator.py via multiprocessing.

    python worker.py <tag> <data_dir> <display_name> <role> <listen_port>
                     <peer_host> <peer_port> <api_port> <instance_name>
"""

import sys
import threading
from pathlib import Path

# Matches main_window.py's _STARTUP_SYNC_DELAY_MS -- give the peer link a
# moment to come up before requesting sync, same as the real GUI does via
# QTimer.singleShot(_STARTUP_SYNC_DELAY_MS, self._sync_mgr.request_sync_all).
_STARTUP_SYNC_DELAY_SECS = 3.0

_TESTENV_DIR = Path(__file__).resolve().parent
if str(_TESTENV_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTENV_DIR))
_REPO_ROOT = _TESTENV_DIR.parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def run(tag: str, data_dir: str, display_name: str, role: str, listen_port: int,
       peer_host: str, peer_port: int, api_port: int, instance_name: str):
    import uvicorn
    from backend_core import Backend
    from api import create_app

    backend = Backend(
        Path(data_dir), display_name, role, listen_port, peer_host, peer_port,
        instance_name,
    )
    backend.write_identity_file()
    backend.start_heartbeat(interval=15.0)
    threading.Timer(_STARTUP_SYNC_DELAY_SECS, backend.sync_mgr.request_sync_all).start()

    app = create_app(backend)
    uvicorn.run(app, host="0.0.0.0", port=api_port, log_level="warning")


if __name__ == "__main__":
    _, tag, data_dir, display_name, role, listen_port, peer_host, peer_port, api_port, instance_name = sys.argv
    run(tag, data_dir, display_name, role, int(listen_port), peer_host,
       int(peer_port), int(api_port), instance_name)
