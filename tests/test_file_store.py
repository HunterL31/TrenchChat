"""
Unit tests for the shared file store in trenchchat.core.storage.

The database layer only: no networking, no manager. Budgets are monkeypatched
down to a few kilobytes so a full store is cheap to build.
"""

import sqlite3
import time

import pytest

from trenchchat.core import storage as storage_module
from trenchchat.core.storage import Storage

CHANNEL = "aa" * 16
OTHER_CHANNEL = "cc" * 16
SENDER = "bb" * 16


@pytest.fixture
def db(tmp_path) -> Storage:
    """Fresh Storage instance backed by a temp file for each test."""
    s = Storage(db_path=tmp_path / "files.db")
    yield s
    s.close()


@pytest.fixture
def budgets(monkeypatch):
    """Kilobyte-sized budgets, so filling the store costs nothing."""
    monkeypatch.setattr(storage_module, "FILE_STORE_MAX_BYTES", 3000)
    monkeypatch.setattr(storage_module, "OWN_FILE_STORE_MAX_BYTES", 2000)
    monkeypatch.setattr(storage_module, "PARTIAL_STORE_MAX_BYTES", 1500)
    monkeypatch.setattr(storage_module, "PARTIAL_FILE_TTL_SECS", 100.0)


def _hash(marker: str) -> str:
    return marker * 64


def _manifest(name: str = "notes.txt", size: int = 5,
              marker: str = "a", root_marker: str = "b") -> dict:
    return {
        "name": name,
        "size": size,
        "hash": bytes.fromhex(_hash(marker)),
        "chunk_root": bytes.fromhex(_hash(root_marker)),
    }


def _store(db: Storage, marker: str, size: int, *,
           own: bool = False, complete: bool = True) -> str:
    """Put one file of *size* bytes in the store and return its hash."""
    hash_hex = _hash(marker)
    db.begin_file(hash_hex, size, own=own)
    assert db.put_file_chunk(hash_hex, 0, b"x" * size)
    if complete:
        db.mark_file_complete(hash_hex)
    return hash_hex


def _channel(db: Storage, channel_hash: str = CHANNEL) -> None:
    db.upsert_channel(channel_hash, "Test", "", SENDER, "public", 1.0)


# ---------------------------------------------------------------------------
# Manifest columns on messages
# ---------------------------------------------------------------------------

class TestMessageManifestColumns:
    def test_migration_adds_file_columns(self, tmp_path):
        """_migrate_file_manifest() adds the columns to a legacy database."""
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
        conn.execute(
            "INSERT INTO messages (channel_hash, sender_hash, content, timestamp, "
            "message_id, received_at) VALUES (?, ?, 'old', 1.0, 'old_msg', 1.0)",
            (CHANNEL, SENDER),
        )
        conn.commit()
        conn.close()

        s = Storage(db_path=db_path)
        cols = [c["name"] for c in
                s._conn.execute("PRAGMA table_info(messages)").fetchall()]
        for column in ("file_name", "file_size", "file_hash",
                       "file_chunk_root", "file_stripped"):
            assert column in cols
        row = s._conn.execute(
            "SELECT * FROM messages WHERE message_id = 'old_msg'").fetchone()
        assert row["file_name"] is None
        assert row["file_stripped"] == 0
        s.close()

    def test_manifest_round_trips_through_a_message(self, db):
        _channel(db)
        assert db.insert_message(
            channel_hash=CHANNEL, sender_hash=SENDER, sender_name="Alice",
            content="have a look", timestamp=10.0, message_id="file_msg_1",
            reply_to=None, last_seen_id=None, received_at=10.0,
            manifest=_manifest(),
        )
        row = db.get_messages(CHANNEL)[0]
        assert row["file_name"] == "notes.txt"
        assert row["file_size"] == 5
        assert row["file_hash"] == _hash("a")
        assert row["file_chunk_root"] == _hash("b")
        assert row["file_stripped"] == 0

    def test_message_without_a_manifest_leaves_the_columns_null(self, db):
        _channel(db)
        db.insert_message(
            channel_hash=CHANNEL, sender_hash=SENDER, sender_name="Alice",
            content="just words", timestamp=11.0, message_id="text_msg_1",
            reply_to=None, last_seen_id=None, received_at=11.0,
        )
        row = db.get_messages(CHANNEL)[0]
        assert row["file_name"] is None
        assert row["file_size"] is None
        assert row["file_hash"] is None
        assert row["file_chunk_root"] is None
        assert row["file_stripped"] == 0

    def test_stripped_manifest_is_recorded(self, db):
        _channel(db)
        db.insert_message(
            channel_hash=CHANNEL, sender_hash=SENDER, sender_name="Alice",
            content="refused attachment", timestamp=12.0, message_id="bad_msg_1",
            reply_to=None, last_seen_id=None, received_at=12.0,
            file_stripped=True,
        )
        row = db.get_messages(CHANNEL)[0]
        assert row["file_stripped"] == 1
        assert row["file_hash"] is None


