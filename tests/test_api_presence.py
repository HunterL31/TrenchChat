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
import time
import warnings
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import RNS

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
    # No path to anyone unless a test puts one in the table.
    backend.rns.get_path_table.return_value = []
    return backend


def _delivery_hash(identity_hex: str) -> bytes:
    return RNS.Destination.hash(bytes.fromhex(identity_hex), "lxmf", "delivery")


def _path_entry(identity_hex: str, hops: int, via: bytes | None = None,
                ttl_secs: float = 600.0) -> dict:
    return {
        "hash": _delivery_hash(identity_hex),
        "via": via,
        "hops": hops,
        "expires": time.time() + ttl_secs,
    }


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
        body = res.json()
        peers = body["peers"]
        assert {e["identity_hash"] for e in peers} == {PEER_A, PEER_B}
        for e in peers:
            assert isinstance(e["quality"], int)
            assert isinstance(e["quality_label"], str)
            # Every field the header summary and its popover read, present on
            # every row: null rather than absent when this node has no path.
            for field in ("hops", "via", "rtt_ms", "path_expires_in"):
                assert field in e
            assert e["is_online"] is True
            assert e["last_seen"] == 123.0

    def test_link_quality_summarises_reach_across_the_roster(self, client, backend):
        self._open_join(backend)
        backend.rns.get_path_table.return_value = [_path_entry(PEER_A, hops=1)]

        summary = client.get(f"/channels/{CH}/link_quality", headers=AUTH).json()["summary"]
        # One of the two members has a path; the other is queued-for-retry
        # territory, and the headline says so rather than hiding it behind the
        # one link that happens to be good.
        assert summary["reachable"] == 1
        assert summary["total"] == 2
        assert summary["median_hops"] == 1
        assert summary["best_identity_hash"] == PEER_A
        assert summary["best_hops"] == 1
        assert summary["level_label"] == "Excellent"

    def test_link_quality_sorts_reachable_peers_first(self, client, backend):
        self._open_join(backend)
        backend.rns.get_path_table.return_value = [
            _path_entry(PEER_B, hops=2, via=bytes.fromhex("ab" * 8)),
        ]

        peers = client.get(f"/channels/{CH}/link_quality", headers=AUTH).json()["peers"]
        assert [e["identity_hash"] for e in peers] == [PEER_B, PEER_A]
        assert peers[0]["hops"] == 2
        assert peers[0]["via"] == "ab" * 8
        assert peers[0]["path_expires_in"] > 0
        assert peers[1]["hops"] is None
        assert peers[1]["via"] is None
        assert peers[1]["path_expires_in"] is None

    def test_link_quality_asks_the_mesh_for_nothing(self, client, backend):
        self._open_join(backend)

        # The client re-reads this on every topology change and on a timer, so
        # a path request here would turn a passive indicator into traffic.
        with patch.object(RNS.Transport, "request_path") as request_path:
            assert client.get(f"/channels/{CH}/link_quality",
                              headers=AUTH).status_code == 200
        request_path.assert_not_called()

    def test_link_quality_leaves_the_local_identity_out(self, client, backend):
        backend.storage.get_channel.return_value = {
            "permissions": permissions_to_json(PRESET_OPEN)}
        backend.subscription_mgr.get_subscribers.return_value = {
            PEER_A, backend.identity.hash_hex}

        body = client.get(f"/channels/{CH}/link_quality", headers=AUTH).json()
        # A link to yourself always scores EXCELLENT; including it would pin
        # the meter to full bars whatever the mesh is doing, and count a
        # member this node never has to reach.
        assert {e["identity_hash"] for e in body["peers"]} == {PEER_A}
        assert body["summary"]["total"] == 1


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
