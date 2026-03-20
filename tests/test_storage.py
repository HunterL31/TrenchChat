"""
Unit tests for trenchchat.core.storage.Storage.

These tests exercise the database layer directly with no networking.
Each test gets its own in-memory SQLite database via a tmp_path fixture.
"""

import time
from pathlib import Path

import pytest

from trenchchat.core.storage import Storage


@pytest.fixture
def db(tmp_path) -> Storage:
    """Fresh Storage instance backed by a temp file for each test."""
    s = Storage(db_path=tmp_path / "test.db")
    yield s
    s.close()


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------

class TestChannels:
    def test_upsert_and_get_channel(self, db):
        db.upsert_channel("aabbcc", "General", "A test channel",
                          "creator01", "public", time.time())
        ch = db.get_channel("aabbcc")
        assert ch is not None
        assert ch["name"] == "General"
        assert ch["description"] == "A test channel"
        assert ch["creator_hash"] == "creator01"
        assert ch["access_mode"] == "public"

    def test_upsert_updates_existing(self, db):
        ts = time.time()
        db.upsert_channel("aabbcc", "Old Name", "", "creator01", "public", ts)
        db.upsert_channel("aabbcc", "New Name", "Updated", "creator01", "public", ts)
        ch = db.get_channel("aabbcc")
        assert ch["name"] == "New Name"
        assert ch["description"] == "Updated"

    def test_get_channel_missing(self, db):
        assert db.get_channel("nonexistent") is None

    def test_get_all_channels_empty(self, db):
        assert db.get_all_channels() == []

    def test_get_all_channels_ordered_by_name(self, db):
        ts = time.time()
        db.upsert_channel("h1", "Zebra", "", "c1", "public", ts)
        db.upsert_channel("h2", "Alpha", "", "c1", "public", ts)
        db.upsert_channel("h3", "Mango", "", "c1", "public", ts)
        names = [r["name"] for r in db.get_all_channels()]
        assert names == ["Alpha", "Mango", "Zebra"]

    def test_touch_channel_updates_last_seen(self, db):
        db.upsert_channel("aabbcc", "G", "", "c1", "public", time.time())
        before = db.get_channel("aabbcc")["last_seen"]
        time.sleep(0.05)
        db.touch_channel("aabbcc")
        after = db.get_channel("aabbcc")["last_seen"]
        assert after >= before

    def test_touch_channel_missing_is_noop(self, db):
        db.touch_channel("doesnotexist")  # should not raise


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

