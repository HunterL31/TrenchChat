"""
Access control on the HTTP/WS API the Flutter client talks to.

Unlike the rest of the suite these tests do not need a real peer: what is
under test is the transport in front of the backend, not any protocol
behaviour, so the backend is stubbed out. The endpoints themselves are
covered by the manager-level tests they delegate to.

Every endpoint here acts as the identity it serves -- sends messages as them,
returns their whole transcript -- so an unauthenticated one is a remote
control for that identity, reachable by any local process and, cross-origin,
by any web page the user happens to visit.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_TESTENV_DIR = Path(__file__).resolve().parents[1] / "devtools" / "testenv"
if str(_TESTENV_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTENV_DIR))

# The backend's dependencies are dev-only (devtools/testenv/requirements.txt),
# so the API tests skip on a bare install. The bind-address tests read source
# and need none of them, so they still run.
try:
    from fastapi.testclient import TestClient

    from api import TOKEN_HEADER, create_app, generate_token
    _HAVE_BACKEND_DEPS = True
except ImportError:  # pragma: no cover - depends on the local install
    _HAVE_BACKEND_DEPS = False
    TOKEN_HEADER = "x-tc-token"

needs_backend = pytest.mark.skipif(
    not _HAVE_BACKEND_DEPS,
    reason="install devtools/testenv/requirements.txt to exercise the API",
)

TOKEN = "test-token-not-a-real-one"
OTHER_ORIGIN = "https://evil.example"


def _stub_backend():
    """A backend that satisfies create_app's wiring and nothing more."""
    backend = MagicMock()
    backend.config.display_name = "Tester"
    backend.identity.hash_hex = "a" * 32
    backend.invite_mgr.list_pending_invites.return_value = []
    backend.storage.get_messages.return_value = []
    backend.storage.list_channels.return_value = []
    return backend


@pytest.fixture
def client():
    return TestClient(create_app(_stub_backend(), token=TOKEN))


@needs_backend
class TestTokenRequired:
    def test_request_without_token_is_rejected(self, client):
        res = client.get("/me")
        assert res.status_code == 401

    def test_request_with_wrong_token_is_rejected(self, client):
        res = client.get("/me", headers={TOKEN_HEADER: "wrong"})
        assert res.status_code == 401

    def test_request_with_token_header_is_accepted(self, client):
        res = client.get("/me", headers={TOKEN_HEADER: TOKEN})
        assert res.status_code == 200
        assert res.json()["hash_hex"] == "a" * 32

    def test_bearer_authorization_is_accepted(self, client):
        res = client.get("/me", headers={"Authorization": f"Bearer {TOKEN}"})
        assert res.status_code == 200

    def test_query_parameter_is_accepted(self, client):
        # An <img> src cannot carry a header, so image URLs rely on this.
        res = client.get(f"/me?token={TOKEN}")
        assert res.status_code == 200

    def test_mutating_endpoint_without_token_is_rejected(self, client):
        res = client.post("/me/display_name", json={"display_name": "attacker"})
        assert res.status_code == 401

    def test_generated_tokens_differ(self):
        assert generate_token() != generate_token()


@needs_backend
class TestCors:
    def test_no_wildcard_origin_by_default(self, client):
        res = client.get("/me", headers={TOKEN_HEADER: TOKEN,
                                        "Origin": OTHER_ORIGIN})
        allowed = res.headers.get("access-control-allow-origin")
        assert allowed != "*"
        assert allowed != OTHER_ORIGIN

    def test_configured_origin_is_echoed(self):
        app = create_app(_stub_backend(), token=TOKEN,
                         allowed_origins=["http://127.0.0.1:8800"])
        res = TestClient(app).get(
            "/me", headers={TOKEN_HEADER: TOKEN, "Origin": "http://127.0.0.1:8800"})
        assert res.headers.get("access-control-allow-origin") == "http://127.0.0.1:8800"


@needs_backend
class TestWebsocket:
    def test_socket_without_token_is_refused(self, client):
        with pytest.raises(Exception):
            with client.websocket_connect("/ws"):
                pass

    def test_socket_with_token_is_accepted(self, client):
        with client.websocket_connect(f"/ws?token={TOKEN}") as ws:
            assert ws is not None

    def test_socket_from_foreign_origin_is_refused(self, client):
        # Browsers apply neither CORS nor same-origin policy to a WS
        # handshake, so the Origin check here is the only one there is.
        with pytest.raises(Exception):
            with client.websocket_connect(f"/ws?token={TOKEN}",
                                          headers={"Origin": OTHER_ORIGIN}):
                pass


@needs_backend
class TestStaticClientStaysPublic:
    def test_mounted_assets_need_no_token(self, tmp_path):
        """The web client has to load before it can present a token."""
        from fastapi.staticfiles import StaticFiles

        (tmp_path / "index.html").write_text("<!doctype html>hello")
        app = create_app(_stub_backend(), token=TOKEN)
        app.mount("/", StaticFiles(directory=str(tmp_path), html=True), name="web")

        client = TestClient(app)
        assert client.get("/index.html").status_code == 200
        # ...while the API behind it still does.
        assert client.get("/me").status_code == 401


class TestBindDefaults:
    """The API drives a real identity, so it must not default to 0.0.0.0."""

    def _source(self, relative: str) -> str:
        return (Path(__file__).resolve().parents[1] / relative).read_text()

    def test_serve_profile_binds_localhost_by_default(self):
        source = self._source("devtools/testenv/serve_profile.py")
        assert '"--host", default="127.0.0.1"' in source

    def test_orchestrator_binds_localhost_by_default(self):
        source = self._source("devtools/testenv/orchestrator.py")
        assert '_BIND_HOST = "127.0.0.1"' in source
        assert '"--host", default="127.0.0.1"' in source

    def test_main_flutter_binds_localhost(self):
        assert 'host="127.0.0.1"' in self._source("main_flutter.py")
