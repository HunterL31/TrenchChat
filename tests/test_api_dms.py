"""
The direct-message and friend-request endpoints of the HTTP/WS API.

What is under test here is the endpoint surface -- that each one delegates to
the action it should, and that a refusal from the core reaches the client as
an answer it can show a person rather than a silent success. The gate itself
is covered where it is enforced (tests/test_direct_messages.py and
tests/test_adversarial.py); the backend is stubbed down to what these touch.
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
PEER = "bb" * 16
CONVERSATION = "cc" * 8


@pytest.fixture
def backend():
    backend = MagicMock()
    backend.config.display_name = "Tester"
    backend.identity.hash_hex = "a" * 32
    backend.invite_mgr.list_pending_invites.return_value = []
    backend.direct_mgr.conversation_hash.return_value = CONVERSATION
    return backend


@pytest.fixture
def client(backend):
    return TestClient(create_app(backend, token=TOKEN),
                      base_url="http://127.0.0.1:8801")


@needs_backend
class TestDirectMessageEndpoints:
    def test_open_conversation_returns_its_address(self, client, backend):
        backend.direct_mgr.open_conversation.return_value = CONVERSATION
        res = client.post(f"/dms/{PEER}", headers=AUTH)
        assert res.status_code == 200
        assert res.json()["hash"] == CONVERSATION

    def test_open_conversation_with_a_non_friend_is_refused(self, client, backend):
        backend.direct_mgr.open_conversation.return_value = None
        res = client.post(f"/dms/{PEER}", headers=AUTH)
        assert res.status_code == 403
        assert res.json()["ok"] is False

    def test_send_reports_the_stored_message(self, client, backend):
        backend.direct_mgr.may_dm.return_value = True
        backend.messaging.send_direct.return_value = "msg-1"
        res = client.post(f"/dms/{PEER}/messages", json={"content": "hello"},
                          headers=AUTH)
        assert res.status_code == 200
        assert res.json() == {"ok": True, "hash": CONVERSATION, "message_id": "msg-1"}

    def test_send_to_a_non_friend_is_refused(self, client, backend):
        backend.direct_mgr.may_dm.return_value = False
        res = client.post(f"/dms/{PEER}/messages", json={"content": "hello"},
                          headers=AUTH)
        assert res.status_code == 403
        backend.messaging.send_direct.assert_not_called()

    def test_a_malformed_attachment_is_rejected(self, client, backend):
        backend.direct_mgr.may_dm.return_value = True
        res = client.post(f"/dms/{PEER}/messages",
                          json={"content": "x", "image_data_b64": "not base64!"},
                          headers=AUTH)
        assert res.status_code == 400
        backend.messaging.send_direct.assert_not_called()

    def test_listing_conversations_delegates_to_the_manager(self, client, backend):
        backend.direct_mgr.conversations.return_value = [{"hash": CONVERSATION}]
        res = client.get("/dms", headers=AUTH)
        assert res.json() == [{"hash": CONVERSATION}]

    def test_marking_read_and_deleting_report_whether_they_applied(
            self, client, backend):
        backend.direct_mgr.mark_read.return_value = True
        backend.direct_mgr.delete_conversation.return_value = False
        assert client.post(f"/dms/{CONVERSATION}/read", headers=AUTH).json()["ok"]
        assert client.delete(f"/dms/{CONVERSATION}", headers=AUTH).json()["ok"] is False


@needs_backend
class TestFriendRequestEndpoints:
    def test_sending_a_request_delegates_to_the_manager(self, client, backend):
        backend.friends_mgr.send_friend_request.return_value = True
        res = client.post("/friends/requests",
                          json={"identity_hash": PEER, "note": "from the ridge"},
                          headers=AUTH)
        assert res.status_code == 200
        backend.friends_mgr.send_friend_request.assert_called_once_with(
            PEER, note="from the ridge", nickname="")

    def test_a_malformed_hash_is_rejected(self, client, backend):
        backend.friends_mgr.send_friend_request.return_value = False
        res = client.post("/friends/requests", json={"identity_hash": "nope"},
                          headers=AUTH)
        assert res.status_code == 400

    def test_accept_and_decline_report_whether_a_request_existed(
            self, client, backend):
        backend.friends_mgr.accept_friend_request.return_value = True
        backend.friends_mgr.decline_friend_request.return_value = False
        assert client.post(f"/friends/requests/{PEER}/accept", json={},
                           headers=AUTH).json()["ok"] is True
        assert client.post(f"/friends/requests/{PEER}/decline",
                           headers=AUTH).json()["ok"] is False

    def test_pending_requests_are_listed(self, client, backend):
        backend.friends_mgr.get_pending_requests.return_value = {
            "incoming": [{"identity_hash": PEER}], "outgoing": [],
        }
        res = client.get("/friends/requests", headers=AUTH)
        assert res.json()["incoming"][0]["identity_hash"] == PEER

    def test_an_incoming_request_carries_any_words_the_peer_sent(
            self, client, backend):
        """A client with no handshake asks by messaging, so the same queue has
        to describe both kinds."""
        backend.friends_mgr.get_pending_requests.return_value = {
            "incoming": [{
                "identity_hash": PEER, "message": "is this thing on",
                "message_count": 2, "from_trenchchat": False,
            }],
            "outgoing": [],
        }
        entry = client.get("/friends/requests", headers=AUTH).json()["incoming"][0]
        assert entry["message"] == "is this thing on"
        assert entry["message_count"] == 2
        assert entry["from_trenchchat"] is False


@needs_backend
class TestPropagationEndpoints:
    def test_status_reports_the_selected_node(self, client, backend):
        backend.propagation_nodes.selected = "dd" * 8
        backend.propagation_nodes.pinned = ""
        backend.propagation_nodes.known_nodes.return_value = []
        backend.router.propagation_sync_state.return_value = 0
        res = client.get("/propagation", headers=AUTH)
        assert res.json()["selected"] == "dd" * 8

    def test_a_malformed_node_hash_is_rejected(self, client, backend):
        backend.propagation_nodes.pin.return_value = False
        res = client.post("/propagation/node", json={"node_hash": "zz"}, headers=AUTH)
        assert res.status_code == 400

    def test_collecting_without_a_node_is_a_conflict(self, client, backend):
        backend.collect_propagated.return_value = False
        res = client.post("/propagation/sync", headers=AUTH)
        assert res.status_code == 409


@needs_backend
class TestTokenRequired:
    @pytest.mark.parametrize("method,path", [
        ("get", "/dms"),
        ("post", f"/dms/{PEER}"),
        ("post", f"/dms/{PEER}/messages"),
        ("post", f"/dms/{CONVERSATION}/read"),
        ("delete", f"/dms/{CONVERSATION}"),
        ("get", "/friends/requests"),
        ("post", "/friends/requests"),
        ("post", f"/friends/requests/{PEER}/accept"),
        ("post", f"/friends/requests/{PEER}/decline"),
        ("delete", f"/friends/requests/{PEER}"),
        ("get", "/propagation"),
        ("post", "/propagation/node"),
        ("post", "/propagation/sync"),
    ])
    def test_every_new_endpoint_needs_the_token(self, client, method, path):
        kwargs = {"json": {}} if method == "post" else {}
        res = getattr(client, method)(path, **kwargs)
        assert res.status_code == 401