class TestMessages:
    def _seed_channel(self, db):
        db.upsert_channel("ch01", "Test", "", "creator", "public", time.time())

    def test_insert_and_retrieve_message(self, db):
        self._seed_channel(db)
        ts = time.time()
        db.insert_message("ch01", "sender01", "Alice", "Hello", ts, "msgid1",
                          None, None, ts)
        msgs = db.get_messages("ch01")
        assert len(msgs) == 1
        assert msgs[0]["content"] == "Hello"
        assert msgs[0]["sender_name"] == "Alice"
        assert msgs[0]["message_id"] == "msgid1"

    def test_insert_duplicate_returns_false(self, db):
        self._seed_channel(db)
        ts = time.time()
        r1 = db.insert_message("ch01", "s", "S", "Hi", ts, "dup", None, None, ts)
        r2 = db.insert_message("ch01", "s", "S", "Hi", ts, "dup", None, None, ts)
        assert r1 is True
        assert r2 is False
        assert len(db.get_messages("ch01")) == 1

    def test_message_exists(self, db):
        self._seed_channel(db)
        ts = time.time()
        db.insert_message("ch01", "s", "S", "Hi", ts, "exists01", None, None, ts)
        assert db.message_exists("exists01") is True
        assert db.message_exists("nope") is False

    def test_get_messages_limit(self, db):
        self._seed_channel(db)
        for i in range(10):
            ts = time.time() + i
            db.insert_message("ch01", "s", "S", f"msg{i}", ts, f"id{i}", None, None, ts)
        assert len(db.get_messages("ch01", limit=5)) == 5

    def test_get_messages_before_ts(self, db):
        self._seed_channel(db)
        base = time.time()
        for i in range(5):
            ts = base + i
            db.insert_message("ch01", "s", "S", f"msg{i}", ts, f"id{i}", None, None, ts)
        # Only messages with timestamp < base+3
        msgs = db.get_messages("ch01", before_ts=base + 3)
        assert all(m["timestamp"] < base + 3 for m in msgs)
        assert len(msgs) == 3

    def test_get_latest_message_id(self, db):
        self._seed_channel(db)
        base = time.time()
        db.insert_message("ch01", "s", "S", "first", base, "first_id", None, None, base)
        db.insert_message("ch01", "s", "S", "last", base + 1, "last_id", None, None, base + 1)
        assert db.get_latest_message_id("ch01") == "last_id"

    def test_get_latest_message_id_empty(self, db):
        self._seed_channel(db)
        assert db.get_latest_message_id("ch01") is None

    def test_get_messages_after(self, db):
        self._seed_channel(db)
        base = time.time()
        for i in range(5):
            ts = base + i
            db.insert_message("ch01", "s", "S", f"msg{i}", ts, f"id{i}", None, None, ts)
        msgs = db.get_messages_after("ch01", base + 2)
        assert len(msgs) == 2
        assert all(m["timestamp"] > base + 2 for m in msgs)

    def test_reply_to_stored(self, db):
        self._seed_channel(db)
        ts = time.time()
        db.insert_message("ch01", "s", "S", "original", ts, "orig", None, None, ts)
        db.insert_message("ch01", "s2", "B", "reply", ts + 1, "rep", "orig", None, ts + 1)
        msgs = db.get_messages("ch01")
        reply = next(m for m in msgs if m["message_id"] == "rep")
        assert reply["reply_to"] == "orig"


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------

class TestSubscriptions:
    def _seed_channel(self, db):
        db.upsert_channel("ch01", "Test", "", "creator", "public", time.time())

    def test_subscribe_and_is_subscribed(self, db):
        self._seed_channel(db)
        assert db.is_subscribed("ch01") is False
        db.subscribe("ch01")
        assert db.is_subscribed("ch01") is True

    def test_unsubscribe(self, db):
        self._seed_channel(db)
        db.subscribe("ch01")
        db.unsubscribe("ch01")
        assert db.is_subscribed("ch01") is False

    def test_subscribe_idempotent(self, db):
        self._seed_channel(db)
        db.subscribe("ch01")
        db.subscribe("ch01")
        assert len(db.get_subscriptions()) == 1

    def test_get_subscriptions(self, db):
        for i in range(3):
            db.upsert_channel(f"ch0{i}", f"Ch{i}", "", "c", "public", time.time())
            db.subscribe(f"ch0{i}")
        subs = db.get_subscriptions()
        assert len(subs) == 3

    def test_update_last_sync(self, db):
        self._seed_channel(db)
        db.subscribe("ch01")
        before = db.get_subscriptions()[0]["last_sync_at"]
        time.sleep(0.05)
        db.update_last_sync("ch01")
        after = db.get_subscriptions()[0]["last_sync_at"]
        assert after > before


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------

