"""
The invite endpoints of the HTTP/WS API the Flutter client talks to.

An invite reaches a client one of two ways: a token, which accepting answers
with a join request, or a signed membership document an admin published for a
scope this node holds no anchor for, which accepting confirms locally. The
second kind carries no token, and the endpoints used to assume one -- calling
.hex() on it raised inside the manager's callback guard, so the entry and its
event were both lost and the peer had nothing to accept.

Like test_api_channels.py these need no peer; the backend is stubbed down to
what the invite endpoints touch.
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
SCOPE = "c" * 32


@pytest.fixture
def backend():
    backend = MagicMock()
    backend.config.display_name = "Tester"
    backend.identity.hash_hex = "a" * 32
    backend.invite_mgr.list_pending_invites.return_value = []
    backend.invite_mgr.list_pending_memberships.return_value = []
    backend.invite_mgr.invite_scope_kind.return_value = "channel"
    return backend


def _client(backend):
    return TestClient(create_app(backend, token=TOKEN),
                      base_url="http://127.0.0.1:8801")


@needs_backend
class TestHeldMemberships:
    def test_a_held_document_is_listed_as_an_invite_with_no_token(self, backend):
        backend.invite_mgr.list_pending_memberships.return_value = [
            {"channel_hash": SCOPE, "channel_name": "mesh-crew",
             "admin_hash": "b" * 32},
        ]

        with _client(backend) as client:
            entries = client.get("/invites", headers=AUTH).json()

        assert [e["channel_hash_hex"] for e in entries] == [SCOPE]
        assert entries[0]["token_hex"] is None
        assert entries[0]["channel_name"] == "mesh-crew"

    def test_accepting_one_confirms_it_rather_than_sending_a_join_request(self, backend):
        backend.invite_mgr.list_pending_memberships.return_value = [
            {"channel_hash": SCOPE, "channel_name": "mesh-crew",
             "admin_hash": "b" * 32},
        ]
        backend.invite_mgr.accept_pending_membership.return_value = True

        with _client(backend) as client:
            res = client.post(f"/invites/{SCOPE}/accept", headers=AUTH)

        assert res.status_code == 200
        backend.invite_mgr.accept_pending_membership.assert_called_once_with(SCOPE)
        backend.accept_invite.assert_not_called()

    def test_a_document_that_will_not_verify_answers_409(self, backend):
        backend.invite_mgr.list_pending_memberships.return_value = [
            {"channel_hash": SCOPE, "channel_name": "mesh-crew",
             "admin_hash": "b" * 32},
        ]
        backend.invite_mgr.accept_pending_membership.return_value = False

        with _client(backend) as client:
            res = client.post(f"/invites/{SCOPE}/accept", headers=AUTH)

        assert res.status_code == 409
        assert res.json()["ok"] is False

    def test_declining_one_clears_the_held_document(self, backend):
        backend.invite_mgr.list_pending_memberships.return_value = [
            {"channel_hash": SCOPE, "channel_name": "mesh-crew",
             "admin_hash": "b" * 32},
        ]

        with _client(backend) as client:
            assert client.post(f"/invites/{SCOPE}/decline",
                               headers=AUTH).status_code == 200

        backend.invite_mgr.decline_pending_membership.assert_called_once_with(SCOPE)
        backend.invite_mgr.decline_invite.assert_not_called()


@needs_backend
class TestTokenlessInviteCallback:
    def test_a_null_token_still_raises_the_invite(self, backend):
        with _client(backend) as client:
            on_invite = backend.invite_mgr.add_invite_callback.call_args[0][0]
            on_invite(SCOPE, "mesh-crew", None, 0.0, "b" * 32)

            entries = client.get("/invites", headers=AUTH).json()

        assert [e["channel_hash_hex"] for e in entries] == [SCOPE]
        assert entries[0]["token_hex"] is None


@needs_backend
class TestTokenInvitesAreUnchanged:
    def test_accepting_a_token_invite_sends_a_join_request(self, backend):
        backend.invite_mgr.list_pending_invites.return_value = [
            {"channel_hash_hex": SCOPE, "channel_name": "mesh-crew",
             "token": b"\xab" * 8, "expiry": 4_000_000_000.0,
             "admin_hash_hex": "b" * 32},
        ]

        with _client(backend) as client:
            res = client.post(f"/invites/{SCOPE}/accept", headers=AUTH)

        assert res.status_code == 200
        backend.accept_invite.assert_called_once_with(
            SCOPE, b"\xab" * 8, 4_000_000_000.0, "b" * 32)
        backend.invite_mgr.accept_pending_membership.assert_not_called()

    def test_an_unknown_channel_is_still_a_404(self, backend):
        with _client(backend) as client:
            res = client.post(f"/invites/{SCOPE}/accept", headers=AUTH)

        assert res.status_code == 404
