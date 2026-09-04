"""
Unit tests for trenchchat.core.storage.Storage.

These tests exercise the database layer directly with no networking.
Each test gets its own in-memory SQLite database via a tmp_path fixture.
"""

import os
import stat
import time
from pathlib import Path

import pytest

from trenchchat.core.fileutils import OWNER_RW_MODE
from trenchchat.core.permissions import (
    CREATE_CHANNEL, INVITE, PRESET_PRIVATE, PRESET_SERVER, ROLE_ADMIN,
    ROLE_MEMBER, ROLE_OWNER, SEND_MESSAGE,
    is_discoverable, is_open_join, permissions_from_json,
)
from trenchchat.core.storage import Storage
from trenchchat.core.lockbox import sqlcipher_hex_key


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
        import json
        perms = json.loads(ch["permissions"])
        assert perms["open_join"] is True

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

    def test_default_fetch_returns_the_newest_page_not_the_oldest(self, db):
        """A channel with more than *limit* messages must not freeze on its
        oldest page: the default fetch returns the newest *limit*, ascending."""
        self._seed_channel(db)
        base = time.time()
        for i in range(250):
            ts = base + i
            db.insert_message("ch01", "s", "S", f"msg{i}", ts, f"id{i:03d}",
                              None, None, ts)

        page = db.get_messages("ch01")
        assert len(page) == 200
        # The newest 200 are ids 50..249, returned oldest-first for display.
        assert page[0]["message_id"] == "id050"
        assert page[-1]["message_id"] == "id249"
        assert [m["message_id"] for m in page] == sorted(m["message_id"] for m in page)

    def test_before_ts_returns_the_older_page_from_the_newest_end(self, db):
        """Paging back with before_ts fetches the messages just older than the
        oldest one shown, still selected from the newest end and ascending."""
        self._seed_channel(db)
        base = time.time()
        for i in range(250):
            ts = base + i
            db.insert_message("ch01", "s", "S", f"msg{i}", ts, f"id{i:03d}",
                              None, None, ts)

        first_page = db.get_messages("ch01")           # ids 50..249
        oldest_shown = first_page[0]["timestamp"]      # ts of id050
        older = db.get_messages("ch01", limit=30, before_ts=oldest_shown)
        assert len(older) == 30
        # The 30 immediately older than id050 are ids 20..49.
        assert older[0]["message_id"] == "id020"
        assert older[-1]["message_id"] == "id049"

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

    def test_get_message_index_covers_the_half_open_range(self, db):
        self._seed_channel(db)
        base = time.time()
        for i in range(5):
            ts = base + i
            db.insert_message("ch01", "s", "S", f"msg{i}", ts, f"id{i}", None, None, ts)
        index = db.get_message_index("ch01", base + 1, base + 4)
        assert [r["message_id"] for r in index] == ["id1", "id2", "id3"]
        assert [r["timestamp"] for r in index] == [base + 1, base + 2, base + 3]
        assert all(r["sender_hash"] == "s" for r in index)

    def test_get_message_index_reports_whether_a_row_is_signed(self, db):
        self._seed_channel(db)
        ts = time.time()
        db.insert_message("ch01", "s", "S", "unsigned", ts, "plain", None, None, ts)
        db.insert_message("ch01", "s", "S", "signed", ts + 1, "signed", None, None,
                          ts + 1, author_sig=b"\x01" * 64)
        index = {r["message_id"]: bool(r["has_sig"])
                 for r in db.get_message_index("ch01", 0.0, ts + 10)}
        assert index == {"plain": False, "signed": True}

    def test_get_message_index_is_scoped_to_one_channel(self, db):
        self._seed_channel(db)
        db.upsert_channel("ch02", "Other", "", "creator", "public", time.time())
        ts = time.time()
        db.insert_message("ch01", "s", "S", "here", ts, "mine", None, None, ts)
        db.insert_message("ch02", "s", "S", "there", ts, "theirs", None, None, ts)
        assert [r["message_id"] for r in db.get_message_index("ch01", 0.0, ts + 10)] \
            == ["mine"]

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

    def test_get_last_sync(self, db):
        self._seed_channel(db)
        db.subscribe("ch01")
        assert db.get_last_sync("ch01") == 0.0

        watermark = time.time()
        db.update_last_sync("ch01", watermark)
        assert db.get_last_sync("ch01") == pytest.approx(watermark)

    def test_get_last_sync_for_unsubscribed_channel(self, db):
        assert db.get_last_sync("nope") == 0.0


class TestChannelUnread:
    def _seed(self, db):
        db.upsert_channel("ch01", "Test", "", "creator", "public", time.time())
        db.subscribe("ch01")

    def _insert(self, db, sender, msg_id, ts):
        db.insert_message("ch01", sender, sender, "hi", ts, msg_id, None, None, ts)

    def test_unread_counts_only_others_messages(self, db):
        self._seed(db)
        now = time.time()
        self._insert(db, "peer", "m1", now - 10)
        self._insert(db, "peer", "m2", now - 5)
        self._insert(db, "me0000", "m3", now - 3)
        assert db.get_unread_counts("me0000") == {"ch01": 2}

    def test_mark_read_resets_the_count(self, db):
        self._seed(db)
        now = time.time()
        self._insert(db, "peer", "m1", now - 10)
        assert db.mark_channel_read("ch01") is True
        assert db.get_unread_counts("me0000") == {"ch01": 0}

    def test_messages_after_the_watermark_count_again(self, db):
        self._seed(db)
        now = time.time()
        db.mark_channel_read("ch01", ts=now - 7)
        self._insert(db, "peer", "m1", now - 10)
        self._insert(db, "peer", "m2", now - 5)
        assert db.get_unread_counts("me0000") == {"ch01": 1}

    def test_a_channel_with_no_messages_reads_zero(self, db):
        self._seed(db)
        assert db.get_unread_counts("me0000") == {"ch01": 0}

    def test_unsubscribed_channels_are_absent(self, db):
        db.upsert_channel("ch02", "Other", "", "creator", "public", time.time())
        assert db.get_unread_counts("me0000") == {}

    def test_mark_read_on_an_unsubscribed_channel_is_false(self, db):
        assert db.mark_channel_read("nope") is False


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