class TestMembers:
    def _seed_channel(self, db):
        db.upsert_channel("ch01", "Test", "", "creator", "invite", time.time())

    def test_upsert_and_is_member(self, db):
        self._seed_channel(db)
        assert db.is_member("ch01", "alice") is False
        db.upsert_member("ch01", "alice", "Alice", is_admin=False)
        assert db.is_member("ch01", "alice") is True

    def test_is_admin(self, db):
        self._seed_channel(db)
        db.upsert_member("ch01", "alice", "Alice", is_admin=True)
        db.upsert_member("ch01", "bob", "Bob", is_admin=False)
        assert db.is_admin("ch01", "alice") is True
        assert db.is_admin("ch01", "bob") is False

    def test_remove_member(self, db):
        self._seed_channel(db)
        db.upsert_member("ch01", "alice", "Alice", is_admin=False)
        db.remove_member("ch01", "alice")
        assert db.is_member("ch01", "alice") is False

    def test_get_members(self, db):
        self._seed_channel(db)
        db.upsert_member("ch01", "alice", "Alice", is_admin=True)
        db.upsert_member("ch01", "bob", "Bob", is_admin=False)
        members = db.get_members("ch01")
        assert len(members) == 2
        hashes = {m["identity_hash"] for m in members}
        assert hashes == {"alice", "bob"}

    def test_replace_members(self, db):
        self._seed_channel(db)
        db.upsert_member("ch01", "alice", "Alice", is_admin=True)
        db.upsert_member("ch01", "bob", "Bob", is_admin=False)
        db.replace_members("ch01", [("carol", "Carol", True)])
        members = db.get_members("ch01")
        assert len(members) == 1
        assert members[0]["identity_hash"] == "carol"

    def test_upsert_member_updates_existing(self, db):
        self._seed_channel(db)
        db.upsert_member("ch01", "alice", "Alice", is_admin=False)
        db.upsert_member("ch01", "alice", "Alice Admin", is_admin=True)
        assert db.is_admin("ch01", "alice") is True
        members = db.get_members("ch01")
        assert len(members) == 1


# ---------------------------------------------------------------------------
# Member list versions
# ---------------------------------------------------------------------------

class TestMemberListVersions:
    def _seed_channel(self, db):
        db.upsert_channel("ch01", "Test", "", "creator", "invite", time.time())

    def test_upsert_and_get_version(self, db):
        self._seed_channel(db)
        assert db.get_member_list_version("ch01") is None
        db.upsert_member_list_version("ch01", 1, time.time(), b"blob1")
        row = db.get_member_list_version("ch01")
        assert row is not None
        assert row["version"] == 1
        assert row["document_blob"] == b"blob1"

    def test_upsert_replaces_existing(self, db):
        self._seed_channel(db)
        db.upsert_member_list_version("ch01", 1, 1000.0, b"v1")
        db.upsert_member_list_version("ch01", 2, 2000.0, b"v2")
        row = db.get_member_list_version("ch01")
        assert row["version"] == 2
        assert row["document_blob"] == b"v2"


# ---------------------------------------------------------------------------
# Missed deliveries
# ---------------------------------------------------------------------------

class TestMissedDeliveries:
    def test_record_and_get(self, db):
        db.record_missed_delivery("ch01", "bob", "msg01")
        ids = db.get_missed_message_ids("ch01", "bob")
        assert "msg01" in ids

    def test_record_idempotent(self, db):
        db.record_missed_delivery("ch01", "bob", "msg01")
        db.record_missed_delivery("ch01", "bob", "msg01")
        assert db.get_missed_message_ids("ch01", "bob").count("msg01") == 1

    def test_clear_missed_deliveries(self, db):
        db.record_missed_delivery("ch01", "bob", "msg01")
        db.record_missed_delivery("ch01", "bob", "msg02")
        db.clear_missed_deliveries("ch01", "bob")
        assert db.get_missed_message_ids("ch01", "bob") == []

    def test_clear_only_affects_recipient(self, db):
        db.record_missed_delivery("ch01", "bob", "msg01")
        db.record_missed_delivery("ch01", "carol", "msg01")
        db.clear_missed_deliveries("ch01", "bob")
        assert db.get_missed_message_ids("ch01", "carol") == ["msg01"]

    def test_purge_old_missed_deliveries(self, db):
        db.record_missed_delivery("ch01", "bob", "old_msg")
        time.sleep(0.05)
        cutoff = time.time()
        db.record_missed_delivery("ch01", "bob", "new_msg")
        db.purge_old_missed_deliveries(cutoff)
        ids = db.get_missed_message_ids("ch01", "bob")
        assert "old_msg" not in ids
        assert "new_msg" in ids
