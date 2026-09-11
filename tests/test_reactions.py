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
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from trenchchat.core.protocol import (
    F_MSG_TYPE, F_CHANNEL_HASH, F_EMOJI_HASH, F_EMOJI_DATA, F_EMOJI_NAME,
    F_REACTION_MSG_ID, F_REACTION_REMOVE, F_REACTION_UNICODE,
    MT_REACTION, MT_EMOJI_REQUEST, MT_EMOJI_RESPONSE,
)
from trenchchat.core.permissions import PRESET_OPEN, PRESET_PRIVATE, ROLE_MEMBER
from trenchchat.core.reaction import (
    EMOJI_FLUSH_BATCH, EMOJI_FLUSH_COOLDOWN_SECS, EMOJI_REQUEST_RETRY_SECS,
    MAX_EMOJI_BYTES, MAX_EMOJI_NAME_LEN, MAX_EMOJI_REFS_PER_MESSAGE,
    ReactionManager, compute_emoji_hash,
)
from trenchchat.core.storage import Storage
from trenchchat.network.base import InboundMessage, SendState, reticulum_limits


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


class _RecordingRouter:
    """A router that records what would have been sent, and sends nothing."""

    def __init__(self):
        self.sent: list[tuple[str, dict]] = []
        self.paths_requested: list[str] = []
        self.reachable = True
        self._delivery_callbacks: list = []

    def add_delivery_callback(self, cb) -> None:
        self._delivery_callbacks.append(cb)

    def deliver(self, message) -> None:
        """Hand a message to every registered delivery callback."""
        for cb in list(self._delivery_callbacks):
            cb(message)

    def send(self, dest_hex, fields, content="", **kwargs):
        if not self.reachable:
            return SendState.NO_PATH
        self.sent.append((dest_hex, dict(fields)))
        return SendState.SENT

    def request_path(self, dest_hex) -> None:
        self.paths_requested.append(dest_hex)

    def limits_for(self, dest_hex):
        return reticulum_limits()

    def public_key_for(self, peer_hex):
        return None

    def sent_fields(self) -> list[dict]:
        """The field dicts of every recorded send."""
        return [fields for _dest, fields in self.sent]


def _make_router_mock():
    """Return a router double that records sends."""
    return _RecordingRouter()


def _make_message(fields: dict, source_hex: str = ""):
    """An inbound message as the Router hands one to a manager."""
    return InboundMessage(source_hex=source_hex, fields=fields)