# ---------------------------------------------------------------------------
# File permissions
# ---------------------------------------------------------------------------

class TestDatabaseFilePermissions:
    def test_new_db_file_is_owner_only(self, tmp_path):
        """A freshly created database file must have owner-only permissions."""
        if os.name == "nt":
            pytest.skip("POSIX permission test not applicable on Windows")

        db_path = tmp_path / "storage.db"
        s = Storage(db_path=db_path)
        s.close()

        mode = stat.S_IMODE(os.stat(db_path).st_mode)
        assert mode == OWNER_RW_MODE

    def test_existing_permissive_db_file_is_hardened(self, tmp_path):
        """An existing DB file with loose permissions is tightened on open."""
        if os.name == "nt":
            pytest.skip("POSIX permission test not applicable on Windows")

        db_path = tmp_path / "storage.db"

        # Create with default permissions first.
        s = Storage(db_path=db_path)
        s.close()

        # Loosen them to simulate a pre-existing installation.
        os.chmod(db_path, 0o644)
        assert stat.S_IMODE(os.stat(db_path).st_mode) == 0o644

        # Re-opening must harden the file.
        s2 = Storage(db_path=db_path)
        s2.close()
        assert stat.S_IMODE(os.stat(db_path).st_mode) == OWNER_RW_MODE

    def test_wal_sidecar_is_secured_if_present(self, tmp_path):
        """The -wal sidecar file is also locked down when it exists."""
        if os.name == "nt":
            pytest.skip("POSIX permission test not applicable on Windows")

        db_path = tmp_path / "storage.db"
        wal_path = tmp_path / "storage.db-wal"

        s = Storage(db_path=db_path)
        # Force a checkpoint so WAL is flushed and sidecar exists.
        s._conn.execute("PRAGMA wal_checkpoint(FULL)")
        s.close()

        if not wal_path.exists():
            pytest.skip("WAL sidecar not present after checkpoint on this platform")

        os.chmod(wal_path, 0o644)

        s2 = Storage(db_path=db_path)
        s2.close()
        assert stat.S_IMODE(os.stat(wal_path).st_mode) == OWNER_RW_MODE


# ---------------------------------------------------------------------------
# SQLCipher encryption
# ---------------------------------------------------------------------------

class TestStorageEncryption:
    """Tests for Storage encrypted (SQLCipher) mode."""

    def test_encrypted_db_opens_and_accepts_writes(self, tmp_path):
        """An encrypted Storage instance can write and read data."""
        key = os.urandom(32)
        db_path = tmp_path / "enc.db"
        s = Storage(db_path=db_path, encryption_key=key)
        try:
            s.upsert_channel("aabb", "Enc Chan", "", "creator01", "public", time.time())
            ch = s.get_channel("aabb")
            assert ch is not None
            assert ch["name"] == "Enc Chan"
        finally:
            s.close()

    def test_encrypted_db_is_not_readable_as_plain_sqlite(self, tmp_path):
        """A SQLCipher-encrypted file must not be openable via plain sqlite3."""
        import sqlite3 as _sqlite3

        key = os.urandom(32)
        db_path = tmp_path / "enc.db"
        s = Storage(db_path=db_path, encryption_key=key)
        s.upsert_channel("aabb", "Secret", "", "c1", "public", time.time())
        s.close()

        # Attempting to query via plain sqlite3 should raise DatabaseError
        # (the file header is encrypted).
        conn = _sqlite3.connect(str(db_path))
        with pytest.raises(_sqlite3.DatabaseError):
            conn.execute("SELECT * FROM channels").fetchall()
        conn.close()

    def test_wrong_key_raises_on_open(self, tmp_path):
        """Opening a SQLCipher DB with the wrong key raises an error."""
        import sqlcipher3.dbapi2 as _sqlcipher  # type: ignore[import]

        key = os.urandom(32)
        wrong_key = os.urandom(32)
        db_path = tmp_path / "enc.db"

        s = Storage(db_path=db_path, encryption_key=key)
        s.upsert_channel("aabb", "Hidden", "", "c1", "public", time.time())
        s.close()

        with pytest.raises(Exception):
            bad = Storage(db_path=db_path, encryption_key=wrong_key)
            # Force a read to trigger the decryption error.
            bad.get_all_channels()
            bad.close()

    def test_encrypt_database_migration(self, tmp_path):
        """encrypt_database converts a plain DB to SQLCipher in-place."""
        db_path = tmp_path / "plain.db"
        key = os.urandom(32)

        # Create plain DB and write a record.
        s = Storage(db_path=db_path)
        s.upsert_channel("cc11", "MigChan", "", "creator", "public", time.time())
        s.close()

        # Migrate to encrypted using a temporary helper instance.
        helper = Storage.__new__(Storage)
        helper.encrypt_database(new_key=key, db_path=db_path)

        # Re-open with the key and verify data survived.
        s2 = Storage(db_path=db_path, encryption_key=key)
        ch = s2.get_channel("cc11")
        s2.close()
        assert ch is not None
        assert ch["name"] == "MigChan"

    def test_decrypt_database_migration(self, tmp_path):
        """decrypt_database converts a SQLCipher DB back to plaintext in-place."""
        db_path = tmp_path / "enc.db"
        key = os.urandom(32)

        # Create encrypted DB.
        s = Storage(db_path=db_path, encryption_key=key)
        s.upsert_channel("dd22", "DecChan", "", "creator", "public", time.time())
        s.close()

        # Migrate to plaintext using a temporary helper instance.
        helper = Storage.__new__(Storage)
        helper.decrypt_database(current_key=key, db_path=db_path)

        # Re-open without a key and verify data survived.
        s2 = Storage(db_path=db_path)
        ch = s2.get_channel("dd22")
        s2.close()
        assert ch is not None
        assert ch["name"] == "DecChan"


# ---------------------------------------------------------------------------
# Membership tenure
# ---------------------------------------------------------------------------

CHAN = "aabbccddeeff0011"
ID_A = "aaaaaaaaaaaaaaaa"
ID_B = "bbbbbbbbbbbbbbbb"


