"""
Subprocess entry point for one tester: builds a Backend and serves its
API over uvicorn. Launched by orchestrator.py via multiprocessing.

    python worker.py <tag> <data_dir> <display_name> <role> <listen_port>
                     <peer_host> <peer_port> <api_port> <instance_name>
                     <enable_transport> <link_bitrate> <api_token>
                     <bind_host> <page_origins>
"""

import os
import sys
import os
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
       peer_host: str, peer_port: int, api_port: int, instance_name: str,
       enable_transport: bool = False, link_bitrate: int = 0,
       api_token: str | None = None, bind_host: str = "127.0.0.1",
       page_origins: list[str] | None = None):
    import uvicorn
    from backend_core import Backend
    from api import create_app

    backend = Backend(
        Path(data_dir), display_name, role, listen_port, peer_host, peer_port,
        instance_name, enable_transport=enable_transport, link_bitrate=link_bitrate,
    )
    backend.write_identity_file()
    if os.environ.get("TC_TESTENV_NOMAD_DEMO"):
        # A seeding failure must not take the tester down with it.
        try:
            from nomad_demo import seed_demo_node
            seed_demo_node(backend, display_name)
        except Exception as e:
            import RNS
            RNS.log(f"TesterBackend: nomad demo seed failed: {e}",
                    RNS.LOG_WARNING)
    # A tester announcing every 10s makes first contact instant, which is the
    # opposite of a real client's 15-minute cadence. Scenarios that need to
    # observe meeting a stranger slow one tester down through this.
    backend.start_heartbeat(interval=float(os.environ.get("TC_TESTENV_HEARTBEAT_SECS", 10.0)))
    backend.start_presence_pruner(interval=5.0)
    backend.start_voice_ticker(interval=1.0)
    backend.start_bandwidth_sampler()
    threading.Timer(_STARTUP_SYNC_DELAY_SECS, backend.sync_mgr.request_sync_all).start()

    app = create_app(backend, token=api_token, allowed_origins=page_origins)
    uvicorn.run(app, host=bind_host, port=api_port, log_level="warning")


if __name__ == "__main__":
    (_, tag, data_dir, display_name, role, listen_port, peer_host, peer_port,
     api_port, instance_name, enable_transport, link_bitrate, api_token,
     bind_host, page_origins) = sys.argv
    run(tag, data_dir, display_name, role, int(listen_port), peer_host,
       int(peer_port), int(api_port), instance_name,
       enable_transport=enable_transport.lower() in ("1", "true", "yes"),
       link_bitrate=int(link_bitrate), api_token=api_token,
       bind_host=bind_host,
       page_origins=[o for o in page_origins.split(",") if o])
