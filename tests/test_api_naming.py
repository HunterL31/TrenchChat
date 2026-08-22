"""
The naming/presence propagation surface of the HTTP/WS API (BUG 26, BUG 43).

Covers:
  - POST /me/display_name re-announces both destinations so peers relearn the
    name, driven through actions.set_display_name.
  - the directory_updated WS event fired when a peer's display name changes.
  - the avatar_updated WS event carrying the version a client needs to bust
    its avatar cache.

Like test_api_theme.py these need no peer -- the backend is a MagicMock stubbed
down to what the endpoints and callbacks touch.
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
WS_HOST = {"Host": "127.0.0.1:8801"}

SELF_HEX = "a" * 32
PEER = "b" * 32


@pytest.fixture
def backend():
    backend = MagicMock()
    backend.config.display_name = "Tester"
    backend.config.avatar_version = 0
    backend.identity.hash_hex = SELF_HEX
    backend.invite_mgr.list_pending_invites.return_value = []
    return backend


@pytest.fixture
def client(backend):
    # The context manager runs the app's lifespan, which binds the event bus
    # to a loop -- without it every emit is a no-op.
    with TestClient(create_app(backend, token=TOKEN),
                    base_url="http://127.0.0.1:8801") as client:
        yield client


def _registered_callback(mock_add):
    """The callback last handed to an add_*_callback mock."""
    assert mock_add.call_args is not None, "callback was never registered"
    return mock_add.call_args[0][0]


@needs_backend
class TestDisplayName:
    def test_display_name_reannounces_both_destinations(self, client, backend):
        res = client.post("/me/display_name", headers=AUTH,
                          json={"display_name": "Zephyr"})
        assert res.status_code == 200
        assert res.json() == {"ok": True}
        backend.router.set_display_name.assert_called_once_with("Zephyr")
        backend.router.announce.assert_called_once()
        backend.router.announce_user.assert_called_once()


@needs_backend
class TestDirectoryEvent:
    def test_directory_change_publishes_event(self, client, backend):
        cb = _registered_callback(backend.user_directory.add_directory_callback)
        with client.websocket_connect(f"/ws?token={TOKEN}", headers=WS_HOST) as ws:
            cb(PEER, "New Name")
            event = ws.receive_json()
        assert event == {
            "type": "directory_updated",
            "identity_hash": PEER,
            "display_name": "New Name",
        }


@needs_backend
class TestAvatarEvent:
    def test_peer_avatar_event_carries_version(self, client, backend):
        backend.storage.get_peer_avatar.return_value = {
            "avatar_data": b"jpeg", "avatar_version": 7}
        cb = _registered_callback(backend.avatar_mgr.add_avatar_callback)
        with client.websocket_connect(f"/ws?token={TOKEN}", headers=WS_HOST) as ws:
            cb(PEER)
            event = ws.receive_json()
        assert event == {
            "type": "avatar_updated",
            "identity_hash": PEER,
            "avatar_version": 7,
        }

    def test_removed_peer_avatar_event_has_null_version(self, client, backend):
        backend.storage.get_peer_avatar.return_value = None
        cb = _registered_callback(backend.avatar_mgr.add_avatar_callback)
        with client.websocket_connect(f"/ws?token={TOKEN}", headers=WS_HOST) as ws:
            cb(PEER)
            event = ws.receive_json()
        assert event == {
            "type": "avatar_updated",
            "identity_hash": PEER,
            "avatar_version": None,
        }

    def test_own_avatar_event_uses_config_version(self, client, backend):
        backend.config.avatar_version = 3
        cb = _registered_callback(backend.avatar_mgr.add_avatar_callback)
        with client.websocket_connect(f"/ws?token={TOKEN}", headers=WS_HOST) as ws:
            cb(SELF_HEX)
            event = ws.receive_json()
        assert event == {
            "type": "avatar_updated",
            "identity_hash": SELF_HEX,
            "avatar_version": 3,
        }
        # The peer-avatar table is never consulted for our own change.
        backend.storage.get_peer_avatar.assert_not_called()