class TestMembershipTenure:
    def test_open_tenure_makes_member_at_timestamp(self, db):
        t0 = 1_000_000.0
        db.open_tenure(CHAN, ID_A, t0)
        assert db.was_member_at(CHAN, ID_A, t0)
        assert db.was_member_at(CHAN, ID_A, t0 + 100)

    def test_was_member_before_join_returns_false(self, db):
        t0 = 1_000_000.0
        db.open_tenure(CHAN, ID_A, t0)
        assert not db.was_member_at(CHAN, ID_A, t0 - 1)

    def test_close_tenure_excludes_timestamp_at_or_after_left_at(self, db):
        t0 = 1_000_000.0
        t1 = t0 + 500.0
        db.open_tenure(CHAN, ID_A, t0)
        db.close_tenure(CHAN, ID_A, t1)
        assert db.was_member_at(CHAN, ID_A, t0)
        assert db.was_member_at(CHAN, ID_A, t1 - 1)
        assert not db.was_member_at(CHAN, ID_A, t1)
        assert not db.was_member_at(CHAN, ID_A, t1 + 1)

    def test_gap_after_kick_and_before_rejoin(self, db):
        t0 = 1_000_000.0
        kick = t0 + 300.0
        rejoin = t0 + 600.0
        db.open_tenure(CHAN, ID_A, t0)
        db.close_tenure(CHAN, ID_A, kick)
        db.open_tenure(CHAN, ID_A, rejoin)
        # Before kick: valid
        assert db.was_member_at(CHAN, ID_A, t0)
        assert db.was_member_at(CHAN, ID_A, kick - 1)
        # In the gap: invalid
        assert not db.was_member_at(CHAN, ID_A, kick)
        assert not db.was_member_at(CHAN, ID_A, rejoin - 1)
        # After rejoin: valid
        assert db.was_member_at(CHAN, ID_A, rejoin)
        assert db.was_member_at(CHAN, ID_A, rejoin + 1000)

    def test_update_tenure_adds_removed_and_added(self, db):
        t0 = 1_000_000.0
        t1 = t0 + 300.0
        id_c = "cccccccccccccccc"
        db.open_tenure(CHAN, ID_A, t0)
        db.open_tenure(CHAN, ID_B, t0)
        # ID_B is removed, ID_A stays, new member id_c is added
        db.update_tenure(CHAN, {ID_A, ID_B}, {ID_A, id_c}, t1)
        # A: unchanged open interval
        assert db.was_member_at(CHAN, ID_A, t1)
        # B: closed at t1
        assert db.was_member_at(CHAN, ID_B, t1 - 1)
        assert not db.was_member_at(CHAN, ID_B, t1)
        # C: new open interval from t1
        assert db.was_member_at(CHAN, id_c, t1)
        assert not db.was_member_at(CHAN, id_c, t1 - 1)

    def test_update_tenure_no_change_idempotent(self, db):
        t0 = 1_000_000.0
        t1 = t0 + 200.0
        db.open_tenure(CHAN, ID_A, t0)
        # Same set in and out, nothing changes
        db.update_tenure(CHAN, {ID_A}, {ID_A}, t1)
        assert db.was_member_at(CHAN, ID_A, t1)

    def test_close_tenure_with_no_open_interval_is_noop(self, db):
        # Should not raise
        db.close_tenure(CHAN, ID_A, 1_000_000.0)

    def test_open_tenure_idempotent(self, db):
        t0 = 1_000_000.0
        db.open_tenure(CHAN, ID_A, t0)
        db.open_tenure(CHAN, ID_A, t0)  # duplicate: ignored
        # Should still be a member
        assert db.was_member_at(CHAN, ID_A, t0)

    def test_has_any_tenure_empty(self, db):
        assert not db.has_any_tenure(CHAN)

    def test_has_any_tenure_after_open(self, db):
        db.open_tenure(CHAN, ID_A, 1_000_000.0)
        assert db.has_any_tenure(CHAN)

    def test_was_member_at_unknown_identity_returns_false(self, db):
        db.open_tenure(CHAN, ID_A, 1_000_000.0)
        assert not db.was_member_at(CHAN, ID_B, 1_000_000.0)

    def test_get_departed_within_returns_recent_closed_interval(self, db):
        t0 = 1_000_000.0
        left = t0 + 500.0
        db.open_tenure(CHAN, ID_A, t0)
        db.close_tenure(CHAN, ID_A, left)
        rows = db.get_departed_within(CHAN, left - 1)
        assert len(rows) == 1
        assert rows[0]["identity_hash"] == ID_A
        assert rows[0]["joined_at"] == t0
        assert rows[0]["left_at"] == left

    def test_get_departed_within_excludes_interval_closed_before_cutoff(self, db):
        t0 = 1_000_000.0
        left = t0 + 500.0
        db.open_tenure(CHAN, ID_A, t0)
        db.close_tenure(CHAN, ID_A, left)
        assert db.get_departed_within(CHAN, left + 1) == []

    def test_get_departed_within_excludes_open_intervals(self, db):
        db.open_tenure(CHAN, ID_A, 1_000_000.0)
        assert db.get_departed_within(CHAN, 0.0) == []

    def test_record_departed_tenure_adds_interval(self, db):
        db.record_departed_tenure(CHAN, ID_A, 1_000_000.0, 1_000_500.0)
        assert db.was_member_at(CHAN, ID_A, 1_000_200.0)
        assert not db.was_member_at(CHAN, ID_A, 1_000_600.0)

    def test_record_departed_tenure_does_not_overwrite_existing(self, db):
        db.open_tenure(CHAN, ID_A, 1_000_000.0)
        db.close_tenure(CHAN, ID_A, 1_000_500.0)
        # A conflicting claim for the same (channel, identity, joined_at) is ignored
        db.record_departed_tenure(CHAN, ID_A, 1_000_000.0, 1_000_999.0)
        row = db._conn.execute(
            "SELECT left_at FROM membership_tenure "
            "WHERE channel_hash=? AND identity_hash=? AND joined_at=?",
            (CHAN, ID_A, 1_000_000.0)
        ).fetchone()
        assert row["left_at"] == 1_000_500.0

    def test_backfill_from_members_table(self, tmp_path):
        """Existing members are backfilled into tenure on first open."""
        db = Storage(db_path=tmp_path / "bf.db")
        # Manually insert a member row, bypassing tenure so we can test backfill
        db.upsert_channel(CHAN, "Test", "", "creator", "invite", time.time())
        t0 = time.time() - 10
        db._conn.execute(
            "INSERT INTO members (channel_hash, identity_hash, display_name, role, added_at)"
            " VALUES (?, ?, '', 'member', ?)",
            (CHAN, ID_A, t0)
        )
        db._conn.commit()
        # Clear tenure so _migrate_tenure will backfill
        db._conn.execute("DELETE FROM membership_tenure")
        db._conn.commit()
        db._migrate_tenure()
        assert db.was_member_at(CHAN, ID_A, t0 + 1)
        db.close()

    def test_repair_tenure_widens_interval_to_earlier_stored_message(self, db):
        """A joined_at backfilled too late (e.g. from a stale
        members.added_at) is widened to cover a message already on file
        that predates it."""
        db.upsert_channel(CHAN, "Test", "", "creator", "invite", time.time())
        t0 = 1_000_000.0
        bad_joined_at = t0 + 500.0
        db.open_tenure(CHAN, ID_A, bad_joined_at)
        db.insert_message(
            channel_hash=CHAN, sender_hash=ID_A, sender_name="A",
            content="hi", timestamp=t0, message_id="repair-m1",
            reply_to=None, last_seen_id=None, received_at=t0,
        )
        assert not db.was_member_at(CHAN, ID_A, t0)

        db._repair_tenure_from_message_history()

        assert db.was_member_at(CHAN, ID_A, t0)

    def test_repair_tenure_does_not_widen_when_message_is_within_interval(self, db):
        db.upsert_channel(CHAN, "Test", "", "creator", "invite", time.time())
        t0 = 1_000_000.0
        db.open_tenure(CHAN, ID_A, t0)
        db.insert_message(
            channel_hash=CHAN, sender_hash=ID_A, sender_name="A",
            content="hi", timestamp=t0 + 10, message_id="repair-m2",
            reply_to=None, last_seen_id=None, received_at=t0 + 10,
        )

        db._repair_tenure_from_message_history()

        row = db._conn.execute(
            "SELECT joined_at FROM membership_tenure WHERE channel_hash=? AND identity_hash=?",
            (CHAN, ID_A)
        ).fetchone()
        assert row["joined_at"] == t0

    def test_repair_tenure_skips_identity_with_no_tenure_record(self, db):
        db.upsert_channel(CHAN, "Test", "", "creator", "invite", time.time())
        db.insert_message(
            channel_hash=CHAN, sender_hash=ID_B, sender_name="B",
            content="hi", timestamp=1_000_000.0, message_id="repair-m3",
            reply_to=None, last_seen_id=None, received_at=1_000_000.0,
        )

        db._repair_tenure_from_message_history()

        assert not db.was_member_at(CHAN, ID_B, 1_000_000.0)

    def test_repair_tenure_runs_safely_from_init_on_existing_db(self, tmp_path):
        """Reproduces a real startup crash: the repair pass runs inside
        __init__ via _migrate_permissions, before self._lock and
        self._scope_cache existed at one point -- only visible on a database
        that already has message rows, since the repair query is a no-op on
        an empty messages table."""
        db_path = tmp_path / "existing.db"
        db1 = Storage(db_path=db_path)
        db1.upsert_channel(CHAN, "Test", "", "creator", "invite", time.time())
        t0 = 1_000_000.0
        db1.open_tenure(CHAN, ID_A, t0 + 500.0)
        db1.insert_message(
            channel_hash=CHAN, sender_hash=ID_A, sender_name="A",
            content="hi", timestamp=t0, message_id="reopen-m1",
            reply_to=None, last_seen_id=None, received_at=t0,
        )
        db1.close()

        db2 = Storage(db_path=db_path)  # must not raise on construction
        assert db2.was_member_at(CHAN, ID_A, t0)
        db2.close()