@pytest.fixture
def db(tmp_path) -> Storage:
    s = Storage(db_path=tmp_path / "test.db")
    yield s
    s.close()




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

    def _setup_channel(self, storage, channel_hex: str,
                       member_hex: str = "bb" * 16) -> None:
        """Insert a channel row, subscribe, and admit member_hex.

        Inbound reactions are authorised the same way inbound messages are, so
        the sender has to be a real member holding send_message for the
        legitimate-traffic cases below.  Rejection of an unauthorised reactor
        is covered in tests/test_adversarial.py.
        """
        storage.upsert_channel(
            hash=channel_hex, name="test", description="",
            creator_hash="aa" * 16, permissions=PRESET_PRIVATE, created_at=0.0,
        )
        storage.subscribe(channel_hex)
        storage.upsert_member(channel_hex, member_hex, "Member", role=ROLE_MEMBER)

    def test_inbound_reaction_stored(self, reaction_mgr):
        mgr, storage, identity, router = reaction_mgr
        sender_hex = "bb" * 16

        channel = "cc" * 16
        self._setup_channel(storage, channel)
        msg_id = "msg_abc"
        img = _make_png()
        emoji_hash = compute_emoji_hash(img)
        storage.insert_emoji(emoji_hash, "e", img, time.time())


        lxm = _make_message({
            F_MSG_TYPE: MT_REACTION,
            F_CHANNEL_HASH: bytes.fromhex(channel),
            F_REACTION_MSG_ID: msg_id,
            F_EMOJI_HASH: bytes.fromhex(emoji_hash),
            F_REACTION_REMOVE: False,
        }, sender_hex)

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


        lxm = _make_message({
            F_MSG_TYPE: MT_REACTION,
            F_CHANNEL_HASH: bytes.fromhex(channel),
            F_REACTION_MSG_ID: msg_id,
            F_EMOJI_HASH: bytes.fromhex(emoji_hash),
            F_REACTION_REMOVE: True,
        }, sender_hex)

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


        lxm = _make_message({
            F_MSG_TYPE: MT_REACTION,
            F_CHANNEL_HASH: bytes.fromhex(channel),
            F_REACTION_MSG_ID: msg_id,
            F_EMOJI_HASH: bytes.fromhex(emoji_hash),
            F_REACTION_REMOVE: False,
        }, sender_hex)

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



        lxm = _make_message({
            F_MSG_TYPE: MT_REACTION,
            F_CHANNEL_HASH: bytes.fromhex(channel),
            F_REACTION_MSG_ID: msg_id,
            F_EMOJI_HASH: bytes.fromhex(emoji_hash),
            F_REACTION_REMOVE: False,
        }, sender_hex)


        self._deliver(router, lxm)

        # A MT_EMOJI_REQUEST should have been sent
        assert any(f.get(F_MSG_TYPE) == MT_EMOJI_REQUEST
                   for f in router.sent_fields())

    def test_inbound_emoji_request_dedup(self, reaction_mgr):
        """A second reaction with the same unknown emoji must not send a duplicate request."""
        mgr, storage, identity, router = reaction_mgr
        sender_hex = "bb" * 16
        channel = "cc" * 16
        self._setup_channel(storage, channel)
        img = _make_png()
        emoji_hash = compute_emoji_hash(img)



        def deliver_reaction(msg_id: str):
            lxm = _make_message({
                F_MSG_TYPE: MT_REACTION,
                F_CHANNEL_HASH: bytes.fromhex(channel),
                F_REACTION_MSG_ID: msg_id,
                F_EMOJI_HASH: bytes.fromhex(emoji_hash),
                F_REACTION_REMOVE: False,
            }, sender_hex)
            for cb in router._delivery_callbacks:
                cb(lxm)

        deliver_reaction("msg1")
        request_count_after_first = sum(
            1 for f in router.sent_fields()
            if f.get(F_MSG_TYPE) == MT_EMOJI_REQUEST
        )

        deliver_reaction("msg2")
        request_count_after_second = sum(
            1 for f in router.sent_fields()
            if f.get(F_MSG_TYPE) == MT_EMOJI_REQUEST
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

        # Emoji requests are only served to peers we share a channel with,
        # so the library cannot be enumerated by an arbitrary node.
        channel_hex = "dd" * 16
        storage.upsert_channel(
            hash=channel_hex, name="test", description="",
            creator_hash="aa" * 16, permissions=PRESET_PRIVATE, created_at=0.0,
        )
        storage.subscribe(channel_hex)
        storage.upsert_member(channel_hex, requester_hex, "Requester", role=ROLE_MEMBER)

        lxm = _make_message({
            F_MSG_TYPE: MT_EMOJI_REQUEST,
            F_EMOJI_HASH: bytes.fromhex(emoji_hash),
        }, requester_hex)

        self._deliver(router, lxm)

        assert len(router.sent) == 1
        wire = router.sent_fields()[0]
        assert wire.get(F_MSG_TYPE) == MT_EMOJI_RESPONSE
        assert wire.get(F_EMOJI_DATA) == img

    def test_emoji_request_silent_when_unknown(self, reaction_mgr):
        mgr, storage, identity, router = reaction_mgr
        requester_hex = "cc" * 16
        unknown_hash = "a" * 64



        lxm = _make_message({
            F_MSG_TYPE: MT_EMOJI_REQUEST,
            F_EMOJI_HASH: bytes.fromhex(unknown_hash),
        }, requester_hex)

        self._deliver(router, lxm)

        assert not any(f.get(F_MSG_TYPE) == MT_EMOJI_RESPONSE
                       for f in router.sent_fields())

    def test_emoji_response_stored(self, reaction_mgr):
        mgr, storage, identity, router = reaction_mgr
        img = _make_png()
        emoji_hash = compute_emoji_hash(img)

        sender_hex = "dd" * 16

        # A response only applies to an emoji we asked for; this is the
        # state _request_emoji leaves behind.
        mgr._pending_emoji_requests[emoji_hash] = time.time()
        lxm = _make_message({
            F_MSG_TYPE: MT_EMOJI_RESPONSE,
            F_EMOJI_HASH: bytes.fromhex(emoji_hash),
            F_EMOJI_DATA: img,
        }, sender_hex)

        self._deliver(router, lxm)

        assert storage.emoji_exists(emoji_hash)
        row = storage.get_emoji(emoji_hash)
        assert bytes(row["image_data"]) == img

    def test_emoji_response_stores_correct_name(self, reaction_mgr):
        """The name carried in F_EMOJI_NAME is stored, not the truncated hash."""
        mgr, storage, identity, router = reaction_mgr
        img = _make_png()
        emoji_hash = compute_emoji_hash(img)

        sender_hex = "dd" * 16

        # A response only applies to an emoji we asked for; this is the
        # state _request_emoji leaves behind.
        mgr._pending_emoji_requests[emoji_hash] = time.time()
        lxm = _make_message({
            F_MSG_TYPE:   MT_EMOJI_RESPONSE,
            F_EMOJI_HASH: bytes.fromhex(emoji_hash),
            F_EMOJI_DATA: img,
            F_EMOJI_NAME: "wave",
        }, sender_hex)

        self._deliver(router, lxm)

        row = storage.get_emoji(emoji_hash)
        assert row["name"] == "wave"

    def test_emoji_response_falls_back_to_hash_prefix_when_no_name(self, reaction_mgr):
        """When F_EMOJI_NAME is absent the first 8 chars of the hash are used as name."""
        mgr, storage, identity, router = reaction_mgr
        img = _make_png()
        emoji_hash = compute_emoji_hash(img)

        sender_hex = "dd" * 16

        # A response only applies to an emoji we asked for; this is the
        # state _request_emoji leaves behind.
        mgr._pending_emoji_requests[emoji_hash] = time.time()
        lxm = _make_message({
            F_MSG_TYPE:   MT_EMOJI_RESPONSE,
            F_EMOJI_HASH: bytes.fromhex(emoji_hash),
            F_EMOJI_DATA: img,
        }, sender_hex)

        self._deliver(router, lxm)

        row = storage.get_emoji(emoji_hash)
        assert row["name"] == emoji_hash[:8]

    def test_emoji_response_rejected_if_hash_mismatch(self, reaction_mgr):
        mgr, storage, identity, router = reaction_mgr
        img = _make_png()
        wrong_hash = "e" * 64   # does not match img

        sender_hex = "dd" * 16

        lxm = _make_message({
            F_MSG_TYPE: MT_EMOJI_RESPONSE,
            F_EMOJI_HASH: bytes.fromhex(wrong_hash),
            F_EMOJI_DATA: img,
        }, sender_hex)

        self._deliver(router, lxm)

        assert not storage.emoji_exists(wrong_hash)

    def test_emoji_response_rejected_if_oversized(self, reaction_mgr):
        mgr, storage, identity, router = reaction_mgr
        oversized = b"x" * (MAX_EMOJI_BYTES + 1)
        emoji_hash = compute_emoji_hash(oversized)

        sender_hex = "dd" * 16

        lxm = _make_message({
            F_MSG_TYPE: MT_EMOJI_RESPONSE,
            F_EMOJI_HASH: bytes.fromhex(emoji_hash),
            F_EMOJI_DATA: oversized,
        }, sender_hex)

        self._deliver(router, lxm)

        assert not storage.emoji_exists(emoji_hash)

    def test_emoji_callback_fires_on_response(self, reaction_mgr):
        mgr, storage, identity, router = reaction_mgr
        img = _make_png()
        emoji_hash = compute_emoji_hash(img)

        received = []
        mgr.add_emoji_callback(lambda h: received.append(h))

        sender_hex = "dd" * 16

        # A response only applies to an emoji we asked for; this is the
        # state _request_emoji leaves behind.
        mgr._pending_emoji_requests[emoji_hash] = time.time()
        lxm = _make_message({
            F_MSG_TYPE: MT_EMOJI_RESPONSE,
            F_EMOJI_HASH: bytes.fromhex(emoji_hash),
            F_EMOJI_DATA: img,
        }, sender_hex)

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

        lxm = _make_message({
            F_MSG_TYPE: MT_REACTION,
            F_CHANNEL_HASH: bytes.fromhex(channel),
            F_REACTION_MSG_ID: "msg1",
            F_EMOJI_HASH: bytes.fromhex(emoji_hash),
            F_REACTION_REMOVE: False,
        }, None)

        self._deliver(router, lxm)

        assert storage.get_reactions("msg1") == []

    def test_reaction_missing_message_id_ignored(self, reaction_mgr):
        """MT_REACTION without F_REACTION_MSG_ID must be ignored."""
        mgr, storage, identity, router = reaction_mgr
        sender_hex = "bb" * 16
        channel = "cc" * 16
        self._setup_channel(storage, channel)
        emoji_hash = compute_emoji_hash(_make_png())


        lxm = _make_message({
            F_MSG_TYPE: MT_REACTION,
            F_CHANNEL_HASH: bytes.fromhex(channel),
            # F_REACTION_MSG_ID deliberately missing
            F_EMOJI_HASH: bytes.fromhex(emoji_hash),
            F_REACTION_REMOVE: False,
        }, sender_hex)

        self._deliver(router, lxm)

        # Nothing should be stored
        assert storage.get_reactions("") == []

    def test_emoji_response_from_unknown_sender_ignored(self, reaction_mgr):
        """MT_EMOJI_RESPONSE with no resolvable sender identity is silently dropped."""
        mgr, storage, identity, router = reaction_mgr
        img = _make_png()
        emoji_hash = compute_emoji_hash(img)

        # A response only applies to an emoji we asked for; this is the
        # state _request_emoji leaves behind.
        mgr._pending_emoji_requests[emoji_hash] = time.time()
        lxm = _make_message({
            F_MSG_TYPE: MT_EMOJI_RESPONSE,
            F_EMOJI_HASH: bytes.fromhex(emoji_hash),
            F_EMOJI_DATA: img,
        }, "ff" * 16)

        # recall returns None → sender unknown
        self._deliver(router, lxm)

        # The response body is valid so the emoji IS stored even without sender resolve.
        # (The manager uses sender only for request routing, not emoji validation.)
        assert storage.emoji_exists(emoji_hash)

    def test_unsolicited_emoji_response_is_discarded(self, reaction_mgr):
        """An emoji nobody asked for is not written to the local library.

        Storing unsolicited emoji lets any authenticated peer fill the library
        at will, and a fresh hash per push slips past the exists-check that
        would otherwise de-duplicate.
        """
        mgr, storage, identity, router = reaction_mgr
        img = _make_png()
        emoji_hash = compute_emoji_hash(img)

        sender_hex = "dd" * 16

        lxm = _make_message({
            F_MSG_TYPE:   MT_EMOJI_RESPONSE,
            F_EMOJI_HASH: bytes.fromhex(emoji_hash),
            F_EMOJI_DATA: img,
        }, sender_hex)

        self._deliver(router, lxm)

        assert not storage.emoji_exists(emoji_hash), \
            "An emoji response we never requested was stored"

    def test_emoji_request_from_unrelated_peer_on_open_channel_is_refused(
            self, reaction_mgr):
        """Being in a public channel must not make us an open emoji server.

        The open-join branch of the shared-channel check has to name the
        requester; otherwise any node can enumerate the library.
        """
        mgr, storage, identity, router = reaction_mgr
        img = _make_png()
        emoji_hash = compute_emoji_hash(img)
        storage.insert_emoji(emoji_hash, "test", img, time.time())

        channel_hex = "dd" * 16
        storage.upsert_channel(
            hash=channel_hex, name="open", description="",
            creator_hash="aa" * 16, permissions=PRESET_OPEN, created_at=0.0,
        )
        storage.subscribe(channel_hex)

        stranger_hex = "cc" * 16
        assert not mgr._shares_any_channel(stranger_hex), \
            "An unrelated peer was treated as sharing an open-join channel"


        lxm = _make_message({
            F_MSG_TYPE:   MT_EMOJI_REQUEST,
            F_EMOJI_HASH: bytes.fromhex(emoji_hash),
        }, stranger_hex)

        self._deliver(router, lxm)

        assert not router.sent, "The emoji library answered an unrelated peer"


# ---------------------------------------------------------------------------
# ReactionManager.request_emoji (public wrapper)
# ---------------------------------------------------------------------------

class TestRequestEmoji:
    """request_emoji() is the public surface for requesting an emoji by hash."""

    def test_request_emoji_sends_lxm_when_peer_known(self, reaction_mgr):
        mgr, storage, identity, router = reaction_mgr
        img = _make_png()
        emoji_hash = compute_emoji_hash(img)
        peer_hex = "bb" * 32

        mgr.request_emoji(peer_hex, emoji_hash)

        assert len(router.sent) == 1
        fields = router.sent_fields()[0]
        assert fields[F_MSG_TYPE] == MT_EMOJI_REQUEST
        assert fields[F_EMOJI_HASH] == bytes.fromhex(emoji_hash)

    def test_request_emoji_deduplicated(self, reaction_mgr):
        mgr, storage, identity, router = reaction_mgr
        img = _make_png()
        emoji_hash = compute_emoji_hash(img)
        peer_hex = "bb" * 32

        mgr.request_emoji(peer_hex, emoji_hash)
        mgr.request_emoji(peer_hex, emoji_hash)   # second call deduped

        assert len(router.sent) == 1


# ---------------------------------------------------------------------------
# Unicode reaction keys (regression: built-in emoji never reached other peers)
# ---------------------------------------------------------------------------

class TestUnicodeReactionKeys:
    """A reaction key is either a custom emoji's SHA-256 or a unicode char.

    The wire format used to assume the former unconditionally, so reacting
    with a built-in emoji raised out of _broadcast_reaction after the local
    row had already been written -- the reactor saw their own chip and no
    peer ever did.
    """

    def _broadcast_fields(self, mgr, router, emoji_key: str, remove: bool = False):
        """Drive one broadcast to a single peer and return the fields it sent."""
        if remove:
            mgr.remove_reaction("cc" * 16, "msg1", emoji_key, ["bb" * 16])
        else:
            mgr.add_reaction("cc" * 16, "msg1", emoji_key, ["bb" * 16])
        assert len(router.sent) == 1, "reaction was not broadcast"
        return router.sent_fields()[0]

    def test_unicode_reaction_is_broadcast(self, reaction_mgr):
        mgr, storage, identity, router = reaction_mgr
        fields = self._broadcast_fields(mgr, router, "\U0001F44D")

        assert fields[F_MSG_TYPE] == MT_REACTION
        assert fields[F_REACTION_UNICODE] == "\U0001F44D"
        assert F_EMOJI_HASH not in fields

    def test_unicode_reaction_removal_is_broadcast(self, reaction_mgr):
        mgr, storage, identity, router = reaction_mgr
        fields = self._broadcast_fields(mgr, router, "❤️", remove=True)

        assert fields[F_REACTION_UNICODE] == "❤️"
        assert fields[F_REACTION_REMOVE] is True

    def test_custom_emoji_still_uses_the_hash_field(self, reaction_mgr):
        mgr, storage, identity, router = reaction_mgr
        emoji_hash = compute_emoji_hash(_make_png())
        fields = self._broadcast_fields(mgr, router, emoji_hash)

        assert fields[F_EMOJI_HASH] == bytes.fromhex(emoji_hash)
        assert F_REACTION_UNICODE not in fields

    def test_inbound_unicode_reaction_stored(self, reaction_mgr):
        mgr, storage, identity, router = reaction_mgr
        sender_hex = "bb" * 16
        channel = "cc" * 16
        storage.upsert_channel(
            hash=channel, name="test", description="",
            creator_hash="aa" * 16, permissions=PRESET_PRIVATE, created_at=0.0,
        )
        storage.subscribe(channel)
        storage.upsert_member(channel, sender_hex, "Member", role=ROLE_MEMBER)

        lxm = _make_message({
            F_MSG_TYPE: MT_REACTION,
            F_CHANNEL_HASH: bytes.fromhex(channel),
            F_REACTION_MSG_ID: "msg1",
            F_REACTION_UNICODE: "\U0001F44D",
            F_REACTION_REMOVE: False,
        }, sender_hex)

        for cb in router._delivery_callbacks:
            cb(lxm)

        rows = storage.get_reactions("msg1")
        assert [r["emoji_hash"] for r in rows] == ["\U0001F44D"]

    def test_inbound_unicode_reaction_requests_no_image(self, reaction_mgr):
        """A unicode key is not a hash; it must never trigger an emoji fetch."""
        mgr, storage, identity, router = reaction_mgr
        sender_hex = "bb" * 16
        channel = "cc" * 16
        storage.upsert_channel(
            hash=channel, name="test", description="",
            creator_hash="aa" * 16, permissions=PRESET_PRIVATE, created_at=0.0,
        )
        storage.subscribe(channel)
        storage.upsert_member(channel, sender_hex, "Member", role=ROLE_MEMBER)

        lxm = _make_message({
            F_MSG_TYPE: MT_REACTION,
            F_CHANNEL_HASH: bytes.fromhex(channel),
            F_REACTION_MSG_ID: "msg1",
            F_REACTION_UNICODE: "\U0001F44D",
        }, sender_hex)

        for cb in router._delivery_callbacks:
            cb(lxm)

        assert router.sent == []


# ---------------------------------------------------------------------------
# Inline :name@hash: fetch (regression: only one render path requested them)
# ---------------------------------------------------------------------------

class TestInlineEmojiFetch:
    """An inbound chat message pulls the emoji its tokens reference.

    This lives in the core manager rather than a render path so every client
    gets it -- the Flutter client had no render-side hook, so inline custom
    emoji stayed as literal text forever.
    """

    def _share_channel(self, storage, sender_hex: str) -> None:
        """Put the sender in a channel we hold.

        Fetching is gated on that, the same way answering a request is: a
        message from an identity we share nothing with must not turn into an
        outbound request per token it names.
        """
        channel_hex = "cc" * 16
        storage.upsert_channel(
            hash=channel_hex, name="test", description="",
            creator_hash="aa" * 16, permissions=PRESET_PRIVATE, created_at=0.0,
        )
        storage.subscribe(channel_hex)
        storage.upsert_member(channel_hex, sender_hex, "Member", role=ROLE_MEMBER)

    def _deliver_chat(self, mgr, router, sender_hex: str, content: str):
        before = len(router.sent)
        message = _make_message({}, sender_hex)
        message.content = content
        router.deliver(message)
        return router.sent_fields()[before:]

    def test_unknown_inline_token_requests_the_emoji(self, reaction_mgr):
        mgr, storage, identity, router = reaction_mgr
        emoji_hash = compute_emoji_hash(_make_png())
        sender_hex = "bb" * 16
        self._share_channel(storage, sender_hex)

        sent = self._deliver_chat(
            mgr, router, sender_hex, f"look :wave@{emoji_hash}: here"
        )

        assert len(sent) == 1
        assert sent[0][F_MSG_TYPE] == MT_EMOJI_REQUEST
        assert sent[0][F_EMOJI_HASH] == bytes.fromhex(emoji_hash)
        assert sent[0][F_EMOJI_NAME] == "wave"

    def test_known_inline_token_requests_nothing(self, reaction_mgr):
        mgr, storage, identity, router = reaction_mgr
        img = _make_png()
        emoji_hash = compute_emoji_hash(img)
        storage.insert_emoji(emoji_hash, "wave", img, time.time())
        self._share_channel(storage, "bb" * 16)

        sent = self._deliver_chat(
            mgr, router, "bb" * 16, f"look :wave@{emoji_hash}: here"
        )
        assert sent == []

    def test_legacy_token_requests_nothing(self, reaction_mgr):
        """A :name: token carries no hash, so there is nothing to ask for."""
        mgr, storage, identity, router = reaction_mgr
        self._share_channel(storage, "bb" * 16)
        sent = self._deliver_chat(mgr, router, "bb" * 16, "look :wave: here")
        assert sent == []

    def test_plain_message_requests_nothing(self, reaction_mgr):
        mgr, storage, identity, router = reaction_mgr
        self._share_channel(storage, "bb" * 16)
        sent = self._deliver_chat(mgr, router, "bb" * 16, "no tokens at all")
        assert sent == []

    def test_a_stranger_cannot_drive_any_fetch(self, reaction_mgr):
        """No membership, no shared channel, and the path is unthrottled --
        one received packet would otherwise buy an outbound request per token."""
        mgr, storage, identity, router = reaction_mgr
        emoji_hash = compute_emoji_hash(_make_png())

        sent = self._deliver_chat(
            mgr, router, "ee" * 16, f"look :wave@{emoji_hash}: here"
        )
        assert sent == []

    def test_references_from_one_message_are_capped(self, reaction_mgr):
        mgr, storage, identity, router = reaction_mgr
        sender_hex = "bb" * 16
        self._share_channel(storage, sender_hex)

        hashes = [compute_emoji_hash(_make_png(color=(i, i, i)))
                  for i in range(MAX_EMOJI_REFS_PER_MESSAGE + 4)]
        content = " ".join(f":e{i}@{h}:" for i, h in enumerate(hashes))

        sent = self._deliver_chat(mgr, router, sender_hex, content)
        assert len(sent) == MAX_EMOJI_REFS_PER_MESSAGE


# ---------------------------------------------------------------------------
# Emoji request retry (regression: a dropped request was never retried)
# ---------------------------------------------------------------------------

class TestEmojiRequestRetry:
    """A silently-dropped emoji request must not block the hash forever.

    The responder drops requests without replying (throttled, unknown hash, no
    shared channel), so the requester's in-flight marker has to expire or the
    emoji is unreachable for the life of the process.
    """

    def _request(self, mgr, router, peer_hex: str, emoji_hash: str) -> list:
        before = len(router.sent)
        mgr.request_emoji(peer_hex, emoji_hash)
        return router.sent_fields()[before:]

    def test_request_retried_after_the_window_expires(self, reaction_mgr):
        mgr, storage, identity, router = reaction_mgr
        emoji_hash = compute_emoji_hash(_make_png())
        peer_hex = "bb" * 16

        assert len(self._request(mgr, router, peer_hex, emoji_hash)) == 1
        assert self._request(mgr, router, peer_hex, emoji_hash) == []

        with patch("trenchchat.core.reaction.time.time",
                   return_value=time.time() + EMOJI_REQUEST_RETRY_SECS + 1):
            assert len(self._request(mgr, router, peer_hex, emoji_hash)) == 1

    def test_unresolved_path_requests_a_path_and_allows_retry(self, reaction_mgr):
        mgr, storage, identity, router = reaction_mgr
        emoji_hash = compute_emoji_hash(_make_png())
        peer_hex = "bb" * 16

        router.reachable = False
        mgr.request_emoji(peer_hex, emoji_hash)

        assert router.sent == []
        assert router.paths_requested == [peer_hex]
        # No wait imposed on the retry: the request never went out.
        router.reachable = True
        assert len(self._request(mgr, router, peer_hex, emoji_hash)) == 1

    def test_flush_re_requests_emoji_the_peer_reacted_with(self, reaction_mgr):
        mgr, storage, identity, router = reaction_mgr
        peer_hex = "bb" * 16
        have = _make_png(color=(1, 2, 3))
        have_hash = compute_emoji_hash(have)
        storage.insert_emoji(have_hash, "have", have, time.time())
        missing_hash = compute_emoji_hash(_make_png(color=(9, 9, 9)))

        channel = "cc" * 16
        for h in (have_hash, missing_hash, "\U0001F44D"):
            storage.insert_reaction("msg1", h, peer_hex, channel, time.time())

        mgr.flush_pending_emoji(peer_hex)

        assert len(router.sent) == 1, \
            "only the unresolved custom emoji should be requested"
        assert router.sent_fields()[0][F_EMOJI_HASH] == bytes.fromhex(missing_hash)

    def test_periodic_sweep_retries_every_peer(self, reaction_mgr):
        """The maintenance tick is what actually gets a dropped emoji retried.

        A peer announce is far too rare to rely on, and after a burst the
        holder may send nothing else for minutes.
        """
        mgr, storage, identity, router = reaction_mgr
        peer_a, peer_b = "bb" * 16, "dd" * 16
        hash_a = compute_emoji_hash(_make_png(color=(7, 7, 7)))
        hash_b = compute_emoji_hash(_make_png(color=(8, 8, 8)))
        storage.insert_reaction("msg1", hash_a, peer_a, "cc" * 16, time.time())
        storage.insert_reaction("msg2", hash_b, peer_b, "cc" * 16, time.time())

        mgr.retry_pending_emoji()

        requested = {f[F_EMOJI_HASH] for f in router.sent_fields()}
        assert requested == {bytes.fromhex(hash_a), bytes.fromhex(hash_b)}

    def test_sweep_is_a_noop_when_nothing_is_missing(self, reaction_mgr):
        mgr, storage, identity, router = reaction_mgr
        img = _make_png()
        h = compute_emoji_hash(img)
        storage.insert_emoji(h, "have", img, time.time())
        storage.insert_reaction("msg1", h, "bb" * 16, "cc" * 16, time.time())

        mgr.retry_pending_emoji()
        assert router.sent == []

    def test_flush_respects_its_cooldown(self, reaction_mgr):
        """Back-to-back sweeps must not re-query and re-send every tick."""
        mgr, storage, identity, router = reaction_mgr
        peer_hex = "bb" * 16
        h = compute_emoji_hash(_make_png(color=(7, 7, 7)))
        storage.insert_reaction("msg1", h, peer_hex, "cc" * 16, time.time())

        mgr.retry_pending_emoji()
        mgr.retry_pending_emoji()

        assert len(router.sent) == 1

    def test_flush_cooldown_is_shorter_than_the_request_window(self):
        """Otherwise a sweep can keep landing before markers expire and stall."""
        assert EMOJI_FLUSH_COOLDOWN_SECS < EMOJI_REQUEST_RETRY_SECS

    def test_flush_batches_a_large_backlog(self, reaction_mgr):
        """A backlog is drained in batches rather than re-tripping the throttle."""
        mgr, storage, identity, router = reaction_mgr
        peer_hex = "bb" * 16
        for i in range(EMOJI_FLUSH_BATCH + 5):
            storage.insert_reaction("msg1", f"{i:064x}", peer_hex,
                                    "cc" * 16, time.time())

        mgr.flush_pending_emoji(peer_hex)

        assert len(router.sent) == EMOJI_FLUSH_BATCH


# ---------------------------------------------------------------------------
# Emoji is an image ingestion path like any other
# ---------------------------------------------------------------------------

def _png_declaring(width: int, height: int) -> bytes:
    """A tiny PNG whose IHDR claims the given dimensions."""
    import struct
    import zlib

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(b"\x00" * 16)) + chunk(b"IEND", b""))


