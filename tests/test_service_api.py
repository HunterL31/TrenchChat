"""
Integration tests for devtools/testenv/api.py -- the FastAPI service surface
new UI clients (including the Flutter spike) talk to.

Uses real managers via tests/conftest.py's peer_factory, wrapped in a small
Backend look-alike that adds the extra managers create_app() touches which
TestPeer doesn't build (presence, avatar, reaction, directory). No mocks:
TestClient drives the exact app object a real client would hit.
"""

import sys
import time
import warnings
from pathlib import Path
from unittest.mock import patch

# starlette.testclient warns (UserWarning, not DeprecationWarning) that httpx
# support is deprecated; pytest.ini's filterwarnings=error would otherwise
# turn that into a collection error.
warnings.filterwarnings("ignore", message=r"Using `httpx` with `starlette\.testclient`.*")

import pytest
from fastapi.testclient import TestClient

from trenchchat.core import actions
from trenchchat.core.avatar import AvatarManager
from trenchchat.core.permissions import (
    PRESET_OPEN, PRESET_PRIVATE, ROLE_ADMIN, ROLE_MEMBER, ROLE_OWNER,
)
from trenchchat.core.presence import PresenceManager
from trenchchat.core.reaction import ReactionManager
from trenchchat.core.user_directory import UserDirectory

_TESTENV_DIR = Path(__file__).resolve().parents[1] / "devtools" / "testenv"
if str(_TESTENV_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTENV_DIR))

from api import create_app, DEFAULT_MESSAGE_PAGE_SIZE  # noqa: E402


class _ApiBackend:
    """Backend look-alike wrapping a TestPeer's real managers, plus the
    extra managers create_app() touches that TestPeer doesn't build."""

    def __init__(self, peer, rns):
        self.config = peer.config
        self.identity = peer.identity
        self.storage = peer.storage
        self.router = peer.router
        self.channel_mgr = peer.channel_mgr
        self.server_mgr = peer.server_mgr
        self.messaging = peer.messaging
        self.subscription_mgr = peer.subscription_mgr
        self.invite_mgr = peer.invite_mgr
        self.sync_mgr = peer.sync_mgr

        self.presence_mgr = PresenceManager(self.identity.hash_hex, self.config)
        self.avatar_mgr = AvatarManager(self.identity, self.config, self.storage, self.router)
        self.reaction_mgr = ReactionManager(self.identity, self.storage, self.router)
        self.user_directory = UserDirectory(self.identity.hash_hex)

        self.rns = rns
        # Never written by these tests; load_interfaces_config() handles a
        # missing file by returning {}, which is all /reticulum/interfaces needs.
        self.rns_config_path = str(peer.data_dir / "reticulum" / "config")

        self._link_callbacks: list = []

    def add_link_callback(self, cb) -> None:
        self._link_callbacks.append(cb)

    def link_interface(self):
        return None

    def link_online(self) -> bool:
        return False

    def go_offline(self) -> bool:
        return False

    def go_online(self) -> bool:
        return False


@pytest.fixture
def client_factory(peer_factory, rns_instance):
    """Returns make(name) -> (TestClient, _ApiBackend) for a fresh peer."""
    def make(name: str) -> tuple[TestClient, _ApiBackend]:
        peer = peer_factory(name)
        backend = _ApiBackend(peer, rns_instance)
        # api.py's @app.on_event("startup") is pre-existing FastAPI-deprecated
        # API, unrelated to this change; not something this task should fix.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            app = create_app(backend)
        return TestClient(app), backend
    return make


# ---------------------------------------------------------------------------
# GET /channels/{h}/presence
# ---------------------------------------------------------------------------

class TestPresenceEndpoint:
    def test_invite_only_channel_lists_all_members(self, client_factory):
        client, backend = client_factory("alice")
        ch_hash = actions.create_channel(
            backend.channel_mgr, backend.invite_mgr,
            name="crew", description="", permissions=dict(PRESET_PRIVATE),
        )
        backend.storage.upsert_member(
            channel_hash=ch_hash, identity_hash="bb" * 16,
            display_name="Bob", role=ROLE_MEMBER,
        )

        resp = client.get(f"/channels/{ch_hash}/presence")
        assert resp.status_code == 200
        roster = resp.json()
        identity_hashes = {r["identity_hash"] for r in roster}
        assert identity_hashes == {backend.identity.hash_hex, "bb" * 16}
        for entry in roster:
            assert set(entry.keys()) == {"identity_hash", "display_name", "is_online"}

    def test_unknown_channel_returns_empty(self, client_factory):
        client, _ = client_factory("alice")
        resp = client.get(f"/channels/{'aa' * 16}/presence")
        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# GET /channels/{h}/link_quality