# ---------------------------------------------------------------------------
# Peer avatars
# ---------------------------------------------------------------------------

class TestPeerAvatars:
    def test_upsert_and_get_peer_avatar(self, db):
        peer = "aa" * 16
        data = b"\xff\xd8\xff\xe0" + b"\x00" * 50   # fake JPEG header
        db.upsert_peer_avatar(peer, data, avatar_version=1)
        row = db.get_peer_avatar(peer)
        assert row is not None
        assert bytes(row["avatar_data"]) == data
        assert row["avatar_version"] == 1
        assert row["identity_hash"] == peer

    def test_upsert_peer_avatar_updates_existing(self, db):
        peer = "bb" * 16
        db.upsert_peer_avatar(peer, b"old", avatar_version=1)
        db.upsert_peer_avatar(peer, b"new", avatar_version=2)
        row = db.get_peer_avatar(peer)
        assert bytes(row["avatar_data"]) == b"new"
        assert row["avatar_version"] == 2

    def test_get_peer_avatar_missing_returns_none(self, db):
        assert db.get_peer_avatar("cc" * 16) is None

    def test_delete_peer_avatar(self, db):
        peer = "dd" * 16
        db.upsert_peer_avatar(peer, b"data", avatar_version=1)
        db.delete_peer_avatar(peer)
        assert db.get_peer_avatar(peer) is None


# ---------------------------------------------------------------------------
# Avatar delivery tracking
# ---------------------------------------------------------------------------

class TestAvatarDeliveryTracking:
    def test_upsert_and_get_delivery_version(self, db):
        peer = "ee" * 16
        db.upsert_avatar_delivery(peer, avatar_version=3)
        assert db.get_avatar_delivery_version(peer) == 3

    def test_upsert_delivery_updates_existing(self, db):
        peer = "ff" * 16
        db.upsert_avatar_delivery(peer, avatar_version=1)
        db.upsert_avatar_delivery(peer, avatar_version=5)
        assert db.get_avatar_delivery_version(peer) == 5

    def test_get_delivery_version_missing_returns_none(self, db):
        assert db.get_avatar_delivery_version("11" * 16) is None

    def test_clear_avatar_deliveries(self, db):
        db.upsert_avatar_delivery("22" * 16, avatar_version=1)
        db.upsert_avatar_delivery("33" * 16, avatar_version=2)
        db.clear_avatar_deliveries()
        assert db.get_avatar_delivery_version("22" * 16) is None
        assert db.get_avatar_delivery_version("33" * 16) is None


