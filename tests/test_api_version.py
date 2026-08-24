"""
The /version endpoint the Flutter client reads.

The backend records the transition once at startup; the endpoint just has to
hand the client the same verdict, including the downgrade case that is the
whole reason the record exists.
"""

import sys
import warnings
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_TESTENV_DIR = Path(__file__).resolve().parents[1] / "devtools" / "testenv"
if str(_TESTENV_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTENV_DIR))

from trenchchat.version import (
    CHANGE_DOWNGRADE,
    CHANGE_FIRST_RUN,
    InstallState,
    record_launch,
)

try:
    with warnings.catch_warnings():
        # Same httpx fallback warning test_api_security.py suppresses.
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


def _client(state: InstallState):
    backend = MagicMock()
    backend.version_state = state
    backend.identity.hash_hex = "a" * 32
    backend.invite_mgr.list_pending_invites.return_value = []
    backend.storage.get_messages.return_value = []
    backend.storage.list_channels.return_value = []
    return TestClient(create_app(backend, token=TOKEN),
                      base_url="http://127.0.0.1:8801")


@needs_backend
class TestVersionEndpoint:
    def test_reports_a_downgrade(self, tmp_path):
        record_launch(tmp_path, version="1.5.0")
        state = record_launch(tmp_path, version="1.4.0")

        with _client(state) as client:
            body = client.get("/version", headers=AUTH).json()

        assert body["version"] == "1.4.0"
        assert body["previous"] == "1.5.0"
        assert body["transition"] == CHANGE_DOWNGRADE
        assert body["history"][-1]["transition"] == CHANGE_DOWNGRADE

    def test_reports_a_first_run(self, tmp_path):
        state = record_launch(tmp_path, version="1.4.0")

        with _client(state) as client:
            body = client.get("/version", headers=AUTH).json()

        assert body == {
            "version": "1.4.0",
            "previous": None,
            "transition": CHANGE_FIRST_RUN,
            "first_seen": state.first_seen,
            "changed_at": state.changed_at,
            "history": state.history,
        }

    def test_needs_the_token(self, tmp_path):
        state = record_launch(tmp_path, version="1.4.0")

        with _client(state) as client:
            assert client.get("/version").status_code == 401