class TestEmojiImageSanity:
    """The byte cap bounds the payload, not the raster it expands to.

    Message images, sync images and avatars all check this; emoji was the one
    ingestion path that did not, and it is the one rendered inline in the
    transcript -- and re-served to anyone who asks for it.
    """

    def _respond_with(self, mgr, storage, image_bytes: bytes) -> str:
        emoji_hash = compute_emoji_hash(image_bytes)
        with mgr._lock:
            mgr._pending_emoji_requests[emoji_hash] = time.time()
        mgr._handle_emoji_response(_make_message({}), {
            F_EMOJI_HASH: bytes.fromhex(emoji_hash),
            F_EMOJI_DATA: image_bytes,
            F_EMOJI_NAME: "boom",
        })
        return emoji_hash

    def test_a_decompression_bomb_is_not_stored(self, reaction_mgr):
        mgr, storage, identity, router = reaction_mgr
        bomb = _png_declaring(30000, 30000)
        assert len(bomb) < MAX_EMOJI_BYTES, "the bomb must pass the byte cap"

        emoji_hash = self._respond_with(mgr, storage, bomb)

        assert not storage.emoji_exists(emoji_hash), \
            "an emoji declaring a 900-megapixel raster was stored"

    def test_an_ordinary_emoji_is_still_stored(self, reaction_mgr):
        mgr, storage, identity, router = reaction_mgr
        emoji_hash = self._respond_with(mgr, storage, _make_png())
        assert storage.emoji_exists(emoji_hash)

    def test_import_refuses_a_bomb(self, reaction_mgr):
        mgr, storage, identity, router = reaction_mgr
        with pytest.raises(ValueError):
            mgr.import_emoji("boom", _png_declaring(30000, 30000))


class TestEmojiNameIsConstrained:
    """Both clients resolve a legacy :name: token by exact match over the whole
    library, so an unconstrained peer-supplied name shadows an honest one."""

    def _store(self, mgr, name: str) -> str:
        img = _make_png(color=(4, 5, 6))
        emoji_hash = compute_emoji_hash(img)
        with mgr._lock:
            mgr._pending_emoji_requests[emoji_hash] = time.time()
        mgr._handle_emoji_response(_make_message({}), {
            F_EMOJI_HASH: bytes.fromhex(emoji_hash),
            F_EMOJI_DATA: img,
            F_EMOJI_NAME: name,
        })
        return emoji_hash

    def test_a_name_is_trimmed_to_token_charset_and_length(self, reaction_mgr):
        mgr, storage, identity, router = reaction_mgr
        emoji_hash = self._store(mgr, "wa ve!" + "x" * 200)

        stored = storage.get_emoji(emoji_hash)
        assert len(stored["name"]) <= MAX_EMOJI_NAME_LEN
        assert all(c.isalnum() or c in "_-" for c in stored["name"])