# ---------------------------------------------------------------------------
# Image data in messages
# ---------------------------------------------------------------------------

class TestMessageImageData:
    _CHAN = "aa" * 16
    _SENDER = "bb" * 16
    _FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 100

    def _setup_channel(self, db):
        db.upsert_channel(self._CHAN, "Test", "", self._SENDER, "public", 1.0)
        db.subscribe(self._CHAN)

    def test_insert_message_with_image_stores_blob(self, db):
        """image_data is persisted and retrievable via get_messages."""
        self._setup_channel(db)
        ts = 1000.0
        db.insert_message(
            channel_hash=self._CHAN,
            sender_hash=self._SENDER,
            sender_name="Alice",
            content="Look at this",
            timestamp=ts,
            message_id="img_msg_001",
            reply_to=None,
            last_seen_id=None,
            received_at=ts,
            image_data=self._FAKE_JPEG,
        )
        msgs = db.get_messages(self._CHAN)
        assert len(msgs) == 1
        assert bytes(msgs[0]["image_data"]) == self._FAKE_JPEG

    def test_insert_message_without_image_is_null(self, db):
        """Messages without image_data have a NULL image_data column."""
        self._setup_channel(db)
        ts = 1001.0
        db.insert_message(
            channel_hash=self._CHAN,
            sender_hash=self._SENDER,
            sender_name="Alice",
            content="No image",
            timestamp=ts,
            message_id="txt_msg_001",
            reply_to=None,
            last_seen_id=None,
            received_at=ts,
        )
        msgs = db.get_messages(self._CHAN)
        assert len(msgs) == 1
        assert msgs[0]["image_data"] is None

    def test_image_only_message_has_empty_content(self, db):
        """An image-only message stores empty text and non-null image_data."""
        self._setup_channel(db)
        ts = 1002.0
        db.insert_message(
            channel_hash=self._CHAN,
            sender_hash=self._SENDER,
            sender_name="Alice",
            content="",
            timestamp=ts,
            message_id="img_only_001",
            reply_to=None,
            last_seen_id=None,
            received_at=ts,
            image_data=self._FAKE_JPEG,
        )
        msgs = db.get_messages(self._CHAN)
        assert len(msgs) == 1
        assert msgs[0]["content"] == ""
        assert bytes(msgs[0]["image_data"]) == self._FAKE_JPEG

    def test_schema_migration_adds_image_data_column(self, tmp_path):
        """_migrate_image_data() adds image_data to a database that lacks it."""
        import sqlite3
        db_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_hash TEXT NOT NULL,
                sender_hash TEXT NOT NULL,
                sender_name TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                timestamp REAL NOT NULL,
                message_id TEXT NOT NULL UNIQUE,
                reply_to TEXT,
                last_seen_id TEXT,
                received_at REAL NOT NULL
            )
        """)
        conn.commit()
        conn.close()

        s = Storage(db_path=db_path)
        cols = [c["name"] for c in s._conn.execute("PRAGMA table_info(messages)").fetchall()]
        assert "image_data" in cols
        s.close()


# ---------------------------------------------------------------------------
# Servers and scope resolution
# ---------------------------------------------------------------------------

SERVER_H = "aa" * 16
CHAN_IN_SERVER = "bb" * 16
STANDALONE = "cc" * 16
ALICE = "11" * 16
BOB = "22" * 16


def _make_server_with_channel(db):
    """A server owning one channel, plus one unrelated standalone channel."""
    db.upsert_server(SERVER_H, "My Server", "desc", ALICE,
                     PRESET_SERVER, time.time())
    db.upsert_channel(CHAN_IN_SERVER, "general", "", ALICE,
                      PRESET_SERVER, time.time(), server_hash=SERVER_H)
    db.upsert_channel(STANDALONE, "solo", "", BOB, PRESET_PRIVATE, time.time())


class TestServersCRUD:
    def test_upsert_and_get_server(self, db):
        db.upsert_server(SERVER_H, "My Server", "desc", ALICE,
                         PRESET_SERVER, 123.0)
        row = db.get_server(SERVER_H)
        assert row["name"] == "My Server"
        assert row["creator_hash"] == ALICE
        assert row["created_at"] == 123.0

    def test_get_server_missing(self, db):
        assert db.get_server(SERVER_H) is None

    def test_is_server(self, db):
        db.upsert_server(SERVER_H, "S", "", ALICE, PRESET_SERVER, 1.0)
        db.upsert_channel(STANDALONE, "c", "", ALICE, PRESET_PRIVATE, 1.0)
        assert db.is_server(SERVER_H) is True
        assert db.is_server(STANDALONE) is False

    def test_get_all_servers_excludes_channels(self, db):
        _make_server_with_channel(db)
        hashes = [r["hash"] for r in db.get_all_servers()]
        assert hashes == [SERVER_H]

    def test_get_all_channels_excludes_servers(self, db):
        """get_all_channels feeds the sidebar and restore_owned_channels,
        a server must never appear in it."""
        _make_server_with_channel(db)
        hashes = {r["hash"] for r in db.get_all_channels()}
        assert hashes == {CHAN_IN_SERVER, STANDALONE}

    def test_get_server_channels(self, db):
        _make_server_with_channel(db)
        assert [r["hash"] for r in db.get_server_channels(SERVER_H)] == [CHAN_IN_SERVER]

    def test_get_standalone_channels(self, db):
        _make_server_with_channel(db)
        assert [r["hash"] for r in db.get_standalone_channels()] == [STANDALONE]

    def test_get_scope_creator_hash_prefers_server(self, db):
        _make_server_with_channel(db)
        assert db.get_scope_creator_hash(SERVER_H) == ALICE
        assert db.get_scope_creator_hash(STANDALONE) == BOB
        assert db.get_scope_creator_hash("de" * 16) is None


class TestScopeResolution:
    def test_scope_of_server_channel_is_the_server(self, db):
        _make_server_with_channel(db)
        assert db.scope_for(CHAN_IN_SERVER) == SERVER_H

    def test_scope_of_standalone_channel_is_itself(self, db):
        _make_server_with_channel(db)
        assert db.scope_for(STANDALONE) == STANDALONE

    def test_scope_of_unknown_hash_is_itself(self, db):
        assert db.scope_for("ee" * 16) == "ee" * 16

    def test_role_resolves_to_server(self, db):
        _make_server_with_channel(db)
        db.upsert_member(SERVER_H, BOB, "Bob", ROLE_ADMIN)
        assert db.get_role(CHAN_IN_SERVER, BOB) == ROLE_ADMIN
        assert db.is_member(CHAN_IN_SERVER, BOB) is True
        assert db.is_admin(CHAN_IN_SERVER, BOB) is True

    def test_members_resolve_to_server(self, db):
        _make_server_with_channel(db)
        db.upsert_member(SERVER_H, ALICE, "Alice", ROLE_OWNER)
        db.upsert_member(SERVER_H, BOB, "Bob", ROLE_MEMBER)
        assert {r["identity_hash"] for r in db.get_members(CHAN_IN_SERVER)} == {ALICE, BOB}

    def test_display_name_resolves_to_server(self, db):
        _make_server_with_channel(db)
        db.upsert_member(SERVER_H, BOB, "Bobby", ROLE_MEMBER)
        assert db.get_member_display_name(CHAN_IN_SERVER, BOB) == "Bobby"

    def test_standalone_membership_is_unaffected(self, db):
        _make_server_with_channel(db)
        db.upsert_member(SERVER_H, BOB, "Bob", ROLE_ADMIN)
        # Bob is a server admin but has nothing to do with the standalone channel.
        assert db.get_role(STANDALONE, BOB) is None
        assert db.is_member(STANDALONE, BOB) is False

    def test_has_permission_resolves_to_server_role(self, db):
        _make_server_with_channel(db)
        db.upsert_member(SERVER_H, BOB, "Bob", ROLE_ADMIN)
        assert db.has_permission(CHAN_IN_SERVER, BOB, CREATE_CHANNEL) is True
        assert db.has_permission(CHAN_IN_SERVER, BOB, SEND_MESSAGE) is True

    def test_member_permission_denied_create_channel(self, db):
        _make_server_with_channel(db)
        db.upsert_member(SERVER_H, BOB, "Bob", ROLE_MEMBER)
        assert db.has_permission(CHAN_IN_SERVER, BOB, CREATE_CHANNEL) is False

    def test_tenure_resolves_to_server(self, db):
        _make_server_with_channel(db)
        db.open_tenure(SERVER_H, BOB, 100.0)
        assert db.has_any_tenure(CHAN_IN_SERVER) is True
        assert db.was_member_at(CHAN_IN_SERVER, BOB, 150.0) is True
        assert db.was_member_at(CHAN_IN_SERVER, BOB, 50.0) is False
        assert db.get_open_tenure_joined_at(CHAN_IN_SERVER, BOB) == 100.0

    def test_has_any_tenure_false_for_unrelated_standalone(self, db):
        _make_server_with_channel(db)
        db.open_tenure(SERVER_H, BOB, 100.0)
        assert db.has_any_tenure(STANDALONE) is False

    def test_member_list_version_resolves_to_server(self, db):
        _make_server_with_channel(db)
        db.upsert_member_list_version(SERVER_H, 3, 10.0, b"blob")
        row = db.get_member_list_version(CHAN_IN_SERVER)
        assert row is not None and row["version"] == 3

    def test_writes_do_not_resolve(self, db):
        """A membership write keyed by a channel hash must NOT land at server
        scope; that would be a privilege-escalation primitive."""
        _make_server_with_channel(db)
        db.upsert_member(CHAN_IN_SERVER, BOB, "Bob", ROLE_OWNER)
        # It landed on the channel's own row, which no resolving read reaches.
        assert db.get_role(CHAN_IN_SERVER, BOB) is None
        rows = db._conn.execute(
            "SELECT role FROM members WHERE channel_hash = ? AND identity_hash = ?",
            (CHAN_IN_SERVER, BOB),
        ).fetchall()
        assert rows and rows[0]["role"] == ROLE_OWNER

    def test_scope_cache_invalidated_on_upsert(self, db):
        db.upsert_server(SERVER_H, "S", "", ALICE, PRESET_SERVER, 1.0)
        db.upsert_channel(CHAN_IN_SERVER, "c", "", ALICE, PRESET_SERVER, 1.0,
                          server_hash=SERVER_H)
        assert db.scope_for(CHAN_IN_SERVER) == SERVER_H
        assert CHAN_IN_SERVER in db._scope_cache

        db.upsert_channel(CHAN_IN_SERVER, "renamed", "", ALICE, PRESET_SERVER, 1.0,
                          server_hash=SERVER_H)
        assert CHAN_IN_SERVER not in db._scope_cache
        assert db.scope_for(CHAN_IN_SERVER) == SERVER_H


class TestServerHashIsWriteOnce:
    def test_upsert_cannot_reparent_existing_channel(self, db):
        """Defence against roster adoption: a signed server roster naming an
        existing standalone channel must not capture it."""
        db.upsert_channel(STANDALONE, "solo", "", BOB, PRESET_PRIVATE, 1.0)
        db.upsert_server(SERVER_H, "Evil", "", ALICE, PRESET_SERVER, 1.0)
        db.upsert_channel(STANDALONE, "solo", "", BOB, PRESET_PRIVATE, 1.0,
                          server_hash=SERVER_H)
        assert db.get_channel(STANDALONE)["server_hash"] is None
        assert db.scope_for(STANDALONE) == STANDALONE

    def test_server_hash_set_on_insert(self, db):
        db.upsert_server(SERVER_H, "S", "", ALICE, PRESET_SERVER, 1.0)
        db.upsert_channel(CHAN_IN_SERVER, "general", "", ALICE,
                          PRESET_SERVER, 1.0, server_hash=SERVER_H)
        assert db.get_channel(CHAN_IN_SERVER)["server_hash"] == SERVER_H


class TestPermissionMirror:
    def test_set_server_permissions_mirrors_into_children(self, db):
        _make_server_with_channel(db)
        other = "dd" * 16
        db.upsert_channel(other, "random", "", ALICE, PRESET_SERVER, 1.0,
                          server_hash=SERVER_H)
        new_perms = dict(PRESET_SERVER)
        new_perms[ROLE_MEMBER] = [SEND_MESSAGE, INVITE]
        db.set_server_permissions(SERVER_H, new_perms)

        assert db.get_server_permissions(SERVER_H)[ROLE_MEMBER] == [SEND_MESSAGE, INVITE]
        for ch in (CHAN_IN_SERVER, other):
            mirrored = permissions_from_json(db.get_channel(ch)["permissions"])
            assert mirrored[ROLE_MEMBER] == [SEND_MESSAGE, INVITE]

    def test_mirror_does_not_touch_standalone_channels(self, db):
        _make_server_with_channel(db)
        before = db.get_channel(STANDALONE)["permissions"]
        db.set_server_permissions(SERVER_H, dict(PRESET_SERVER))
        assert db.get_channel(STANDALONE)["permissions"] == before

    def test_mirrored_flags_keep_server_channels_invite_only(self, db):
        """open_join False must ride in the mirror, or server channels would be
        routed down the subscriber path and would get announced."""
        _make_server_with_channel(db)
        db.set_server_permissions(SERVER_H, dict(PRESET_SERVER))
        perms = permissions_from_json(db.get_channel(CHAN_IN_SERVER)["permissions"])
        assert is_open_join(perms) is False
        assert is_discoverable(perms) is False

    def test_get_channel_permissions_does_not_resolve(self, db):
        """Permissions are mirrored down, never resolved up; resolving here
        would be bypassed by the ~18 direct row['permissions'] readers."""
        db.upsert_server(SERVER_H, "S", "", ALICE, PRESET_SERVER, 1.0)
        db.upsert_channel(CHAN_IN_SERVER, "general", "", ALICE,
                          PRESET_PRIVATE, 1.0, server_hash=SERVER_H)
        # The channel row keeps its own blob; get_channel_permissions reads it.
        assert db.get_channel_permissions(CHAN_IN_SERVER) == PRESET_PRIVATE


class TestServerSchemaMigration:
    def test_migration_adds_server_hash_column(self, tmp_path):
        """_migrate_servers() adds channels.server_hash to a legacy database."""
        import sqlite3
        db_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE channels (
                hash TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                creator_hash TEXT NOT NULL,
                permissions TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                last_seen REAL NOT NULL
            )
        """)
        conn.execute(
            "INSERT INTO channels VALUES (?, 'old', '', ?, '{}', 1.0, 1.0)",
            (STANDALONE, BOB),
        )
        conn.commit()
        conn.close()

        s = Storage(db_path=db_path)
        cols = [c["name"] for c in s._conn.execute("PRAGMA table_info(channels)").fetchall()]
        assert "server_hash" in cols
        # Pre-existing rows backfill to NULL, i.e. standalone.
        assert s.get_channel(STANDALONE)["server_hash"] is None
        assert s.scope_for(STANDALONE) == STANDALONE
        s.close()

    def test_migration_is_idempotent(self, tmp_path):
        db_path = tmp_path / "twice.db"
        s = Storage(db_path=db_path)
        s.upsert_server(SERVER_H, "S", "", ALICE, PRESET_SERVER, 1.0)
        s.close()
        s2 = Storage(db_path=db_path)
        assert s2.get_server(SERVER_H)["name"] == "S"
        s2.close()


