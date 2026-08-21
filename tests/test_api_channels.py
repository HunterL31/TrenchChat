"""
The channel- and server-creation endpoints of the HTTP/WS API the Flutter
client talks to.

An address comes from the creator's identity plus the name, so a second
channel (or server) of the same name is refused by the core. What is under
test here is that the endpoints turn that refusal into an answer a client can
show a person, rather than the unhandled 500 it used to be.

Like test_api_theme.py these need no peer -- the backend is stubbed down to
what the create actions touch.
"""

import sys
import warnings
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from trenchchat.core.naming import NameInUseError

_TESTENV_DIR = Path(__file__).resolve().parents[1] / "devtools" / "testenv"
if str(_TESTENV_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTENV_DIR))

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


@pytest.fixture
def backend():
    backend = MagicMock()
    backend.config.display_name = "Tester"
    backend.identity.hash_hex = "a" * 32
    backend.invite_mgr.list_pending_invites.return_value = []
    return backend


@pytest.fixture
def client(backend):
    with TestClient(create_app(backend, token=TOKEN),
                    base_url="http://127.0.0.1:8801") as client:
        yield client


@needs_backend
class TestCreateChannelConflict:
    def test_duplicate_name_answers_409_with_the_reason(self, client, backend):
        backend.channel_mgr.create_channel.side_effect = NameInUseError(
            "you already have a channel named 'general'")

        res = client.post("/channels", headers=AUTH,
                          json={"name": "general", "description": "", "access": "public"})

        assert res.status_code == 409
        assert res.json() == {
            "ok": False, "error": "you already have a channel named 'general'",
        }

    def test_a_fresh_name_still_creates(self, client, backend):
        backend.channel_mgr.create_channel.return_value = "b" * 32

        res = client.post("/channels", headers=AUTH,
                          json={"name": "general", "description": "", "access": "public"})

        assert res.status_code == 200
        assert res.json() == {"hash": "b" * 32}

    def test_duplicate_name_in_a_server_answers_409(self, client, backend):
        backend.storage.has_permission.return_value = True
        backend.channel_mgr.create_channel.side_effect = NameInUseError(
            "you already have a channel named 'general'")

        res = client.post("/servers/deadbeef/channels", headers=AUTH,
                          json={"name": "general", "description": ""})

        assert res.status_code == 409
        assert res.json()["error"] == "you already have a channel named 'general'"


@needs_backend
class TestCreateServerConflict:
    def test_duplicate_name_answers_409_with_the_reason(self, client, backend):
        backend.server_mgr.create_server.side_effect = NameInUseError(
            "you already have a server named 'mesh-crew'")

        res = client.post("/servers", headers=AUTH,
                          json={"name": "mesh-crew", "description": ""})

        assert res.status_code == 409
        assert res.json()["error"] == "you already have a server named 'mesh-crew'"

    def test_a_fresh_name_still_creates(self, client, backend):
        backend.server_mgr.create_server.return_value = "d" * 32

        res = client.post("/servers", headers=AUTH,
                          json={"name": "mesh-crew", "description": ""})

        assert res.status_code == 200
        assert res.json() == {"hash": "d" * 32}
