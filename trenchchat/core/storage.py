import sqlite3
import time
from pathlib import Path
from contextlib import contextmanager

from trenchchat.config import DATA_DIR

DB_PATH = DATA_DIR / "storage.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    hash        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    creator_hash TEXT NOT NULL,
    access_mode TEXT NOT NULL DEFAULT 'public',
    created_at  REAL NOT NULL,
    last_seen   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_hash TEXT NOT NULL,
    sender_hash  TEXT NOT NULL,
    sender_name  TEXT NOT NULL DEFAULT '',
    content      TEXT NOT NULL DEFAULT '',
    timestamp    REAL NOT NULL,
    message_id   TEXT NOT NULL UNIQUE,
    reply_to     TEXT,
    last_seen_id TEXT,
    received_at  REAL NOT NULL,
    FOREIGN KEY (channel_hash) REFERENCES channels(hash)
);

CREATE INDEX IF NOT EXISTS idx_messages_channel_ts
    ON messages(channel_hash, timestamp);

CREATE TABLE IF NOT EXISTS subscriptions (
    channel_hash TEXT PRIMARY KEY,
    joined_at    REAL NOT NULL,
    last_sync_at REAL NOT NULL DEFAULT 0,
    FOREIGN KEY (channel_hash) REFERENCES channels(hash)
);

CREATE TABLE IF NOT EXISTS members (
    channel_hash  TEXT NOT NULL,
    identity_hash TEXT NOT NULL,
    display_name  TEXT NOT NULL DEFAULT '',
    is_admin      INTEGER NOT NULL DEFAULT 0,
    added_at      REAL NOT NULL,
    PRIMARY KEY (channel_hash, identity_hash)
);