class TestServerScopePermissions:
    """has_permission() must work when handed a *server* hash, not just a
    channel one. A server has no channels row, so without a fallback every
    admin and member is denied everything, and an owner still looks fine
    because has_permission short-circuits on that role, which is exactly what
    made this hard to notice."""

    def test_admin_holds_server_permissions_by_hash(self, db):
        db.upsert_server(SERVER_H, "HQ", "", ALICE, PRESET_SERVER, 1.0)
        db.upsert_member(SERVER_H, BOB, "Bob", ROLE_ADMIN)
        assert db.get_channel_permissions(SERVER_H) == PRESET_SERVER
        assert db.has_permission(SERVER_H, BOB, CREATE_CHANNEL) is True
        assert db.has_permission(SERVER_H, BOB, SEND_MESSAGE) is True

    def test_member_is_still_denied_admin_only_permissions(self, db):
        db.upsert_server(SERVER_H, "HQ", "", ALICE, PRESET_SERVER, 1.0)
        db.upsert_member(SERVER_H, BOB, "Bob", ROLE_MEMBER)
        assert db.has_permission(SERVER_H, BOB, SEND_MESSAGE) is True
        assert db.has_permission(SERVER_H, BOB, CREATE_CHANNEL) is False

    def test_channel_row_still_wins_for_a_channel_hash(self, db):
        """The fallback must not shadow a real channel's own mirrored row."""
        _make_server_with_channel(db)
        assert db.get_channel_permissions(CHAN_IN_SERVER) == PRESET_SERVER
        assert db.get_channel_permissions(STANDALONE) == PRESET_PRIVATE

    def test_unknown_hash_is_still_empty(self, db):
        assert db.get_channel_permissions("ee" * 16) == {}