# ---------------------------------------------------------------------------
# Chunk storage
# ---------------------------------------------------------------------------

class TestChunkStore:
    def test_chunk_round_trip_in_order(self, db):
        hash_hex = _hash("a")
        db.begin_file(hash_hex, 300)
        for idx, content in enumerate((b"one", b"two", b"three")):
            assert db.put_file_chunk(hash_hex, idx, content)
        assert db.file_chunk_indices(hash_hex) == [0, 1, 2]
        assert db.get_file_chunks(hash_hex, 0, 3) == [b"one", b"two", b"three"]
        assert db.get_file(hash_hex)["held_bytes"] == 11

    def test_chunks_read_back_by_range(self, db):
        hash_hex = _hash("a")
        db.begin_file(hash_hex, 300)
        for idx in range(5):
            db.put_file_chunk(hash_hex, idx, bytes([idx]) * 4)
        assert db.get_file_chunks(hash_hex, 1, 2) == [b"\x01" * 4, b"\x02" * 4]
        assert db.get_file_chunks(hash_hex, 4, 8) == [b"\x04" * 4]
        assert db.get_file_chunks(hash_hex, 9, 2) == []

    def test_out_of_order_arrival_keeps_index_order(self, db):
        hash_hex = _hash("a")
        db.begin_file(hash_hex, 300)
        db.put_file_chunk(hash_hex, 2, b"c")
        db.put_file_chunk(hash_hex, 0, b"a")
        db.put_file_chunk(hash_hex, 1, b"b")
        assert db.file_chunk_indices(hash_hex) == [0, 1, 2]
        db.mark_file_complete(hash_hex)
        assert db.get_file_bytes(hash_hex) == b"abc"

    def test_get_file_bytes_only_when_complete(self, db):
        hash_hex = _store(db, "a", 40, complete=False)
        assert db.get_file_bytes(hash_hex) is None
        db.mark_file_complete(hash_hex)
        assert db.get_file_bytes(hash_hex) == b"x" * 40
        assert db.get_file_bytes(_hash("f")) is None

    def test_repeated_chunk_is_not_counted_twice(self, db):
        hash_hex = _hash("a")
        db.begin_file(hash_hex, 100)
        db.put_file_chunk(hash_hex, 0, b"y" * 10)
        db.put_file_chunk(hash_hex, 0, b"y" * 10)
        assert db.get_file(hash_hex)["held_bytes"] == 10
        assert db.file_chunk_indices(hash_hex) == [0]

    def test_begin_file_is_idempotent_and_keeps_chunks(self, db):
        hash_hex = _store(db, "a", 20, complete=False)
        db.begin_file(hash_hex, 20)
        assert db.file_chunk_indices(hash_hex) == [0]
        assert db.get_file(hash_hex)["held_bytes"] == 20

    def test_begin_file_promotes_a_received_file_to_own(self, db):
        hash_hex = _store(db, "a", 20)
        assert db.get_file(hash_hex)["own"] == 0
        db.begin_file(hash_hex, 20, own=True)
        assert db.get_file(hash_hex)["own"] == 1
        db.begin_file(hash_hex, 20, own=False)
        assert db.get_file(hash_hex)["own"] == 1

    def test_chunk_count_covers_the_whole_file(self, db):
        hash_hex = _hash("a")
        db.begin_file(hash_hex, storage_module.FILE_CHUNK_BYTES + 1)
        assert db.get_file(hash_hex)["chunk_count"] == 2

    def test_delete_removes_the_row_and_the_chunks(self, db):
        hash_hex = _store(db, "a", 30)
        db.delete_file(hash_hex)
        assert db.get_file(hash_hex) is None
        assert db.file_chunk_indices(hash_hex) == []
        assert db.get_file_chunks(hash_hex, 0, 4) == []

    def test_list_files_filters_by_state(self, db):
        own = _store(db, "a", 10, own=True)
        received = _store(db, "b", 10)
        partial = _store(db, "c", 10, complete=False)
        assert {r["hash"] for r in db.list_files()} == {own, received, partial}
        assert [r["hash"] for r in db.list_files(own=True)] == [own]
        assert {r["hash"] for r in db.list_files(complete=True)} == {own, received}
        assert [r["hash"] for r in db.list_files(complete=False, own=False)] == [partial]

    def test_touch_file_records_use(self, db):
        hash_hex = _store(db, "a", 10)
        db.touch_file(hash_hex, used_at=5000.0)
        assert db.get_file(hash_hex)["last_used"] == 5000.0