# ---------------------------------------------------------------------------

class TestLinkQualityEndpoint:
    def test_no_other_recipients_is_unknown(self, client_factory):
        client, backend = client_factory("alice")
        ch_hash = actions.create_channel(
            backend.channel_mgr, backend.invite_mgr,
            name="solo", description="", permissions=dict(PRESET_PRIVATE),
        )
        resp = client.get(f"/channels/{ch_hash}/link_quality")
        assert resp.status_code == 200
        assert resp.json() == {"quality": "unknown", "hops": 0}

    def test_quality_is_serialised_as_lowercase_name(self, client_factory):
        # An open-join channel with no subscribers falls back to [self] as
        # its only recipient (see actions.compute_channel_recipients), which
        # score_channel excludes -- same UNKNOWN/0 result, different code path.
        client, backend = client_factory("alice")
        ch_hash = actions.create_channel(
            backend.channel_mgr, backend.invite_mgr,
            name="public-room", description="", permissions=dict(PRESET_OPEN),
        )
        resp = client.get(f"/channels/{ch_hash}/link_quality")
        assert resp.status_code == 200
        body = resp.json()
        assert body["quality"] in ("excellent", "good", "fair", "poor", "unknown")
        assert isinstance(body["hops"], int)


# ---------------------------------------------------------------------------
# received_at on messages
# ---------------------------------------------------------------------------

class TestReceivedAt:
    def test_message_dict_includes_received_at(self, client_factory):
        client, backend = client_factory("alice")
        ch_hash = actions.create_channel(
            backend.channel_mgr, backend.invite_mgr,
            name="general", description="", permissions=dict(PRESET_OPEN),
        )
        now = time.time()
        backend.storage.insert_message(
            channel_hash=ch_hash, sender_hash=backend.identity.hash_hex,
            sender_name="Alice", content="hi", timestamp=now, message_id="m0",
            reply_to=None, last_seen_id=None, received_at=now + 5.0,
        )
        resp = client.get(f"/channels/{ch_hash}/messages")
        assert resp.status_code == 200
        msgs = resp.json()
        assert len(msgs) == 1
        assert msgs[0]["received_at"] == pytest.approx(now + 5.0)


# ---------------------------------------------------------------------------
# send_message on GET /channels/{h}/my_permissions
# ---------------------------------------------------------------------------

class TestMyPermissionsSendMessage:
    def test_owner_can_send(self, client_factory):
        client, backend = client_factory("alice")
        ch_hash = actions.create_channel(
            backend.channel_mgr, backend.invite_mgr,
            name="crew", description="", permissions=dict(PRESET_PRIVATE),
        )
        resp = client.get(f"/channels/{ch_hash}/my_permissions")
        assert resp.status_code == 200
        assert resp.json()["send_message"] is True

    def test_member_without_send_message_sees_false(self, client_factory):
        client, backend = client_factory("bob")
        ch_hash_hex = ("cc" * 16)

        # Seed Bob's own local view of the channel: a member row with no
        # SEND_MESSAGE grant. Mirrors what a real member-list document would
        # leave in storage after an admin restricts the member role.
        backend.storage.upsert_channel(
            hash=ch_hash_hex, name="crew", description="",
            creator_hash="aa" * 16, permissions=dict(PRESET_PRIVATE),
            created_at=time.time(),
        )
        backend.storage.upsert_member(
            channel_hash=ch_hash_hex, identity_hash=backend.identity.hash_hex,
            display_name="Bob", role=ROLE_MEMBER,
        )
        restricted = dict(PRESET_PRIVATE)
        restricted[ROLE_MEMBER] = []
        backend.storage.set_channel_permissions(ch_hash_hex, restricted)

        resp = client.get(f"/channels/{ch_hash_hex}/my_permissions")
        assert resp.status_code == 200
        assert resp.json()["send_message"] is False

    def test_core_enforcement_still_rejects_the_send_regardless_of_the_gate(
        self, client_factory
    ):
        # The GUI-gate field above is convenience only. Prove the real
        # boundary -- actions.send_message's SEND_MESSAGE check inside
        # compute_send_recipients -- still rejects the send even though
        # nothing in this endpoint enforces anything.
        client, backend = client_factory("bob")
        ch_hash_hex = "cc" * 16
        backend.storage.upsert_channel(
            hash=ch_hash_hex, name="crew", description="",
            creator_hash="aa" * 16, permissions=dict(PRESET_PRIVATE),
            created_at=time.time(),
        )
        backend.storage.upsert_member(
            channel_hash=ch_hash_hex, identity_hash=backend.identity.hash_hex,
            display_name="Bob", role=ROLE_MEMBER,
        )
        restricted = dict(PRESET_PRIVATE)
        restricted[ROLE_MEMBER] = []
        backend.storage.set_channel_permissions(ch_hash_hex, restricted)

        resp = client.post(f"/channels/{ch_hash_hex}/messages", json={"content": "hi"})
        assert resp.status_code == 200
        assert resp.json() == {"ok": False}
        assert backend.storage.get_messages(ch_hash_hex) == []