# ---------------------------------------------------------------------------
# Friends (local-only saved contacts)
# ---------------------------------------------------------------------------

class TestFriends:
    def test_upsert_and_get_friend(self, db):
        peer = "aa" * 16
        db.upsert_friend(peer, "Al", "met at defcon")
        row = db.get_friend(peer)
        assert row is not None
        assert row["identity_hash"] == peer
        assert row["nickname"] == "Al"
        assert row["note"] == "met at defcon"
        assert row["last_seen_at"] == 0

    def test_get_friend_missing_returns_none(self, db):
        assert db.get_friend("bb" * 16) is None

    def test_upsert_preserves_added_at(self, db):
        peer = "cc" * 16
        db.upsert_friend(peer, "Old", "old note")
        first = db.get_friend(peer)
        db.upsert_friend(peer, "New", "new note")
        second = db.get_friend(peer)
        assert second["added_at"] == first["added_at"]
        assert second["nickname"] == "New"
        assert second["note"] == "new note"

    def test_get_friends_ordered_by_nickname_then_hash(self, db):
        db.upsert_friend("cc" * 16, "Zeta", "")
        db.upsert_friend("aa" * 16, "Alpha", "")
        db.upsert_friend("bb" * 16, "Alpha", "")
        rows = db.get_friends()
        ordered = [(r["nickname"], r["identity_hash"]) for r in rows]
        assert ordered == [
            ("Alpha", "aa" * 16),
            ("Alpha", "bb" * 16),
            ("Zeta", "cc" * 16),
        ]

    def test_delete_friend(self, db):
        peer = "dd" * 16
        db.upsert_friend(peer, "Del", "")
        db.delete_friend(peer)
        assert db.get_friend(peer) is None

    def test_get_friend_hashes(self, db):
        db.upsert_friend("aa" * 16, "A", "")
        db.upsert_friend("bb" * 16, "B", "")
        assert db.get_friend_hashes() == {"aa" * 16, "bb" * 16}

    def test_touch_friend_seen(self, db):
        peer = "ee" * 16
        db.upsert_friend(peer, "E", "")
        db.touch_friend_seen(peer, 12345.0)
        assert db.get_friend(peer)["last_seen_at"] == 12345.0

    def test_touch_friend_seen_survives_close_reopen(self, tmp_path):
        peer = "ff" * 16
        db_path = tmp_path / "friends.db"
        db1 = Storage(db_path=db_path)
        db1.upsert_friend(peer, "F", "")
        db1.touch_friend_seen(peer, 99999.0)
        db1.close()

        db2 = Storage(db_path=db_path)
        assert db2.get_friend(peer)["last_seen_at"] == 99999.0
        db2.close()


