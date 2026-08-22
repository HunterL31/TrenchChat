"""
The per-channel presence and link-quality endpoints the Flutter client reads.

Open-join channels keep no members table, so a roster derived from members
alone reads ONLINE-0 / UNKNOWN forever. These endpoints source the roster from
the subscriber list for open-join channels, and from members for invite-only
ones -- the contract the Dart client codes against.

Like test_api_channels.py these need no peer: the backend is a MagicMock stubbed
down to what channel_roster_hexes and the endpoints touch.
"""

import sys
import warnings
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from trenchchat.core.permissions import (
    PRESET_OPEN, PRESET_PRIVATE, permissions_to_json,
)

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
PEER_A = "1" * 32
PEER_B = "2" * 32


@pytest.fixture
def backend():
    backend = MagicMock()
    backend.config.display_name = "Tester"
    backend.identity.hash_hex = "a" * 32
    backend.invite_mgr.list_pending_invites.return_value = []
    # resolve_display_name falls back to a hash prefix when this is empty,
    # keeping the field a plain string rather than a MagicMock.
    backend.storage.get_display_name_for_identity.return_value = ""
    backend.presence_mgr.is_online.return_value = True
    backend.presence_mgr.last_seen_at.return_value = 123.0
    return backend


@pytest.fixture
def client(backend):
    with TestClient(create_app(backend, token=TOKEN),
                    base_url="http://127.0.0.1:8801") as client:
        yield client


@needs_backend
class TestOpenJoinPresence:
    def _open_join(self, backend):
        backend.storage.get_channel.return_value = {
            "permissions": permissions_to_json(PRESET_OPEN)}
        backend.subscription_mgr.get_subscribers.return_value = {PEER_A, PEER_B}

    def test_presence_sources_the_roster_from_subscribers(self, client, backend):
        self._open_join(backend)

        res = client.get(f"/channels/{CH}/presence", headers=AUTH)
        assert res.status_code == 200
        entries = res.json()
        assert {e["identity_hash"] for e in entries} == {PEER_A, PEER_B}
        for e in entries:
            assert e["is_online"] is True
            assert e["last_seen"] == 123.0
            assert isinstance(e["display_name"], str)
        # Members are never consulted for an open-join channel.
        backend.storage.get_members.assert_not_called()

    def test_link_quality_sources_the_roster_from_subscribers(self, client, backend):
        self._open_join(backend)

        res = client.get(f"/channels/{CH}/link_quality", headers=AUTH)
        assert res.status_code == 200
        entries = res.json()
        assert {e["identity_hash"] for e in entries} == {PEER_A, PEER_B}
        for e in entries:
            assert isinstance(e["quality"], int)
            assert isinstance(e["quality_label"], str)
            # The client scores the roster and shows the winning peer's hop
            # count, so every entry carries one (null when there is no path).
            assert "hops" in e

    def test_link_quality_leaves_the_local_identity_out(self, client, backend):
        backend.storage.get_channel.return_value = {
            "permissions": permissions_to_json(PRESET_OPEN)}
        backend.subscription_mgr.get_subscribers.return_value = {
            PEER_A, backend.identity.hash_hex}

        res = client.get(f"/channels/{CH}/link_quality", headers=AUTH)
        assert res.status_code == 200
        # A link to yourself always scores EXCELLENT; including it would pin
        # the client's meter to full bars whatever the mesh is doing.
        assert {e["identity_hash"] for e in res.json()} == {PEER_A}


@needs_backend
class TestInviteOnlyUnchanged:
    def test_presence_still_uses_members_for_invite_only(self, client, backend):
        backend.storage.get_channel.return_value = {
            "permissions": permissions_to_json(PRESET_PRIVATE)}
        backend.storage.get_members.return_value = [
            {"identity_hash": PEER_A}, {"identity_hash": PEER_B}]

        res = client.get(f"/channels/{CH}/presence", headers=AUTH)
        assert res.status_code == 200
        assert {e["identity_hash"] for e in res.json()} == {PEER_A, PEER_B}
        backend.subscription_mgr.get_subscribers.assert_not_called()