# ---------------------------------------------------------------------------
# Which channels a file was shared in
# ---------------------------------------------------------------------------

class TestFileChannels:
    def test_file_channels_reflects_the_messages_carrying_it(self, db):
        _channel(db, CHANNEL)
        _channel(db, OTHER_CHANNEL)
        manifest = _manifest()
        db.insert_message(
            channel_hash=CHANNEL, sender_hash=SENDER, sender_name="Alice",
            content="", timestamp=1.0, message_id="m1", reply_to=None,
            last_seen_id=None, received_at=1.0, manifest=manifest)
        db.insert_message(
            channel_hash=CHANNEL, sender_hash=SENDER, sender_name="Alice",
            content="again", timestamp=2.0, message_id="m2", reply_to=None,
            last_seen_id=None, received_at=2.0, manifest=manifest)
        db.insert_message(
            channel_hash=OTHER_CHANNEL, sender_hash=SENDER, sender_name="Alice",
            content="", timestamp=3.0, message_id="m3", reply_to=None,
            last_seen_id=None, received_at=3.0, manifest=manifest)
        assert sorted(db.file_channels(_hash("a"))) == sorted([CHANNEL, OTHER_CHANNEL])

    def test_unshared_file_has_no_channels(self, db):
        _store(db, "a", 10)
        assert db.file_channels(_hash("a")) == []


# ---------------------------------------------------------------------------
# Budgets and admission
# ---------------------------------------------------------------------------

