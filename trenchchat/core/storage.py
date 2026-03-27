import json
import sqlite3
import threading
import time
from pathlib import Path
from contextlib import contextmanager

from trenchchat.config import DATA_DIR
from trenchchat.core.fileutils import secure_file
from trenchchat.core.permissions import (
    PRESET_OPEN, PRESET_PRIVATE, ROLE_ADMIN, ROLE_MEMBER, ROLE_OWNER,
    has_permission as _check_permission,
    permissions_from_json, permissions_to_json,
)

DB_PATH = DATA_DIR / "storage.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    hash        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    creator_hash TEXT NOT NULL,
    permissions TEXT NOT NULL DEFAULT '{}',
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
    role          TEXT NOT NULL DEFAULT 'member',
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
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._path = str(db_path)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._migrate_permissions()
        self._secure_db_files()
        # Serialise all connection use across threads.  SQLite's Python
        # binding shares a single connection object; concurrent execute/commit/
        # rollback calls from different threads corrupt cursor state even with
        # check_same_thread=False.  An RLock (reentrant) is used so that a
        # single thread can re-enter (e.g. _tx → insert → _tx).
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # File permission hardening
    # ------------------------------------------------------------------

    def _secure_db_files(self) -> None:
        """Enforce owner-only permissions on the database file and its WAL sidecars.

        SQLite in WAL mode creates two sidecar files alongside the main database
        (<db>-wal and <db>-shm).  All three files contain sensitive data and must
        be restricted to the owner.  Sidecars are only secured if they already
        exist; absent sidecars are left for SQLite to create with the OS umask
        (they will be re-secured on the next application launch).
        """
        db = Path(self._path)
        for candidate in (db, db.parent / (db.name + "-wal"), db.parent / (db.name + "-shm")):
            if candidate.exists():
                secure_file(candidate)

    # ------------------------------------------------------------------
    # Schema migration from access_mode/is_admin to permissions/role
    # ------------------------------------------------------------------

    def _has_column(self, table: str, column: str) -> bool:
        cols = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        return any(c["name"] == column for c in cols)

    def _migrate_permissions(self):
        """One-time migration from the legacy access_mode / is_admin schema."""
        changed = False

        # --- channels: access_mode -> permissions ---
        if self._has_column("channels", "access_mode"):
            if not self._has_column("channels", "permissions"):
                self._conn.execute(
                    "ALTER TABLE channels ADD COLUMN permissions TEXT NOT NULL DEFAULT '{}'"
                )
                changed = True
            rows = self._conn.execute(
                "SELECT hash, access_mode FROM channels WHERE permissions = '{}'"
            ).fetchall()
            for row in rows:
                preset = PRESET_OPEN if row["access_mode"] == "public" else PRESET_PRIVATE
                self._conn.execute(
                    "UPDATE channels SET permissions = ? WHERE hash = ?",
                    (permissions_to_json(preset), row["hash"]),
                )
                changed = True

        # --- members: is_admin -> role ---
        if self._has_column("members", "is_admin"):
            if not self._has_column("members", "role"):
                self._conn.execute(
                    "ALTER TABLE members ADD COLUMN role TEXT NOT NULL DEFAULT 'member'"
                )
                changed = True
            self._conn.execute(
                "UPDATE members SET role = 'admin' WHERE is_admin = 1 AND role = 'member'"
            )
            # Promote channel creators to owner
            self._conn.execute("""
                UPDATE members SET role = 'owner'
                WHERE role IN ('admin', 'member')
                  AND (channel_hash, identity_hash) IN (
                      SELECT m.channel_hash, m.identity_hash
                      FROM members m
                      JOIN channels c ON m.channel_hash = c.hash
                      WHERE m.identity_hash = c.creator_hash
                  )
            """)
            changed = True

        if changed:
            self._conn.commit()

    @contextmanager
    def _tx(self):
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                try:
                    self._conn.rollback()
                except Exception:
                    pass  # rollback errors must not mask the original exception
                raise

    def _fetchall(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _fetchone(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def close(self):
        self._conn.close()

    # --- channels ---

    def upsert_channel(self, hash: str, name: str, description: str,
                       creator_hash: str, permissions: str | dict = "",
                       created_at: float = 0.0, *, access_mode: str = ""):
        """Create or update a channel.

        *permissions* can be a JSON string, a dict (will be serialised), or
        a legacy access-mode string (``"public"`` / ``"invite"``).
        The legacy *access_mode* keyword is also accepted.
        """
        if access_mode and not permissions:
            permissions = access_mode
        if isinstance(permissions, dict):
            permissions = permissions_to_json(permissions)
        elif permissions in ("public", "invite"):
            preset = PRESET_OPEN if permissions == "public" else PRESET_PRIVATE
            permissions = permissions_to_json(preset)
        elif not permissions:
            permissions = permissions_to_json(PRESET_PRIVATE)
        with self._tx():
            self._conn.execute("""
                INSERT INTO channels (hash, name, description, creator_hash, permissions, created_at, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(hash) DO UPDATE SET
                    name=excluded.name,
                    description=excluded.description,
                    permissions=excluded.permissions,
                    last_seen=excluded.last_seen
            """, (hash, name, description, creator_hash, permissions, created_at, time.time()))

    def get_channel(self, hash: str) -> sqlite3.Row | None:
        return self._fetchone("SELECT * FROM channels WHERE hash = ?", (hash,))

    def get_all_channels(self) -> list[sqlite3.Row]:
        return self._fetchall("SELECT * FROM channels ORDER BY name")

    def touch_channel(self, hash: str):
        with self._tx():
            self._conn.execute(
                "UPDATE channels SET last_seen = ? WHERE hash = ?", (time.time(), hash)
            )

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
            return self._fetchall("""
                SELECT * FROM messages
                WHERE channel_hash = ?
                ORDER BY timestamp ASC, received_at ASC
                LIMIT ?
            """, (channel_hash, limit))
        return self._fetchall("""
            SELECT * FROM messages
            WHERE channel_hash = ? AND timestamp < ?
            ORDER BY timestamp ASC, received_at ASC
            LIMIT ?
        """, (channel_hash, before_ts, limit))

    def get_latest_message_id(self, channel_hash: str) -> str | None:
        row = self._fetchone("""
            SELECT message_id FROM messages
            WHERE channel_hash = ?
            ORDER BY timestamp DESC, received_at DESC
            LIMIT 1
        """, (channel_hash,))
        return row["message_id"] if row else None

    def message_exists(self, message_id: str) -> bool:
        return self._fetchone(
            "SELECT 1 FROM messages WHERE message_id = ?", (message_id,)
        ) is not None

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
        return self._fetchone(
            "SELECT 1 FROM subscriptions WHERE channel_hash = ?", (channel_hash,)
        ) is not None

    def get_subscriptions(self) -> list[sqlite3.Row]:
        return self._fetchall("SELECT * FROM subscriptions")

    def update_last_sync(self, channel_hash: str):
        with self._tx():
            self._conn.execute(
                "UPDATE subscriptions SET last_sync_at = ? WHERE channel_hash = ?",
                (time.time(), channel_hash)
            )

    # --- members ---

    def upsert_member(self, channel_hash: str, identity_hash: str,
                      display_name: str, role: str | bool = ROLE_MEMBER,
                      *, is_admin: bool | None = None):
        """Insert or update a member.

        *role* should be one of ``ROLE_OWNER``, ``ROLE_ADMIN``, ``ROLE_MEMBER``.
        For backward compatibility a bool is accepted (True → admin, False → member),
        and the legacy *is_admin* keyword is also honoured.
        """
        if is_admin is not None:
            role = ROLE_ADMIN if is_admin else ROLE_MEMBER
        elif isinstance(role, bool):
            role = ROLE_ADMIN if role else ROLE_MEMBER
        with self._tx():
            self._conn.execute("""
                INSERT INTO members (channel_hash, identity_hash, display_name, role, added_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(channel_hash, identity_hash) DO UPDATE SET
                    display_name=excluded.display_name,
                    role=excluded.role
            """, (channel_hash, identity_hash, display_name, role, time.time()))

    def remove_member(self, channel_hash: str, identity_hash: str):
        with self._tx():
            self._conn.execute(
                "DELETE FROM members WHERE channel_hash = ? AND identity_hash = ?",
                (channel_hash, identity_hash)
            )

    def get_members(self, channel_hash: str) -> list[sqlite3.Row]:
        return self._fetchall(
            "SELECT * FROM members WHERE channel_hash = ? ORDER BY added_at",
            (channel_hash,)
        )

    def get_member_display_name(self, channel_hash: str,
                                identity_hash: str) -> str | None:
        """Return the stored display name for a member, or None if not found."""
        row = self._fetchone(
            "SELECT display_name FROM members WHERE channel_hash = ? AND identity_hash = ?",
            (channel_hash, identity_hash),
        )
        return row["display_name"] if row else None

    def get_trenchchat_peer_identities(self) -> set[str]:
        """Return the set of all identity hashes known to be TrenchChat users.

        Includes every identity that appears in any channel's member list.
        This is used to filter the network map to show only nodes that are
        part of the TrenchChat network.
        """
        rows = self._fetchall("SELECT DISTINCT identity_hash FROM members")
        return {row["identity_hash"] for row in rows}

    def get_display_name_for_identity(self, identity_hash: str) -> str | None:
        """Return the most recently stored display name for an identity across all channels.

        Searches the members table without requiring a specific channel hash, so
        it works for any peer we have ever seen in any channel.
        """
        row = self._fetchone(
            "SELECT display_name FROM members WHERE identity_hash = ?"
            " AND display_name IS NOT NULL AND display_name != ''"
            " ORDER BY added_at DESC LIMIT 1",
            (identity_hash,),
        )
        return row["display_name"] if row else None

    def is_member(self, channel_hash: str, identity_hash: str) -> bool:
        return self._fetchone(
            "SELECT 1 FROM members WHERE channel_hash = ? AND identity_hash = ?",
            (channel_hash, identity_hash)
        ) is not None

    def is_admin(self, channel_hash: str, identity_hash: str) -> bool:
        """Backward-compatible check: True if the member is an admin or owner."""
        row = self._fetchone(
            "SELECT role FROM members WHERE channel_hash = ? AND identity_hash = ?",
            (channel_hash, identity_hash)
        )
        return bool(row and row["role"] in (ROLE_ADMIN, ROLE_OWNER))

    def get_role(self, channel_hash: str, identity_hash: str) -> str | None:
        """Return the member's role, or None if not a member."""
        row = self._fetchone(
            "SELECT role FROM members WHERE channel_hash = ? AND identity_hash = ?",
            (channel_hash, identity_hash)
        )
        return row["role"] if row else None

    def replace_members(self, channel_hash: str,
                        members: list[tuple[str, str, str | bool]]):
        """Replace the full member list for a channel atomically.

        members: list of (identity_hash, display_name, role).
        For backward compatibility *role* may be a bool (True → admin).
        """
        with self._tx():
            self._conn.execute(
                "DELETE FROM members WHERE channel_hash = ?", (channel_hash,)
            )
            now = time.time()
            rows = []
            for ih, dn, role_or_flag in members:
                if isinstance(role_or_flag, bool):
                    role_or_flag = ROLE_ADMIN if role_or_flag else ROLE_MEMBER
                rows.append((channel_hash, ih, dn, role_or_flag, now))
            self._conn.executemany("""
                INSERT INTO members (channel_hash, identity_hash, display_name, role, added_at)
                VALUES (?, ?, ?, ?, ?)
            """, rows)

    # --- channel permissions ---

    def get_channel_permissions(self, channel_hash: str) -> dict:
        """Return the parsed permissions dict for a channel."""
        row = self._fetchone(
            "SELECT permissions FROM channels WHERE hash = ?", (channel_hash,)
        )
        if not row or not row["permissions"]:
            return {}
        return permissions_from_json(row["permissions"])

    def set_channel_permissions(self, channel_hash: str, permissions: dict):
        with self._tx():
            self._conn.execute(
                "UPDATE channels SET permissions = ? WHERE hash = ?",
                (permissions_to_json(permissions), channel_hash),
            )

    def has_permission(self, channel_hash: str, identity_hash: str,
                       permission: str) -> bool:
        """Check whether a user has a specific permission on a channel."""
        role = self.get_role(channel_hash, identity_hash)
        if role is None:
            return False
        perms = self.get_channel_permissions(channel_hash)
        return _check_permission(perms, role, permission)

    # --- member list versions ---

    def get_member_list_version(self, channel_hash: str) -> sqlite3.Row | None:
        return self._fetchone(
            "SELECT * FROM member_list_versions WHERE channel_hash = ?",
            (channel_hash,)
        )

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
        return self._fetchall("""
            SELECT * FROM messages
            WHERE channel_hash = ? AND timestamp > ?
            ORDER BY timestamp ASC, received_at ASC
            LIMIT ?
        """, (channel_hash, since_ts, limit))

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
        rows = self._fetchall("""
            SELECT message_id FROM missed_deliveries
            WHERE channel_hash = ? AND recipient_hash = ?
        """, (channel_hash, recipient_hash))
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