# ---------------------------------------------------------------------------
# ?limit=&before= pagination on GET /channels/{h}/messages
# ---------------------------------------------------------------------------

class TestMessagePagination:
    def _seed_messages(self, backend, ch_hash: str, count: int, base_ts: float):
        for i in range(count):
            backend.storage.insert_message(
                channel_hash=ch_hash, sender_hash=backend.identity.hash_hex,
                sender_name="Alice", content=f"msg-{i}", timestamp=base_ts + i,
                message_id=f"m{i}", reply_to=None, last_seen_id=None,
                received_at=base_ts + i,
            )

    def test_default_page_size_bounds_a_large_history(self, client_factory):
        client, backend = client_factory("alice")
        ch_hash = actions.create_channel(
            backend.channel_mgr, backend.invite_mgr,
            name="general", description="", permissions=dict(PRESET_OPEN),
        )
        total = DEFAULT_MESSAGE_PAGE_SIZE + 20
        self._seed_messages(backend, ch_hash, total, base_ts=1_000_000.0)

        resp = client.get(f"/channels/{ch_hash}/messages")
        assert resp.status_code == 200
        msgs = resp.json()
        assert len(msgs) == DEFAULT_MESSAGE_PAGE_SIZE
        # Newest-anchored: the tail of history, not the head.
        assert msgs[-1]["message_id"] == f"m{total - 1}"
        assert msgs[0]["message_id"] == f"m{total - DEFAULT_MESSAGE_PAGE_SIZE}"

    def test_limit_bounds_the_page(self, client_factory):
        client, backend = client_factory("alice")
        ch_hash = actions.create_channel(
            backend.channel_mgr, backend.invite_mgr,
            name="general", description="", permissions=dict(PRESET_OPEN),
        )
        self._seed_messages(backend, ch_hash, 5, base_ts=1_000_000.0)

        resp = client.get(f"/channels/{ch_hash}/messages", params={"limit": 2})
        assert resp.status_code == 200
        msgs = resp.json()
        assert [m["message_id"] for m in msgs] == ["m3", "m4"]

    def test_before_pages_further_back(self, client_factory):
        client, backend = client_factory("alice")
        ch_hash = actions.create_channel(
            backend.channel_mgr, backend.invite_mgr,
            name="general", description="", permissions=dict(PRESET_OPEN),
        )
        base_ts = 1_000_000.0
        self._seed_messages(backend, ch_hash, 5, base_ts=base_ts)

        first_page = client.get(
            f"/channels/{ch_hash}/messages", params={"limit": 2}
        ).json()
        oldest_loaded_ts = first_page[0]["timestamp"]

        second_page = client.get(
            f"/channels/{ch_hash}/messages",
            params={"limit": 2, "before": oldest_loaded_ts},
        ).json()
        assert [m["message_id"] for m in second_page] == ["m1", "m2"]
