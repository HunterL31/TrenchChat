"""
The message delivery-state surface the Flutter client reads (catalogue #35).

A message sent to a dead/unreachable peer used to look identical to a
delivered one. The messages endpoint now carries a per-message delivery_state
for the local user's own outbound messages, and a delivery_status WS event
fires when that state changes.

Like test_api_channels.py these need no peer: the backend is a MagicMock
stubbed down to what the messages endpoint and the delivery callback touch.
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
ME = "a" * 32
OTHER = "b" * 32


def _row(message_id, sender_hash):
    return {
        "message_id": message_id,
        "sender_hash": sender_hash,
        "sender_name": "Someone",
        "content": "hi",
        "timestamp": 1.0,
        "reply_to": None,
        "image_data": None,
        "image_stripped": 0,
    }


@pytest.fixture
def backend():
    backend = MagicMock()
    backend.config.display_name = "Tester"
    backend.identity.hash_hex = ME
    backend.invite_mgr.list_pending_invites.return_value = []
    backend.storage.get_reactions.return_value = []
    return backend


@pytest.fixture
def client(backend):
    with TestClient(create_app(backend, token=TOKEN),
                    base_url="http://127.0.0.1:8801") as client:
        yield client


@needs_backend
class TestMessagesCarryDeliveryState:
    def test_own_message_reports_its_delivery_state(self, client, backend):
        backend.storage.get_messages.return_value = [_row("m1", ME)]
        backend.messaging.get_delivery_state.return_value = "pending"

        res = client.get(f"/channels/{CH}/messages", headers=AUTH)

        assert res.status_code == 200
        body = res.json()
        assert body[0]["delivery_state"] == "pending"
        backend.messaging.get_delivery_state.assert_called_with("m1")

    def test_peer_message_has_no_delivery_state(self, client, backend):
        backend.storage.get_messages.return_value = [_row("m2", OTHER)]

        res = client.get(f"/channels/{CH}/messages", headers=AUTH)

        assert res.json()[0]["delivery_state"] is None
        # Never consulted for someone else's message.
        backend.messaging.get_delivery_state.assert_not_called()


@needs_backend
class TestDeliveryStatusEvent:
    def test_delivery_status_event_is_emitted(self, client, backend):
        # create_app registered the WS-fanout callback on messaging; grab it.
        callback = backend.messaging.add_delivery_status_callback.call_args[0][0]

        # websocket_connect builds the Host header from nothing, not base_url,
        # so it must be set explicitly or the same-origin gate refuses it.
        with client.websocket_connect(
                f"/ws?token={TOKEN}", headers={"Host": "127.0.0.1:8801"}) as ws:
            callback(CH, "m1", "delivered")
            event = ws.receive_json()

        assert event == {
            "type": "delivery_status",
            "channel_hash": CH,
            "message_id": "m1",
            "delivery_state": "delivered",
        }
