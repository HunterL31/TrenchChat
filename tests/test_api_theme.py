"""
The theme endpoints of the HTTP/WS API the Flutter client talks to.

Theme changes are the one kind of state a client mutates that never arrives
from the mesh, so nothing else would tell a second client of the same profile
about them: what is under test here is the event the endpoints publish, plus
the delete route that can carry a name a URL path cannot.

Like test_api_security.py these need no peer -- the backend is stubbed down to
the config the theme actions read and write.
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
# websocket_connect builds the Host header itself and ignores base_url, so the
# socket names the host the backend serves or the handshake is refused on that
# alone.
WS_HOST = {"Host": "127.0.0.1:8801"}


class _FakeConfig:
    """Just the theme surface trenchchat.core.actions reads and writes."""

    def __init__(self):
        self.display_name = "Tester"
        self.ui_theme: dict = {}
        self.ui_theme_library: dict = {}

    def save_ui_theme(self, name: str, theme: dict) -> None:
        self.ui_theme_library[name] = theme

    def delete_ui_theme(self, name: str) -> bool:
        return self.ui_theme_library.pop(name, None) is not None


def _stub_backend():
    backend = MagicMock()
    backend.config = _FakeConfig()
    backend.identity.hash_hex = "a" * 32
    backend.invite_mgr.list_pending_invites.return_value = []
    backend.storage.get_messages.return_value = []
    backend.storage.list_channels.return_value = []
    return backend


@pytest.fixture
def backend():
    return _stub_backend()


@pytest.fixture
def client(backend):
    # The context manager runs the app's lifespan, which is what binds the
    # event bus to a loop -- without it every emit is a no-op.
    with TestClient(create_app(backend, token=TOKEN),
                    base_url="http://127.0.0.1:8801") as client:
        yield client


@needs_backend
class TestThemeEvents:
    def test_setting_the_theme_publishes_it(self, client):
        theme = {"base": {"bgApp": "#101010"}}
        with client.websocket_connect(f"/ws?token={TOKEN}", headers=WS_HOST) as ws:
            res = client.post("/ui_theme", headers=AUTH, json={"theme": theme})
            assert res.status_code == 200
            event = ws.receive_json()

        assert event == {"type": "ui_theme", "theme": theme}

    def test_saving_to_the_library_publishes_the_whole_library(self, client):
        theme = {"base": {"bgApp": "#101010"}}
        with client.websocket_connect(f"/ws?token={TOKEN}", headers=WS_HOST) as ws:
            res = client.post("/ui_theme_library", headers=AUTH,
                              json={"name": "midnight", "theme": theme})
            assert res.status_code == 200
            event = ws.receive_json()

        assert event == {"type": "ui_theme_library", "themes": {"midnight": theme}}

    def test_deleting_publishes_the_library_without_it(self, client):
        client.post("/ui_theme_library", headers=AUTH,
                    json={"name": "midnight", "theme": {}})
        with client.websocket_connect(f"/ws?token={TOKEN}", headers=WS_HOST) as ws:
            res = client.post("/ui_theme_library/delete", headers=AUTH,
                              json={"name": "midnight"})
            assert res.status_code == 200
            event = ws.receive_json()

        assert event == {"type": "ui_theme_library", "themes": {}}


@needs_backend
class TestThemeDelete:
    def test_post_delete_removes_the_theme(self, client, backend):
        client.post("/ui_theme_library", headers=AUTH,
                    json={"name": "midnight", "theme": {"accent": "#111111"}})

        res = client.post("/ui_theme_library/delete", headers=AUTH,
                          json={"name": "midnight"})

        assert res.status_code == 200
        assert res.json() == {"ok": True}
        assert backend.config.ui_theme_library == {}

    def test_post_delete_removes_a_name_holding_a_slash(self, client, backend):
        # The path route cannot address this name at all: the router splits on
        # the slash whether or not the client encoded it.
        client.post("/ui_theme_library", headers=AUTH,
                    json={"name": "a/b", "theme": {}})

        res = client.post("/ui_theme_library/delete", headers=AUTH,
                          json={"name": "a/b"})

        assert res.status_code == 200
        assert backend.config.ui_theme_library == {}

    def test_post_delete_of_an_unknown_name_is_a_404(self, client):
        res = client.post("/ui_theme_library/delete", headers=AUTH,
                          json={"name": "nothing"})

        assert res.status_code == 404
        assert res.json()["ok"] is False

    def test_the_path_route_still_deletes(self, client, backend):
        client.post("/ui_theme_library", headers=AUTH,
                    json={"name": "midnight", "theme": {}})

        res = client.delete("/ui_theme_library/midnight", headers=AUTH)

        assert res.status_code == 200
        assert backend.config.ui_theme_library == {}

    def test_post_delete_needs_the_token(self, client, backend):
        client.post("/ui_theme_library", headers=AUTH,
                    json={"name": "midnight", "theme": {}})

        res = client.post("/ui_theme_library/delete", json={"name": "midnight"})

        assert res.status_code == 401
        assert list(backend.config.ui_theme_library) == ["midnight"]
