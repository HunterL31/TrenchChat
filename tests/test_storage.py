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
        # Before kick — valid
        assert db.was_member_at(CHAN, ID_A, t0)
        assert db.was_member_at(CHAN, ID_A, kick - 1)
        # In the gap — invalid
        assert not db.was_member_at(CHAN, ID_A, kick)
        assert not db.was_member_at(CHAN, ID_A, rejoin - 1)
        # After rejoin — valid
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
        # Same set in and out — nothing changes
        db.update_tenure(CHAN, {ID_A}, {ID_A}, t1)
        assert db.was_member_at(CHAN, ID_A, t1)

    def test_close_tenure_with_no_open_interval_is_noop(self, db):
        # Should not raise
        db.close_tenure(CHAN, ID_A, 1_000_000.0)

    def test_open_tenure_idempotent(self, db):
        t0 = 1_000_000.0
        db.open_tenure(CHAN, ID_A, t0)
        db.open_tenure(CHAN, ID_A, t0)  # duplicate — ignored
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

    def test_backfill_from_members_table(self, tmp_path):
        """Existing members are backfilled into tenure on first open."""
        db = Storage(db_path=tmp_path / "bf.db")
        # Manually insert a member row — bypassing tenure so we can test backfill
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