class TestFileBudgets:
    def test_usage_counts_each_budget_separately(self, db, budgets):
        _store(db, "a", 100, own=True)
        _store(db, "b", 200)
        _store(db, "c", 300, complete=False)
        assert db.file_store_usage() == {"own": 100, "received": 200, "partial": 300}

    def test_own_uploads_survive_a_full_store(self, db, budgets):
        mine = _store(db, "a", 1000, own=True)
        received = [_store(db, m, 1000) for m in ("b", "c", "d")]
        assert db.file_store_usage()["received"] == 3000

        assert db.admit_file(_hash("e"), 1000) is True

        assert db.get_file(mine) is not None
        assert db.file_store_usage()["own"] == 1000
        survivors = [h for h in received if db.get_file(h) is not None]
        assert len(survivors) == 2

    def test_complete_files_are_evicted_least_recently_used_first(self, db, budgets):
        b, c, d = (_store(db, m, 1000) for m in ("b", "c", "d"))
        db.touch_file(b, used_at=300.0)
        db.touch_file(c, used_at=100.0)
        db.touch_file(d, used_at=200.0)

        assert db.admit_file(_hash("e"), 1000) is True

        assert db.get_file(c) is None
        assert db.get_file(b) is not None
        assert db.get_file(d) is not None

    def test_oldest_partial_goes_first_when_the_partial_budget_is_short(self, db, budgets):
        old = _store(db, "a", 600, complete=False)
        time.sleep(0.01)
        recent = _store(db, "b", 600, complete=False)
        kept = _store(db, "c", 1000)

        assert db.admit_file(_hash("d"), 600) is True

        assert db.get_file(old) is None
        assert db.get_file(recent) is not None
        assert db.get_file(kept) is not None

    def test_admission_refuses_a_file_larger_than_the_budget(self, db, budgets):
        kept = _store(db, "a", 1000)
        assert db.admit_file(_hash("b"), 3001) is False
        assert db.admit_file(_hash("b"), 1600) is False
        assert db.get_file(kept) is not None

    def test_admission_refuses_an_own_file_past_the_own_ceiling(self, db, budgets):
        mine = _store(db, "a", 1500, own=True)
        assert db.admit_file(_hash("b"), 1000, own=True) is False
        assert db.admit_file(_hash("b"), 2001, own=True) is False
        assert db.get_file(mine) is not None
        assert db.admit_file(_hash("b"), 500, own=True) is True

    def test_admitting_a_resumed_download_does_not_charge_it_twice(self, db, budgets):
        partial = _store(db, "a", 1400, complete=False)
        assert db.admit_file(partial, 1400) is True
        assert db.get_file(partial) is not None
        assert db.file_chunk_indices(partial) == [0]

    def test_prune_drops_stale_partials_and_keeps_own(self, db, budgets):
        partial = _store(db, "a", 100, complete=False)
        own_partial = _store(db, "b", 100, own=True, complete=False)
        stored_at = db.get_file(partial)["stored_at"]

        assert db.prune_files(now=stored_at + 10.0) == 0
        assert db.get_file(partial) is not None

        assert db.prune_files(now=stored_at + 101.0) == 1
        assert db.get_file(partial) is None
        assert db.get_file(own_partial) is not None

    def test_prune_enforces_the_received_budget_by_lru(self, db, budgets):
        b, c, d, e = (_store(db, m, 1000) for m in ("b", "c", "d", "e"))
        db.touch_file(b, used_at=400.0)
        db.touch_file(c, used_at=100.0)
        db.touch_file(d, used_at=300.0)
        db.touch_file(e, used_at=200.0)
        mine = _store(db, "a", 1000, own=True)

        assert db.prune_files(now=500.0) == 1

        assert db.get_file(c) is None
        assert db.get_file(mine) is not None
        assert db.file_store_usage()["received"] == 3000


# ---------------------------------------------------------------------------
# A full disk
# ---------------------------------------------------------------------------

class TestDiskFull:
    def test_chunk_write_fails_cleanly_when_the_database_is_full(self, db):
        """A full disk answers False and keeps the chunks already held.

        max_page_count is SQLite's own way of running out of room, so this is
        the real "database or disk is full" error, not a stand-in for it.
        """
        hash_hex = _hash("a")
        db.begin_file(hash_hex, 3 * 4096)
        assert db.put_file_chunk(hash_hex, 0, b"x" * 4096)
        held = db.get_file(hash_hex)["held_bytes"]

        pages = db._conn.execute("PRAGMA page_count").fetchone()[0]
        db._conn.execute(f"PRAGMA max_page_count = {pages}")

        assert db.put_file_chunk(hash_hex, 1, b"y" * 4096) is False
        assert db.put_file_chunk(hash_hex, 2, b"z" * 4096) is False

        db._conn.execute("PRAGMA max_page_count = 1073741823")
        assert db.file_chunk_indices(hash_hex) == [0]
        assert db.get_file(hash_hex)["held_bytes"] == held
        assert db.get_file_bytes(hash_hex) is None

    def test_other_database_errors_still_raise(self, db):
        hash_hex = _hash("a")
        db.begin_file(hash_hex, 10)
        with pytest.raises(sqlite3.ProgrammingError):
            db.put_file_chunk(hash_hex, 0, object())
