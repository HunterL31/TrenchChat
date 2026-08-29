"""
The voice device endpoints of the HTTP/WS API — the surface the Flutter
settings picker codes against. Like test_api_theme.py these need no peer:
the backend is stubbed down to the config and voice manager the device
actions touch.
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


class _FakeConfig:
    """Just the voice-device surface trenchchat.core.actions touches."""

    def __init__(self):
        self.display_name = "Tester"
        self.voice_input_device = None
        self.voice_output_device = None


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
    with TestClient(create_app(backend, token=TOKEN),
                    base_url="http://127.0.0.1:8801") as client:
        yield client


@pytest.fixture
def fake_devices(monkeypatch):
    from trenchchat.core.audio import devices
    monkeypatch.setattr(devices, "list_devices", lambda: {
        "available": True, "reason": "",
        "input": ["Built-in Mic", "USB Headset"],
        "output": ["Built-in Speakers", "USB Headset"],
    })


@needs_backend
class TestVoiceDevices:
    def test_get_lists_devices_and_selection(self, client, fake_devices):
        res = client.get("/voice/devices", headers=AUTH)

        assert res.status_code == 200
        body = res.json()
        assert body["input"] == ["Built-in Mic", "USB Headset"]
        assert body["output"] == ["Built-in Speakers", "USB Headset"]
        assert body["selected"] == {"input": None, "output": None}

    def test_post_persists_and_rebuilds_the_pipeline(self, client, backend,
                                                     fake_devices):
        res = client.post("/voice/devices", headers=AUTH, json={
            "input_device": "USB Headset",
            "output_device": None,
        })

        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        assert body["devices"]["selected"] == {
            "input": "USB Headset", "output": None,
        }
        assert backend.config.voice_input_device == "USB Headset"
        backend.voice_mgr.restart_audio.assert_called_once()

    def test_post_same_selection_does_not_rebuild(self, client, backend,
                                                  fake_devices):
        client.post("/voice/devices", headers=AUTH,
                    json={"input_device": "USB Headset"})
        backend.voice_mgr.restart_audio.reset_mock()

        client.post("/voice/devices", headers=AUTH,
                    json={"input_device": "USB Headset"})

        backend.voice_mgr.restart_audio.assert_not_called()

    def test_devices_require_the_token(self, client):
        assert client.get("/voice/devices").status_code in (401, 403)