class TestSubscriberListVersions:
    """The replay watermark for signed subscriber lists has to be durable."""

    def test_absent_by_default(self, db):
        assert db.get_all_subscriber_list_versions() == {}

    def test_survives_close_reopen(self, tmp_path):
        channel = "ab" * 16
        db_path = tmp_path / "subs.db"
        db1 = Storage(db_path=db_path)
        db1.set_subscriber_list_version(channel, 5)
        db1.close()

        db2 = Storage(db_path=db_path)
        assert db2.get_all_subscriber_list_versions() == {channel: 5}
        db2.close()

    def test_never_regresses(self, db):
        channel = "ab" * 16
        db.set_subscriber_list_version(channel, 5)
        db.set_subscriber_list_version(channel, 3)
        assert db.get_all_subscriber_list_versions()[channel] == 5


class TestHasMessage:
    """message_id is globally unique, so a failed insert is not proof of presence."""

    def _insert(self, db, channel, message_id):
        return db.insert_message(
            channel_hash=channel, sender_hash="aa" * 16, sender_name="A",
            content="hi", timestamp=1000.0, message_id=message_id,
            reply_to=None, last_seen_id=None, received_at=1000.0,
        )

    def test_reports_only_this_channel(self, db):
        here, elsewhere = "ab" * 16, "cd" * 16
        for channel in (here, elsewhere):
            db.upsert_channel(channel, "c", "", "creator", "public", 1000.0)
        assert self._insert(db, elsewhere, "mid-1") is True

        # Same id, different channel: the insert is refused and nothing landed
        # here, so this channel does not have the message.
        assert self._insert(db, here, "mid-1") is False
        assert db.has_message(elsewhere, "mid-1") is True
        assert db.has_message(here, "mid-1") is False

    def test_blank_and_unknown_ids(self, db):
        assert db.has_message("ab" * 16, "") is False
        assert db.has_message("ab" * 16, "nope") is False


class TestIsChannelSubscriber:
    def test_reflects_the_subscriber_set(self, db):
        channel, peer = "ab" * 16, "cc" * 16
        assert db.is_channel_subscriber(channel, peer) is False

        db.add_channel_subscriber(channel, peer)
        assert db.is_channel_subscriber(channel, peer) is True

        db.remove_channel_subscriber(channel, peer)
        assert db.is_channel_subscriber(channel, peer) is False

    def test_blank_identity(self, db):
        assert db.is_channel_subscriber("ab" * 16, "") is False


# ---------------------------------------------------------------------------
# Tenure repair takes its evidence from our own clock, not the sender's
# ---------------------------------------------------------------------------

class TestTenureRepairEvidence:
    """_repair_tenure_from_message_history widens a member's join time.

    Its evidence must be received_at. A message timestamp is self-asserted and
    bounded only against the future, so taking it would let a member backdate
    one message and have their tenure widened to cover history they were never
    present for -- which is exactly what the requester-side tenure filter uses
    to decide what to serve them.
    """

    def _channel_with_member(self, db, joined_at):
        db.upsert_channel(hash=CHAN, name="c", description="",
                          creator_hash=ID_B, permissions="invite",
                          created_at=joined_at - 100)
        db.open_tenure(CHAN, ID_A, joined_at)

    def test_a_backdated_message_does_not_widen_tenure(self, db, tmp_path):
        joined_at = 1_000_000.0
        self._channel_with_member(db, joined_at)
        # Sent "a year before they joined", received just now.
        db.insert_message(CHAN, ID_A, "", "backdated", joined_at - 31_536_000,
                          "m-backdated", None, None, received_at=joined_at + 10)

        db._repair_tenure_from_message_history()

        assert not db.was_member_at(CHAN, ID_A, joined_at - 1000), \
            "a backdated message widened the sender's tenure"
        assert db.was_member_at(CHAN, ID_A, joined_at + 10)

    def test_a_genuinely_old_message_still_widens_tenure(self, db):
        """The repair must still do the job it exists for."""
        joined_at = 1_000_000.0
        self._channel_with_member(db, joined_at)
        # Received long before the tenure row says they joined -- the
        # added_at-reset artifact the repair was written to correct.
        db.insert_message(CHAN, ID_A, "", "genuinely old", joined_at - 5000,
                          "m-old", None, None, received_at=joined_at - 5000)

        db._repair_tenure_from_message_history()

        assert db.was_member_at(CHAN, ID_A, joined_at - 4000), \
            "the repair no longer widens tenure for genuinely old history"
