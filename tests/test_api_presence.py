"""
The per-channel presence and link-quality endpoints the Flutter client reads.

Open-join channels keep no members table, so a roster derived from members
alone reads ONLINE-0 / UNKNOWN forever. These endpoints source the roster from
the channel's member list
ones -- the contract the Dart client codes against.

Like test_api_channels.py these need no peer: the backend is a MagicMock stubbed
down to what channel_roster_hexes and the endpoints touch.
"""

import sys
import warnings
from pathlib import Path
from unittest.mock import MagicMock

import pytest


_TESTENV_DIR = Path(__file__).resolve().parents[1] / "devtools" / "testenv"
if str(_TESTENV_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTENV_DIR))

try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient

    from api import TOKEN_HEADER, create_app
    _HAVE_BACKEND_DEPS = True
except ImportError:  # pragma: no cover - depends on the local install
    _HAVE_BACKEND_DEPS = False
    TOKEN_HEADER = "x-tc-token"

needs_backend = pytest.mark.skipif(
    not _HAVE_BACKEND_DEPS,
    reason="install devtools/testenv/requirements.txt to exercise the API",
)

TOKEN = "test-token-not-a-real-one"
AUTH = {TOKEN_HEADER: TOKEN}

CH = "cc" * 16
PEER_A = "1" * 32
PEER_B = "2" * 32


@pytest.fixture
def backend():
    backend = MagicMock()
    backend.config.display_name = "Tester"
    backend.identity.hash_hex = "a" * 32
    backend.invite_mgr.list_pending_invites.return_value = []
    # resolve_display_name falls back to a hash prefix when this is empty,
    # keeping the field a plain string rather than a MagicMock.
    backend.storage.get_display_name_for_identity.return_value = ""
    backend.presence_mgr.is_online.return_value = True
    backend.presence_mgr.last_seen_at.return_value = 123.0
    return backend


@pytest.fixture
def client(backend):
    with TestClient(create_app(backend, token=TOKEN),
                    base_url="http://127.0.0.1:8801") as client:
        yield client

