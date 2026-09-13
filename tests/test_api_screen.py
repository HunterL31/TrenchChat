"""
The screen share endpoints and the watch socket of the HTTP/WS API.

Like test_api_voice.py these need no peer: the backend is stubbed down to the
config, the voice manager and the screen manager the endpoints touch. The
socket's credit rule is the one thing here that is not a pass-through, so it
gets the closest look: one update, then nothing until the client answers.
"""

import json
import sys
import warnings
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.test_screen_wire import jpeg
from trenchchat.core.permissions import PRESET_OPEN, PRESET_PRIVATE
from trenchchat.network.screen_wire import KIND_FULL, ScreenUpdate, unpack_update

_TESTENV_DIR = Path(__file__).resolve().parents[1] / "devtools" / "testenv"
if str(_TESTENV_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTENV_DIR))

try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect

    import api as api_module
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
# websocket_connect builds the Host header from nothing, not base_url.
WS_HOST = {"host": "127.0.0.1:8801"}
CHANNEL = "ab" * 16
PEER = "cd" * 16


class _FakeConfig:
    """Just the surface the screen actions touch."""

    def __init__(self):
        self.display_name = "Tester"
        self.screen_monitor = 1
        self.screen_preset = "clearer"
        self.screen_fps = 15


def _stub_backend(*, in_voice: bool = True, open_join: bool = True):
    backend = MagicMock()
    backend.config = _FakeConfig()
    backend.identity.hash_hex = "a" * 32
    backend.voice_mgr.current_channel = CHANNEL if in_voice else None
    backend.storage.get_channel.return_value = {
        "permissions": json.dumps(PRESET_OPEN if open_join else PRESET_PRIVATE)}
    backend.storage.has_permission.return_value = False
    screen = backend.screen_mgr
    screen.start_share.return_value = None
    screen.stop_share.return_value = True
    screen.unwatch.return_value = True
    screen.held_shares.return_value = [{"peer": PEER, "channel": CHANNEL}]
    screen.watch.return_value = None
    screen.new_client.return_value = 7
    screen.client_count.return_value = 0
    screen.watching.return_value = {"peer": PEER}
    screen.status.return_value = {
        "available": {"ok": True, "reason": ""}, "sharing": None,
        "watching": None, "shares": [{"peer": PEER, "channel": CHANNEL}]}
    return backend


@pytest.fixture
def backend():
    return _stub_backend()


@pytest.fixture
def client(backend, monkeypatch):
    monkeypatch.setattr(api_module, "resolve_display_name",
                        lambda peer, *_a, **_k: f"name-{peer[:4]}")
    with TestClient(create_app(backend, token=TOKEN),
                    base_url="http://127.0.0.1:8801") as client:
        yield client


def _update(seq: int) -> ScreenUpdate:
    return ScreenUpdate(seq=seq, width=64, height=64, kind=KIND_FULL,
                        entries=[(0, 0, jpeg(64, 64))])