CREATE TABLE IF NOT EXISTS member_list_versions (
    channel_hash  TEXT PRIMARY KEY,
    version       INTEGER NOT NULL,
    published_at  REAL NOT NULL,
    document_blob BLOB NOT NULL,
    received_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS missed_deliveries (
    channel_hash   TEXT NOT NULL,
    recipient_hash TEXT NOT NULL,
    message_id     TEXT NOT NULL,
    recorded_at    REAL NOT NULL,
    PRIMARY KEY (channel_hash, recipient_hash, message_id)
);

CREATE INDEX IF NOT EXISTS idx_missed_deliveries_recipient
    ON missed_deliveries(recipient_hash, channel_hash);
"""


class Storage:
    def __init__(self, db_path: Path = DB_PATH):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._path = str(db_path)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    @contextmanager
    def _tx(self):
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def close(self):
        self._conn.close()

    # --- channels ---

    def upsert_channel(self, hash: str, name: str, description: str,
                       creator_hash: str, access_mode: str, created_at: float):
        with self._tx():
            self._conn.execute("""
                INSERT INTO channels (hash, name, description, creator_hash, access_mode, created_at, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(hash) DO UPDATE SET
                    name=excluded.name,
                    description=excluded.description,
                    access_mode=excluded.access_mode,
                    last_seen=excluded.last_seen
            """, (hash, name, description, creator_hash, access_mode, created_at, time.time()))

    def get_channel(self, hash: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM channels WHERE hash = ?", (hash,)
        ).fetchone()

    def get_all_channels(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM channels ORDER BY name"
        ).fetchall()

    def touch_channel(self, hash: str):
        self._conn.execute(
            "UPDATE channels SET last_seen = ? WHERE hash = ?", (time.time(), hash)
        )
        self._conn.commit()

    # --- messages ---

    def insert_message(self, channel_hash: str, sender_hash: str, sender_name: str,
                       content: str, timestamp: float, message_id: str,
                       reply_to: str | None, last_seen_id: str | None,
                       received_at: float) -> bool:
        """Returns True if inserted, False if duplicate."""
        try:
            with self._tx():
                self._conn.execute("""
                    INSERT INTO messages
                        (channel_hash, sender_hash, sender_name, content, timestamp,
                         message_id, reply_to, last_seen_id, received_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (channel_hash, sender_hash, sender_name, content, timestamp,
                      message_id, reply_to, last_seen_id, received_at))
            return True
        except sqlite3.IntegrityError:
            return False

    def get_messages(self, channel_hash: str, limit: int = 200,
                     before_ts: float | None = None) -> list[sqlite3.Row]:
        if before_ts is None:
            return self._conn.execute("""
                SELECT * FROM messages
                WHERE channel_hash = ?
                ORDER BY timestamp ASC, received_at ASC
                LIMIT ?
            """, (channel_hash, limit)).fetchall()
        return self._conn.execute("""
            SELECT * FROM messages
            WHERE channel_hash = ? AND timestamp < ?
            ORDER BY timestamp ASC, received_at ASC
            LIMIT ?
        """, (channel_hash, before_ts, limit)).fetchall()

    def get_latest_message_id(self, channel_hash: str) -> str | None:
        row = self._conn.execute("""
            SELECT message_id FROM messages
            WHERE channel_hash = ?
            ORDER BY timestamp DESC, received_at DESC
            LIMIT 1
        """, (channel_hash,)).fetchone()
        return row["message_id"] if row else None

    def message_exists(self, message_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        return row is not None

    # --- subscriptions ---

    def subscribe(self, channel_hash: str):
        with self._tx():
            self._conn.execute("""
                INSERT OR IGNORE INTO subscriptions (channel_hash, joined_at, last_sync_at)
                VALUES (?, ?, 0)
            """, (channel_hash, time.time()))

    def unsubscribe(self, channel_hash: str):
        with self._tx():
            self._conn.execute(
                "DELETE FROM subscriptions WHERE channel_hash = ?", (channel_hash,)
            )

    def is_subscribed(self, channel_hash: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM subscriptions WHERE channel_hash = ?", (channel_hash,)
        ).fetchone()
        return row is not None

    def get_subscriptions(self) -> list[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM subscriptions").fetchall()

    def update_last_sync(self, channel_hash: str):
        with self._tx():
            self._conn.execute(
                "UPDATE subscriptions SET last_sync_at = ? WHERE channel_hash = ?",
                (time.time(), channel_hash)
            )

    # --- members ---

    def upsert_member(self, channel_hash: str, identity_hash: str,
                      display_name: str, is_admin: bool):
        with self._tx():
            self._conn.execute("""
                INSERT INTO members (channel_hash, identity_hash, display_name, is_admin, added_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(channel_hash, identity_hash) DO UPDATE SET
                    display_name=excluded.display_name,
                    is_admin=excluded.is_admin
            """, (channel_hash, identity_hash, display_name, int(is_admin), time.time()))

    def remove_member(self, channel_hash: str, identity_hash: str):
        with self._tx():
            self._conn.execute(
                "DELETE FROM members WHERE channel_hash = ? AND identity_hash = ?",
                (channel_hash, identity_hash)
            )

    def get_members(self, channel_hash: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM members WHERE channel_hash = ? ORDER BY added_at",
            (channel_hash,)
        ).fetchall()

    def is_member(self, channel_hash: str, identity_hash: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM members WHERE channel_hash = ? AND identity_hash = ?",
            (channel_hash, identity_hash)
        ).fetchone()
        return row is not None

    def is_admin(self, channel_hash: str, identity_hash: str) -> bool:
        row = self._conn.execute(
            "SELECT is_admin FROM members WHERE channel_hash = ? AND identity_hash = ?",
            (channel_hash, identity_hash)
        ).fetchone()
        return bool(row and row["is_admin"])

    def replace_members(self, channel_hash: str,
                        members: list[tuple[str, str, bool]]):
        """Replace the full member list for a channel atomically.
        members: list of (identity_hash, display_name, is_admin)
        """
        with self._tx():
            self._conn.execute(
                "DELETE FROM members WHERE channel_hash = ?", (channel_hash,)
            )
            now = time.time()
            self._conn.executemany("""
                INSERT INTO members (channel_hash, identity_hash, display_name, is_admin, added_at)
                VALUES (?, ?, ?, ?, ?)
            """, [(channel_hash, ih, dn, int(ia), now) for ih, dn, ia in members])

    # --- member list versions ---

    def get_member_list_version(self, channel_hash: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM member_list_versions WHERE channel_hash = ?",
            (channel_hash,)
        ).fetchone()

    def upsert_member_list_version(self, channel_hash: str, version: int,
                                   published_at: float, document_blob: bytes):
        with self._tx():
            self._conn.execute("""
                INSERT INTO member_list_versions
                    (channel_hash, version, published_at, document_blob, received_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(channel_hash) DO UPDATE SET
                    version=excluded.version,
                    published_at=excluded.published_at,
                    document_blob=excluded.document_blob,
                    received_at=excluded.received_at
            """, (channel_hash, version, published_at, document_blob, time.time()))

    # --- message sync helpers ---

    def get_messages_after(self, channel_hash: str, since_ts: float,
                           limit: int = 50) -> list[sqlite3.Row]:
        """Fetch up to `limit` messages for a channel with timestamp > since_ts."""
        return self._conn.execute("""
            SELECT * FROM messages
            WHERE channel_hash = ? AND timestamp > ?
            ORDER BY timestamp ASC, received_at ASC
            LIMIT ?
        """, (channel_hash, since_ts, limit)).fetchall()

    # --- missed_deliveries ---

    def record_missed_delivery(self, channel_hash: str,
                                recipient_hash: str, message_id: str):
        with self._tx():
            self._conn.execute("""
                INSERT OR IGNORE INTO missed_deliveries
                    (channel_hash, recipient_hash, message_id, recorded_at)
                VALUES (?, ?, ?, ?)
            """, (channel_hash, recipient_hash, message_id, time.time()))

    def get_missed_message_ids(self, channel_hash: str,
                                recipient_hash: str) -> list[str]:
        rows = self._conn.execute("""
            SELECT message_id FROM missed_deliveries
            WHERE channel_hash = ? AND recipient_hash = ?
        """, (channel_hash, recipient_hash)).fetchall()
        return [r["message_id"] for r in rows]

    def clear_missed_deliveries(self, channel_hash: str, recipient_hash: str):
        with self._tx():
            self._conn.execute("""
                DELETE FROM missed_deliveries
                WHERE channel_hash = ? AND recipient_hash = ?
            """, (channel_hash, recipient_hash))

    def purge_old_missed_deliveries(self, before_ts: float):
        """Remove hint records older than the sync window."""
        with self._tx():
            self._conn.execute(
                "DELETE FROM missed_deliveries WHERE recorded_at < ?",
                (before_ts,)
            )
