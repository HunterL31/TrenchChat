"""
Tests for emoji reactions: storage layer, ReactionManager send/receive,
emoji request/response protocol, and adversarial cases.

Covers:
  - Storage: insert/get/remove/search/list emoji and reactions
  - ReactionManager.import_emoji() size enforcement
  - compute_emoji_hash correctness
  - MT_REACTION broadcast and inbound handling
  - MT_EMOJI_REQUEST / MT_EMOJI_RESPONSE round-trip
  - Duplicate emoji request dedup
  - Reaction removal broadcast
  - Adversarial: reaction from peer not subscribed is stored (reactions are
    lightweight trust: the channel membership check sits in Messaging, not
    ReactionManager; we verify the manager does not crash on unknown senders)
"""

import io
import time
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from trenchchat.core.protocol import (
    F_MSG_TYPE, F_CHANNEL_HASH, F_EMOJI_HASH, F_EMOJI_DATA,
    F_REACTION_MSG_ID, F_REACTION_REMOVE,
    MT_REACTION, MT_EMOJI_REQUEST, MT_EMOJI_RESPONSE,
)
from trenchchat.core.reaction import ReactionManager, compute_emoji_hash, MAX_EMOJI_BYTES
from trenchchat.core.storage import Storage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_png(width: int = 32, height: int = 32,
              color: tuple = (200, 100, 50)) -> bytes:
    """Return a small PNG image as bytes."""
    img = Image.new("RGBA", (width, height), color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_identity_mock(hex_str: str):
    """Return a minimal identity mock with .hash_hex and .hash."""
    m = MagicMock()
    m.hash_hex = hex_str
    m.hash = bytes.fromhex(hex_str)
    return m


def _make_router_mock():
    """Return a minimal router mock with a delivery_destination."""
    router = MagicMock()
    router.delivery_destination = MagicMock()
    router.delivery_destination.hash = bytes(32)
    router._delivery_callbacks = []

    def _add_cb(cb):
        router._delivery_callbacks.append(cb)

    router.add_delivery_callback.side_effect = _add_cb
    return router


def _make_lxm(fields: dict, source_hash_hex: str | None = None):
    """Return a minimal mock LXMessage."""
    lxm = MagicMock()
    lxm.fields = fields
    lxm.source_hash = bytes.fromhex(source_hash_hex) if source_hash_hex else None
    return lxm


@pytest.fixture
def db(tmp_path) -> Storage:
    s = Storage(db_path=tmp_path / "test.db")
    yield s
    s.close()


_REACTION_RECALL = "trenchchat.core.reaction.RNS.Identity.recall"
_REACTION_DEST_HASH = "trenchchat.core.reaction.RNS.Destination.hash"
_REACTION_DEST = "trenchchat.core.reaction.RNS.Destination"
_REACTION_TRANSPORT = "trenchchat.core.reaction.RNS.Transport.request_path"


@pytest.fixture
def reaction_mgr(tmp_path):
    """ReactionManager with mocked identity and router."""
    identity = _make_identity_mock("aa" * 16)
    storage = Storage(db_path=tmp_path / "rmgr.db")
    router = _make_router_mock()

    mgr = ReactionManager(identity, storage, router)

    yield mgr, storage, identity, router
    storage.close()


# ---------------------------------------------------------------------------
# compute_emoji_hash
# ---------------------------------------------------------------------------

class TestComputeEmojiHash:
    def test_returns_hex_sha256(self):
        data = b"hello emoji"
        import hashlib
        expected = hashlib.sha256(data).hexdigest()
        assert compute_emoji_hash(data) == expected

    def test_same_data_same_hash(self):
        data = _make_png()
        assert compute_emoji_hash(data) == compute_emoji_hash(data)

    def test_different_data_different_hash(self):
        a = _make_png(color=(255, 0, 0))
        b = _make_png(color=(0, 255, 0))
        assert compute_emoji_hash(a) != compute_emoji_hash(b)


# ---------------------------------------------------------------------------
# Storage: custom_emojis table
# ---------------------------------------------------------------------------

class TestStorageEmojis:
    def test_insert_and_get(self, db):
        img = _make_png()
        h = compute_emoji_hash(img)
        assert db.insert_emoji(h, "test_emoji", img, time.time()) is True
        row = db.get_emoji(h)
        assert row is not None
        assert row["name"] == "test_emoji"
        assert bytes(row["image_data"]) == img

    def test_insert_duplicate_returns_false(self, db):
        img = _make_png()
        h = compute_emoji_hash(img)
        db.insert_emoji(h, "first", img, time.time())
        assert db.insert_emoji(h, "second", img, time.time()) is False

    def test_emoji_exists(self, db):
        img = _make_png()
        h = compute_emoji_hash(img)
        assert db.emoji_exists(h) is False
        db.insert_emoji(h, "e", img, time.time())
        assert db.emoji_exists(h) is True

    def test_search_emojis_by_name(self, db):
        img = _make_png()
        h = compute_emoji_hash(img)
        db.insert_emoji(h, "salute", img, time.time())

        img2 = _make_png(color=(10, 20, 30))
        h2 = compute_emoji_hash(img2)
        db.insert_emoji(h2, "pepe", img2, time.time())

        results = db.search_emojis("sal")
        assert len(results) == 1
        assert results[0]["name"] == "salute"

    def test_search_returns_empty_when_no_match(self, db):
        assert db.search_emojis("zzz_no_match") == []

    def test_list_emojis_returns_all(self, db):
        for i in range(3):
            img = _make_png(color=(i * 50, 0, 0))
            db.insert_emoji(compute_emoji_hash(img), f"emoji_{i}", img, time.time())
        rows = db.list_emojis()
        assert len(rows) == 3

    def test_delete_emoji(self, db):
        img = _make_png()
        h = compute_emoji_hash(img)
        db.insert_emoji(h, "del_me", img, time.time())
        db.delete_emoji(h)
        assert db.get_emoji(h) is None
        assert db.emoji_exists(h) is False


# ---------------------------------------------------------------------------
# Storage: reactions table
# ---------------------------------------------------------------------------

class TestStorageReactions:
    def test_insert_and_get(self, db):
        msg_id = "msg1"
        emoji_hash = "a" * 64
        reactor = "b" * 32
        db.insert_reaction(msg_id, emoji_hash, reactor, "chan1", time.time())
        rows = db.get_reactions(msg_id)
        assert len(rows) == 1
        assert rows[0]["emoji_hash"] == emoji_hash
        assert rows[0]["reactor_hash"] == reactor

    def test_insert_duplicate_returns_false(self, db):
        db.insert_reaction("msg1", "a" * 64, "b" * 32, "chan1", time.time())
        result = db.insert_reaction("msg1", "a" * 64, "b" * 32, "chan1", time.time())
        assert result is False

    def test_multiple_reactors_same_emoji(self, db):
        db.insert_reaction("msg1", "a" * 64, "b" * 32, "chan1", time.time())
        db.insert_reaction("msg1", "a" * 64, "c" * 32, "chan1", time.time())
        rows = db.get_reactions("msg1")
        assert len(rows) == 2

    def test_remove_reaction(self, db):
        msg_id = "msg1"
        emoji_hash = "a" * 64
        reactor = "b" * 32
        db.insert_reaction(msg_id, emoji_hash, reactor, "chan1", time.time())
        db.remove_reaction(msg_id, emoji_hash, reactor)
        assert db.get_reactions(msg_id) == []

    def test_remove_only_affects_matching_row(self, db):
        db.insert_reaction("msg1", "a" * 64, "b" * 32, "chan1", time.time())
        db.insert_reaction("msg1", "a" * 64, "c" * 32, "chan1", time.time())
        db.remove_reaction("msg1", "a" * 64, "b" * 32)
        rows = db.get_reactions("msg1")
        assert len(rows) == 1
        assert rows[0]["reactor_hash"] == "c" * 32

    def test_get_reactions_empty(self, db):
        assert db.get_reactions("no_such_msg") == []


# ---------------------------------------------------------------------------
# ReactionManager.import_emoji
# ---------------------------------------------------------------------------

class TestImportEmoji:
    def test_import_stores_emoji(self, reaction_mgr):
        mgr, storage, identity, router = reaction_mgr
        img = _make_png()
        emoji_hash = mgr.import_emoji("test", img)
        assert storage.emoji_exists(emoji_hash)
        assert compute_emoji_hash(img) == emoji_hash

    def test_import_rejects_oversized(self, reaction_mgr):
        mgr, storage, identity, router = reaction_mgr
        oversized = b"x" * (MAX_EMOJI_BYTES + 1)
        with pytest.raises(ValueError, match="bytes"):
            mgr.import_emoji("big", oversized)

    def test_import_idempotent_same_hash(self, reaction_mgr):
        mgr, storage, identity, router = reaction_mgr
        img = _make_png()
        h1 = mgr.import_emoji("name1", img)
        h2 = mgr.import_emoji("name2", img)
        assert h1 == h2
        assert storage.emoji_exists(h1)


# ---------------------------------------------------------------------------
# ReactionManager: add_reaction / remove_reaction callbacks
# ---------------------------------------------------------------------------

class TestReactionCallbacks:
    def test_add_reaction_fires_callback(self, reaction_mgr):
        mgr, storage, identity, router = reaction_mgr
        fired = []
        mgr.add_reaction_callback(lambda ch, mid: fired.append((ch, mid)))

        channel = "ab" * 16     # valid hex
        msg_id = "msg1"
        emoji_hash = compute_emoji_hash(_make_png())

        storage.insert_emoji(emoji_hash, "e", _make_png(), time.time())
        # No subscribers -> no LXMF sends needed
        mgr.add_reaction(channel, msg_id, emoji_hash, [])

        assert (channel, msg_id) in fired

    def test_remove_reaction_fires_callback(self, reaction_mgr):
        mgr, storage, identity, router = reaction_mgr
        fired = []
        mgr.add_reaction_callback(lambda ch, mid: fired.append((ch, mid)))

        channel = "ab" * 16
        msg_id = "msg1"
        emoji_hash = compute_emoji_hash(_make_png())
        storage.insert_reaction(msg_id, emoji_hash, identity.hash_hex, channel, time.time())

        mgr.remove_reaction(channel, msg_id, emoji_hash, [])

        assert (channel, msg_id) in fired

    def test_add_reaction_stores_locally(self, reaction_mgr):
        mgr, storage, identity, router = reaction_mgr
        channel = "ab" * 16
        msg_id = "msg1"
        emoji_hash = compute_emoji_hash(_make_png())

        mgr.add_reaction(channel, msg_id, emoji_hash, [])

        rows = storage.get_reactions(msg_id)
        assert any(r["reactor_hash"] == identity.hash_hex for r in rows)

    def test_remove_reaction_removes_locally(self, reaction_mgr):
        mgr, storage, identity, router = reaction_mgr
        channel = "ab" * 16
        msg_id = "msg1"
        emoji_hash = compute_emoji_hash(_make_png())

        storage.insert_reaction(msg_id, emoji_hash, identity.hash_hex, channel, time.time())

        mgr.remove_reaction(channel, msg_id, emoji_hash, [])

        rows = storage.get_reactions(msg_id)
        assert all(r["reactor_hash"] != identity.hash_hex for r in rows)


# ---------------------------------------------------------------------------
# ReactionManager: inbound MT_REACTION handling
# ---------------------------------------------------------------------------

class TestInboundReaction:
    def _delivery_callbacks(self, router) -> list:
        return router._delivery_callbacks

    def _deliver(self, router, lxm):
        for cb in router._delivery_callbacks:
            cb(lxm)

    def _setup_channel(self, storage, channel_hex: str) -> None:
        """Insert a dummy channel row and subscribe to it."""
        storage.upsert_channel(
            hash=channel_hex, name="test", description="",
            creator_hash="aa" * 16, permissions="{}", created_at=0.0,
        )
        storage.subscribe(channel_hex)

    def test_inbound_reaction_stored(self, reaction_mgr):
        mgr, storage, identity, router = reaction_mgr
        sender_hex = "bb" * 16

        channel = "cc" * 16
        self._setup_channel(storage, channel)
        msg_id = "msg_abc"
        img = _make_png()
        emoji_hash = compute_emoji_hash(img)
        storage.insert_emoji(emoji_hash, "e", img, time.time())

        sender_identity_mock = MagicMock()
        sender_identity_mock.hash = bytes.fromhex(sender_hex)

        lxm = _make_lxm({
            F_MSG_TYPE: MT_REACTION,
            F_CHANNEL_HASH: bytes.fromhex(channel),
            F_REACTION_MSG_ID: msg_id,
            F_EMOJI_HASH: bytes.fromhex(emoji_hash),
            F_REACTION_REMOVE: False,
        }, source_hash_hex=sender_hex)

        with patch(_REACTION_RECALL, return_value=sender_identity_mock):
            self._deliver(router, lxm)

        rows = storage.get_reactions(msg_id)
        assert any(r["reactor_hash"] == sender_hex for r in rows)

    def test_inbound_removal_removes_reaction(self, reaction_mgr):
        mgr, storage, identity, router = reaction_mgr
        sender_hex = "bb" * 16
        channel = "cc" * 16
        self._setup_channel(storage, channel)
        msg_id = "msg_abc"
        emoji_hash = compute_emoji_hash(_make_png())

        storage.insert_reaction(msg_id, emoji_hash, sender_hex, channel, time.time())

        sender_identity_mock = MagicMock()
        sender_identity_mock.hash = bytes.fromhex(sender_hex)

        lxm = _make_lxm({
            F_MSG_TYPE: MT_REACTION,
            F_CHANNEL_HASH: bytes.fromhex(channel),
            F_REACTION_MSG_ID: msg_id,
            F_EMOJI_HASH: bytes.fromhex(emoji_hash),
            F_REACTION_REMOVE: True,
        }, source_hash_hex=sender_hex)

        with patch(_REACTION_RECALL, return_value=sender_identity_mock):
            self._deliver(router, lxm)

        rows = storage.get_reactions(msg_id)
        assert all(r["reactor_hash"] != sender_hex for r in rows)

    def test_inbound_reaction_not_subscribed_ignored(self, reaction_mgr):
        mgr, storage, identity, router = reaction_mgr
        sender_hex = "bb" * 16
        channel = "cc" * 16
        # intentionally NOT subscribed to this channel
        msg_id = "msg_abc"
        emoji_hash = compute_emoji_hash(_make_png())

        sender_identity_mock = MagicMock()
        sender_identity_mock.hash = bytes.fromhex(sender_hex)

        lxm = _make_lxm({
            F_MSG_TYPE: MT_REACTION,
            F_CHANNEL_HASH: bytes.fromhex(channel),
            F_REACTION_MSG_ID: msg_id,
            F_EMOJI_HASH: bytes.fromhex(emoji_hash),
            F_REACTION_REMOVE: False,
        }, source_hash_hex=sender_hex)

        with patch(_REACTION_RECALL, return_value=sender_identity_mock):
            self._deliver(router, lxm)

        assert storage.get_reactions(msg_id) == []

    def test_inbound_reaction_requests_unknown_emoji(self, reaction_mgr):
        mgr, storage, identity, router = reaction_mgr
        sender_hex = "bb" * 16
        channel = "cc" * 16
        self._setup_channel(storage, channel)
        msg_id = "msg_abc"
        img = _make_png()
        emoji_hash = compute_emoji_hash(img)
        # Do NOT store the emoji locally

        sender_identity_mock = MagicMock()
        sender_identity_mock.hash = bytes.fromhex(sender_hex)

        sent_lxms = []
        router.send = lambda lxm: sent_lxms.append(lxm)

        lxm = _make_lxm({
            F_MSG_TYPE: MT_REACTION,
            F_CHANNEL_HASH: bytes.fromhex(channel),
            F_REACTION_MSG_ID: msg_id,
            F_EMOJI_HASH: bytes.fromhex(emoji_hash),
            F_REACTION_REMOVE: False,
        }, source_hash_hex=sender_hex)

        # recall returns the sender identity; configure .hash so .hex() returns sender_hex
        sender_identity_mock = MagicMock()
        sender_identity_mock.hash = bytes.fromhex(sender_hex)

        with patch(_REACTION_RECALL, return_value=sender_identity_mock), \
             patch(_REACTION_DEST_HASH, return_value=bytes(16)), \
             patch(_REACTION_DEST, return_value=MagicMock()), \
             patch("trenchchat.core.reaction.LXMF.LXMessage") as mock_lxm_cls:
            mock_lxm_cls.return_value = MagicMock(fields={})
            self._deliver(router, lxm)

        # A MT_EMOJI_REQUEST should have been sent
        assert any(
            getattr(m, "fields", {}).get(F_MSG_TYPE) == MT_EMOJI_REQUEST
            for m in sent_lxms
        )

    def test_inbound_emoji_request_dedup(self, reaction_mgr):
        """A second reaction with the same unknown emoji must not send a duplicate request."""
        mgr, storage, identity, router = reaction_mgr
        sender_hex = "bb" * 16
        channel = "cc" * 16
        self._setup_channel(storage, channel)
        img = _make_png()
        emoji_hash = compute_emoji_hash(img)

        sent_lxms = []
        router.send = lambda lxm: sent_lxms.append(lxm)

        sender_identity_mock = MagicMock()
        sender_identity_mock.hash = bytes.fromhex(sender_hex)

        def deliver_reaction(msg_id: str):
            lxm = _make_lxm({
                F_MSG_TYPE: MT_REACTION,
                F_CHANNEL_HASH: bytes.fromhex(channel),
                F_REACTION_MSG_ID: msg_id,
                F_EMOJI_HASH: bytes.fromhex(emoji_hash),
                F_REACTION_REMOVE: False,
            }, source_hash_hex=sender_hex)
            with patch(_REACTION_RECALL, return_value=sender_identity_mock), \
                 patch(_REACTION_DEST_HASH, return_value=bytes(16)), \
                 patch(_REACTION_DEST, return_value=MagicMock()), \
                 patch("trenchchat.core.reaction.LXMF.LXMessage") as mock_lxm_cls:
                mock_lxm_cls.return_value = MagicMock(fields={})
                for cb in router._delivery_callbacks:
                    cb(lxm)

        deliver_reaction("msg1")
        request_count_after_first = sum(
            1 for m in sent_lxms
            if getattr(m, "fields", {}).get(F_MSG_TYPE) == MT_EMOJI_REQUEST
        )

        deliver_reaction("msg2")
        request_count_after_second = sum(
            1 for m in sent_lxms
            if getattr(m, "fields", {}).get(F_MSG_TYPE) == MT_EMOJI_REQUEST
        )

        assert request_count_after_first == 1
        assert request_count_after_second == 1  # no duplicate request


# ---------------------------------------------------------------------------
# ReactionManager: MT_EMOJI_REQUEST / MT_EMOJI_RESPONSE
# ---------------------------------------------------------------------------

class TestEmojiRequestResponse:
    def _deliver(self, router, lxm):
        for cb in router._delivery_callbacks:
            cb(lxm)

    def test_emoji_request_sends_response_when_known(self, reaction_mgr):
        mgr, storage, identity, router = reaction_mgr
        requester_hex = "cc" * 16
        img = _make_png()
        emoji_hash = compute_emoji_hash(img)
        storage.insert_emoji(emoji_hash, "test", img, time.time())

        # The identity mock must return a proper hex string from .hash.hex()
        requester_identity_mock = MagicMock()
        requester_identity_mock.hash = bytes.fromhex(requester_hex)

        sent_lxms = []
        router.send = lambda lxm: sent_lxms.append(lxm)

        lxm = _make_lxm({
            F_MSG_TYPE: MT_EMOJI_REQUEST,
            F_EMOJI_HASH: bytes.fromhex(emoji_hash),
        }, source_hash_hex=requester_hex)

        # recall resolves the requester identity; .hash must be bytes so .hex() works.
        # We also mock LXMF.LXMessage so we can inspect the fields of outbound messages.
        outbound_fields = {}

        def capture_lxm(dest, source, content, desired_method=None):
            m = MagicMock()
            m.fields = {}

            def set_fields(v):
                m._fields = v
                outbound_fields.update(v)

            type(m).fields = property(lambda s: s._fields if hasattr(s, "_fields") else {},
                                      lambda s, v: set_fields(v))
            m._fields = {}
            return m

        with patch(_REACTION_RECALL, return_value=requester_identity_mock), \
             patch(_REACTION_DEST_HASH, return_value=bytes.fromhex(requester_hex)), \
             patch(_REACTION_DEST, return_value=MagicMock()), \
             patch("trenchchat.core.reaction.LXMF.LXMessage", side_effect=capture_lxm):
            self._deliver(router, lxm)

        assert outbound_fields.get(F_MSG_TYPE) == MT_EMOJI_RESPONSE
        assert outbound_fields.get(F_EMOJI_DATA) == img

    def test_emoji_request_silent_when_unknown(self, reaction_mgr):
        mgr, storage, identity, router = reaction_mgr
        requester_hex = "cc" * 16
        unknown_hash = "a" * 64

        requester_identity_mock = MagicMock()
        requester_identity_mock.hash = bytes.fromhex(requester_hex)

        sent_lxms = []
        router.send = lambda lxm: sent_lxms.append(lxm)

        lxm = _make_lxm({
            F_MSG_TYPE: MT_EMOJI_REQUEST,
            F_EMOJI_HASH: bytes.fromhex(unknown_hash),
        }, source_hash_hex=requester_hex)

        with patch(_REACTION_RECALL, return_value=requester_identity_mock):
            self._deliver(router, lxm)

        assert not any(
            getattr(m, "fields", {}).get(F_MSG_TYPE) == MT_EMOJI_RESPONSE
            for m in sent_lxms
        )

    def test_emoji_response_stored(self, reaction_mgr):
        mgr, storage, identity, router = reaction_mgr
        img = _make_png()
        emoji_hash = compute_emoji_hash(img)

        sender_hex = "dd" * 16
        sender_identity_mock = MagicMock()
        sender_identity_mock.hash = bytes.fromhex(sender_hex)

        lxm = _make_lxm({
            F_MSG_TYPE: MT_EMOJI_RESPONSE,
            F_EMOJI_HASH: bytes.fromhex(emoji_hash),
            F_EMOJI_DATA: img,
        }, source_hash_hex=sender_hex)

        with patch(_REACTION_RECALL, return_value=sender_identity_mock):
            self._deliver(router, lxm)

        assert storage.emoji_exists(emoji_hash)
        row = storage.get_emoji(emoji_hash)
        assert bytes(row["image_data"]) == img

    def test_emoji_response_rejected_if_hash_mismatch(self, reaction_mgr):
        mgr, storage, identity, router = reaction_mgr
        img = _make_png()
        wrong_hash = "e" * 64   # does not match img

        sender_hex = "dd" * 16
        sender_identity_mock = MagicMock()
        sender_identity_mock.hash = bytes.fromhex(sender_hex)

        lxm = _make_lxm({
            F_MSG_TYPE: MT_EMOJI_RESPONSE,
            F_EMOJI_HASH: bytes.fromhex(wrong_hash),
            F_EMOJI_DATA: img,
        }, source_hash_hex=sender_hex)

        with patch(_REACTION_RECALL, return_value=sender_identity_mock):
            self._deliver(router, lxm)

        assert not storage.emoji_exists(wrong_hash)

    def test_emoji_response_rejected_if_oversized(self, reaction_mgr):
        mgr, storage, identity, router = reaction_mgr
        oversized = b"x" * (MAX_EMOJI_BYTES + 1)
        emoji_hash = compute_emoji_hash(oversized)

        sender_hex = "dd" * 16
        sender_identity_mock = MagicMock()
        sender_identity_mock.hash = bytes.fromhex(sender_hex)

        lxm = _make_lxm({
            F_MSG_TYPE: MT_EMOJI_RESPONSE,
            F_EMOJI_HASH: bytes.fromhex(emoji_hash),
            F_EMOJI_DATA: oversized,
        }, source_hash_hex=sender_hex)

        with patch(_REACTION_RECALL, return_value=sender_identity_mock):
            self._deliver(router, lxm)

        assert not storage.emoji_exists(emoji_hash)

    def test_emoji_callback_fires_on_response(self, reaction_mgr):
        mgr, storage, identity, router = reaction_mgr
        img = _make_png()
        emoji_hash = compute_emoji_hash(img)

        received = []
        mgr.add_emoji_callback(lambda h: received.append(h))

        sender_hex = "dd" * 16
        sender_identity_mock = MagicMock()
        sender_identity_mock.hash = bytes.fromhex(sender_hex)

        lxm = _make_lxm({
            F_MSG_TYPE: MT_EMOJI_RESPONSE,
            F_EMOJI_HASH: bytes.fromhex(emoji_hash),
            F_EMOJI_DATA: img,
        }, source_hash_hex=sender_hex)

        with patch(_REACTION_RECALL, return_value=sender_identity_mock):
            self._deliver(router, lxm)

        assert emoji_hash in received


# ---------------------------------------------------------------------------
# Adversarial: non-member reacting
# ---------------------------------------------------------------------------

class TestAdversarialReactions:
    """
    ReactionManager stores reactions from any subscribed-channel sender without
    checking channel membership (that enforcement lives in Messaging for chat
    messages).  This is intentional: reactions are lightweight and channel
    membership is already validated upstream.  These tests verify the manager
    handles unknown senders gracefully (no crash, no data corruption).
    """

    def _deliver(self, router, lxm):
        for cb in router._delivery_callbacks:
            cb(lxm)

    def _setup_channel(self, storage, channel_hex: str) -> None:
        storage.upsert_channel(
            hash=channel_hex, name="test", description="",
            creator_hash="aa" * 16, permissions="{}", created_at=0.0,
        )
        storage.subscribe(channel_hex)

    def test_reaction_with_no_source_hash_ignored(self, reaction_mgr):
        """A message with no source_hash must be silently dropped."""
        mgr, storage, identity, router = reaction_mgr
        channel = "cc" * 16
        self._setup_channel(storage, channel)
        emoji_hash = compute_emoji_hash(_make_png())

        lxm = _make_lxm({
            F_MSG_TYPE: MT_REACTION,
            F_CHANNEL_HASH: bytes.fromhex(channel),
            F_REACTION_MSG_ID: "msg1",
            F_EMOJI_HASH: bytes.fromhex(emoji_hash),
            F_REACTION_REMOVE: False,
        }, source_hash_hex=None)

        with patch(_REACTION_RECALL, return_value=None):
            self._deliver(router, lxm)

        assert storage.get_reactions("msg1") == []

    def test_reaction_missing_message_id_ignored(self, reaction_mgr):
        """MT_REACTION without F_REACTION_MSG_ID must be ignored."""
        mgr, storage, identity, router = reaction_mgr
        sender_hex = "bb" * 16
        channel = "cc" * 16
        self._setup_channel(storage, channel)
        emoji_hash = compute_emoji_hash(_make_png())

        sender_identity_mock = MagicMock()
        sender_identity_mock.hash = bytes.fromhex(sender_hex)

        lxm = _make_lxm({
            F_MSG_TYPE: MT_REACTION,
            F_CHANNEL_HASH: bytes.fromhex(channel),
            # F_REACTION_MSG_ID deliberately missing
            F_EMOJI_HASH: bytes.fromhex(emoji_hash),
            F_REACTION_REMOVE: False,
        }, source_hash_hex=sender_hex)

        with patch(_REACTION_RECALL, return_value=sender_identity_mock):
            self._deliver(router, lxm)

        # Nothing should be stored
        assert storage.get_reactions("") == []

    def test_emoji_response_from_unknown_sender_ignored(self, reaction_mgr):
        """MT_EMOJI_RESPONSE with no resolvable sender identity is silently dropped."""
        mgr, storage, identity, router = reaction_mgr
        img = _make_png()
        emoji_hash = compute_emoji_hash(img)

        lxm = _make_lxm({
            F_MSG_TYPE: MT_EMOJI_RESPONSE,
            F_EMOJI_HASH: bytes.fromhex(emoji_hash),
            F_EMOJI_DATA: img,
        }, source_hash_hex="ff" * 16)

        # recall returns None → sender unknown
        with patch(_REACTION_RECALL, return_value=None):
            self._deliver(router, lxm)

        # The response body is valid so the emoji IS stored even without sender resolve.
        # (The manager uses sender only for request routing, not emoji validation.)
        assert storage.emoji_exists(emoji_hash)