@needs_backend
class TestScreenEndpoints:
    def test_start_passes_the_gate_and_remembers_the_choices(self, client, backend):
        res = client.post("/screen/start", headers=AUTH,
                          json={"monitor": 2, "preset": "smoother", "fps": 20})
        assert res.status_code == 200 and res.json() == {"ok": True, "reason": None}
        backend.screen_mgr.start_share.assert_called_once_with(
            CHANNEL, monitor=2, preset="smoother", fps=20)
        assert (backend.config.screen_monitor, backend.config.screen_preset,
                backend.config.screen_fps) == (2, "smoother", 20)

    def test_start_outside_voice_is_refused_before_the_manager(self, monkeypatch):
        backend = _stub_backend(in_voice=False)
        with TestClient(create_app(backend, token=TOKEN),
                        base_url="http://127.0.0.1:8801") as client:
            res = client.post("/screen/start", headers=AUTH, json={})
        assert res.json() == {"ok": False, "reason": "not_in_voice"}
        backend.screen_mgr.start_share.assert_not_called()

    def test_start_without_the_permission_is_refused_before_the_manager(self):
        backend = _stub_backend(open_join=False)
        with TestClient(create_app(backend, token=TOKEN),
                        base_url="http://127.0.0.1:8801") as client:
            res = client.post("/screen/start", headers=AUTH, json={})
        assert res.json() == {"ok": False, "reason": "no_screen_permission"}
        backend.screen_mgr.start_share.assert_not_called()

    def test_stop_and_unwatch(self, client, backend):
        assert client.post("/screen/stop", headers=AUTH).json() == {"ok": True}
        backend.screen_mgr.stop_share.assert_called_once()
        assert client.post("/screen/unwatch", headers=AUTH).json() == {"ok": True}

    def test_status_names_every_peer(self, client):
        body = client.get("/screen/status", headers=AUTH).json()
        assert body["shares"][0]["display_name"] == "name-cdcd"
        assert body["available"] == {"ok": True, "reason": ""}

    def test_sources_carry_the_defaults_and_thumbnails(self, client, monkeypatch):
        from trenchchat.core.screen import capture
        monkeypatch.setattr(capture, "list_monitors", lambda: {
            "available": True, "reason": "",
            "monitors": [{"index": 1, "width": 1920, "height": 1080,
                          "left": 0, "top": 0}]})
        monkeypatch.setattr(capture, "monitor_thumbnail", lambda index: b"jpg")
        body = client.get("/screen/sources", headers=AUTH).json()
        assert body["monitors"][0]["thumbnail"] == "anBn"
        assert body["selected"] == {"monitor": 1, "preset": "clearer", "fps": 15}

    def test_every_endpoint_requires_the_token(self, client):
        for path in ("/screen/status", "/screen/sources"):
            assert client.get(path).status_code in (401, 403)
        for path in ("/screen/start", "/screen/stop", "/screen/unwatch"):
            assert client.post(path, json={}).status_code in (401, 403)


@needs_backend
class TestWatchSocket:
    def test_one_update_then_nothing_until_the_client_answers(self, client, backend):
        screen = backend.screen_mgr
        screen.next_for_client.side_effect = [_update(1), _update(2), None, None]
        with client.websocket_connect(f"/screen/watch/{PEER}?token={TOKEN}",
                                      headers=WS_HOST) as ws:
            first = unpack_update(ws.receive_bytes())
            assert first.seq == 1
            # Without the client's answer the second update must not come;
            # the stub would hand it out at once if asked.
            assert screen.next_for_client.call_count == 1
            ws.send_text("r")
            second = unpack_update(ws.receive_bytes())
            assert second.seq == 2
        screen.watch.assert_called_once_with(PEER)
        screen.drop_client.assert_called_once_with(7)
        screen.unwatch.assert_called_once()

    def test_a_refused_watch_says_why_and_closes(self, client, backend):
        backend.screen_mgr.watch.return_value = "no_session"
        with client.websocket_connect(f"/screen/watch/{PEER}?token={TOKEN}",
                                      headers=WS_HOST) as ws:
            assert json.loads(ws.receive_text()) == {"ended": "no_session"}
        backend.screen_mgr.new_client.assert_not_called()

    def test_a_share_this_node_was_not_told_of_is_refused_before_the_manager(
            self, client, backend):
        backend.screen_mgr.held_shares.return_value = []
        with client.websocket_connect(f"/screen/watch/{PEER}?token={TOKEN}",
                                      headers=WS_HOST) as ws:
            assert json.loads(ws.receive_text()) == {"ended": "no_share"}
        backend.screen_mgr.watch.assert_not_called()

    def test_a_share_that_ends_tells_the_client(self, client, backend):
        screen = backend.screen_mgr
        screen.next_for_client.return_value = None
        screen.watching.return_value = None
        with client.websocket_connect(f"/screen/watch/{PEER}?token={TOKEN}",
                                      headers=WS_HOST) as ws:
            assert json.loads(ws.receive_text()) == {"ended": "stopped"}

    def test_the_socket_requires_the_token(self, client):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"/screen/watch/{PEER}", headers=WS_HOST):
                pass
