import json
import sqlite3
import shutil
import threading
import time
from pathlib import Path
from contextlib import contextmanager

import RNS

from trenchchat.config import DATA_DIR
from trenchchat.core.fileutils import secure_file
from trenchchat.core.lockbox import sqlcipher_hex_key
from trenchchat.core.permissions import (
    PRESET_OPEN, PRESET_PRIVATE, ROLE_ADMIN, ROLE_MEMBER, ROLE_OWNER,
    has_permission as _check_permission,
    permissions_from_json, permissions_to_json,
)

DB_PATH = DATA_DIR / "storage.db"


def _connect_plain(path: str) -> sqlite3.Connection:
    """Open a plain SQLite connection."""
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _connect_encrypted(path: str, raw_key: bytes) -> sqlite3.Connection:
    """Open a SQLCipher-encrypted connection with the given 32-byte raw key.

    The PRAGMA key must be issued immediately after connect and before any
    schema access; SQLCipher applies it to decrypt the file header.
    """
    import sqlcipher3.dbapi2 as _sqlcipher  # type: ignore[import]

    conn = _sqlcipher.connect(path, check_same_thread=False)
    conn.row_factory = _sqlcipher.Row
    hex_key = sqlcipher_hex_key(raw_key)
    conn.execute(f"PRAGMA key = \"x'{hex_key}'\"")
    return conn

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
    image_data   BLOB,
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

CREATE TABLE IF NOT EXISTS membership_tenure (
    channel_hash  TEXT NOT NULL,
    identity_hash TEXT NOT NULL,
    joined_at     REAL NOT NULL,
    left_at       REAL,
    PRIMARY KEY (channel_hash, identity_hash, joined_at)
);

CREATE INDEX IF NOT EXISTS idx_tenure_lookup
    ON membership_tenure(channel_hash, identity_hash, joined_at);

CREATE TABLE IF NOT EXISTS peer_avatars (
    identity_hash  TEXT PRIMARY KEY,
    avatar_data    BLOB NOT NULL,
    avatar_version INTEGER NOT NULL DEFAULT 0,
    updated_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS avatar_deliveries (
    identity_hash  TEXT PRIMARY KEY,
    avatar_version INTEGER NOT NULL,
    delivered_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS custom_emojis (
    emoji_hash   TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    image_data   BLOB NOT NULL,
    added_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS reactions (
    message_id    TEXT NOT NULL,
    emoji_hash    TEXT NOT NULL,
    reactor_hash  TEXT NOT NULL,
    channel_hash  TEXT NOT NULL,
    reacted_at    REAL NOT NULL,
    PRIMARY KEY (message_id, emoji_hash, reactor_hash)
);

CREATE INDEX IF NOT EXISTS idx_reactions_message
    ON reactions(message_id);
"""


class Storage:
    """SQLite-backed persistent store for TrenchChat.

    When *encryption_key* is supplied the database is opened via SQLCipher
    with the provided 32-byte raw key.  When None the stdlib sqlite3 module
    is used and the file is stored in plaintext (backward-compatible default).
    """

    def __init__(self, db_path: Path = DB_PATH,
                 encryption_key: bytes | None = None):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._path = str(db_path)
        self._encryption_key = encryption_key

        if encryption_key is not None:
            self._conn = _connect_encrypted(self._path, encryption_key)
        else:
            self._conn = _connect_plain(self._path)

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

        self._migrate_tenure()
        self._migrate_image_data()
        self._migrate_reactions()

    def _migrate_tenure(self):
        """Create membership_tenure table and backfill current members if the table is new."""
        # The table is created by SCHEMA, but it may be empty on first run with
        # an existing database.  Backfill open intervals from current members.
        count = self._conn.execute(
            "SELECT COUNT(*) FROM membership_tenure"
        ).fetchone()[0]
        if count == 0:
            rows = self._conn.execute(
                "SELECT channel_hash, identity_hash, added_at FROM members"
            ).fetchall()
            if rows:
                self._conn.executemany("""
                    INSERT OR IGNORE INTO membership_tenure
                        (channel_hash, identity_hash, joined_at, left_at)
                    VALUES (?, ?, ?, NULL)
                """, [(r["channel_hash"], r["identity_hash"], r["added_at"]) for r in rows])
                self._conn.commit()

    def _migrate_image_data(self):
        """Add image_data BLOB column to messages table for existing databases."""
        if not self._has_column("messages", "image_data"):
            self._conn.execute(
                "ALTER TABLE messages ADD COLUMN image_data BLOB"
            )
            self._conn.commit()

    def _migrate_reactions(self):
        """Create custom_emojis and reactions tables for existing databases."""
        tables = {r[0] for r in self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if "custom_emojis" not in tables:
            self._conn.execute("""
                CREATE TABLE custom_emojis (
                    emoji_hash   TEXT PRIMARY KEY,
                    name         TEXT NOT NULL,
                    image_data   BLOB NOT NULL,
                    added_at     REAL NOT NULL
                )
            """)
            self._conn.commit()
        if "reactions" not in tables:
            self._conn.execute("""
                CREATE TABLE reactions (
                    message_id    TEXT NOT NULL,
                    emoji_hash    TEXT NOT NULL,
                    reactor_hash  TEXT NOT NULL,
                    channel_hash  TEXT NOT NULL,
                    reacted_at    REAL NOT NULL,
                    PRIMARY KEY (message_id, emoji_hash, reactor_hash)
                )
            """)
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_reactions_message ON reactions(message_id)"
            )
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

    def close(self) -> None:
        """Close the underlying database connection."""
        self._conn.close()

    # ------------------------------------------------------------------
    # PIN migration helpers
    # ------------------------------------------------------------------

    def encrypt_database(self, new_key: bytes, db_path: Path = DB_PATH) -> None:
        """Re-key the database from plaintext to SQLCipher encryption.

        Opens the current (plain) database via sqlcipher3 (without issuing
        PRAGMA key so it reads the file as plaintext), attaches a new
        encrypted copy, copies all data via SQLCipher's ``sqlcipher_export``,
        then replaces the original file with the encrypted one.  The Storage
        instance must be closed before this is called.

        This is a class method-style helper called from the settings dialog
        when the user sets a PIN for the first time.
        """
        import sqlcipher3.dbapi2 as _sqlcipher  # type: ignore[import]

        enc_path = str(db_path) + ".encrypted"
        hex_key = sqlcipher_hex_key(new_key)

        # Open the plaintext DB with sqlcipher3 but WITHOUT issuing PRAGMA key;
        # sqlcipher3 can read plain sqlite files when no key pragma is issued.
        plain_conn = _sqlcipher.connect(str(db_path))
        try:
            plain_conn.execute(
                f"ATTACH DATABASE '{enc_path}' AS encrypted KEY \"x'{hex_key}'\""
            )
            plain_conn.execute("SELECT sqlcipher_export('encrypted')")
            plain_conn.execute("DETACH DATABASE encrypted")
        finally:
            plain_conn.close()

        shutil.move(enc_path, str(db_path))
        secure_file(db_path)
        RNS.log("TrenchChat [storage]: database encrypted with PIN key", RNS.LOG_NOTICE)

    def decrypt_database(self, current_key: bytes, db_path: Path = DB_PATH) -> None:
        """Export the database from SQLCipher back to plaintext.

        Used when the user removes their PIN.  The Storage instance must be
        closed before this is called.
        """
        import sqlcipher3.dbapi2 as _sqlcipher  # type: ignore[import]

        plain_path = str(db_path) + ".plain"
        hex_key = sqlcipher_hex_key(current_key)

        enc_conn = _sqlcipher.connect(str(db_path))
        try:
            enc_conn.execute(f"PRAGMA key = \"x'{hex_key}'\"")
            enc_conn.execute(
                f"ATTACH DATABASE '{plain_path}' AS plaintext KEY ''"
            )
            enc_conn.execute("SELECT sqlcipher_export('plaintext')")
            enc_conn.execute("DETACH DATABASE plaintext")
        finally:
            enc_conn.close()

        shutil.move(plain_path, str(db_path))
        secure_file(db_path)
        RNS.log("TrenchChat [storage]: database decrypted (PIN removed)", RNS.LOG_NOTICE)

    def rekey_database(self, old_key: bytes, new_key: bytes,
                       db_path: Path = DB_PATH) -> None:
        """Change the SQLCipher key (used when the user changes their PIN).

        The Storage instance must be closed before this is called.
        """
        import sqlcipher3.dbapi2 as _sqlcipher  # type: ignore[import]

        new_hex = sqlcipher_hex_key(new_key)

        conn = _connect_encrypted(str(db_path), old_key)
        try:
            conn.execute(f"PRAGMA rekey = \"x'{new_hex}'\"")
        finally:
            conn.close()

        RNS.log("TrenchChat [storage]: database re-keyed with new PIN", RNS.LOG_NOTICE)

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
                       received_at: float,
                       image_data: bytes | None = None) -> bool:
        """Returns True if inserted, False if duplicate."""
        try:
            with self._tx():
                self._conn.execute("""
                    INSERT INTO messages
                        (channel_hash, sender_hash, sender_name, content, timestamp,
                         message_id, reply_to, last_seen_id, received_at, image_data)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (channel_hash, sender_hash, sender_name, content, timestamp,
                      message_id, reply_to, last_seen_id, received_at, image_data))
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

    # --- membership tenure ---

    def open_tenure(self, channel_hash: str, identity_hash: str, joined_at: float):
        """Record that an identity joined a channel at *joined_at*.

        Inserts a new open interval row (left_at IS NULL).  If an identical
        (channel_hash, identity_hash, joined_at) row already exists it is left
        unchanged (idempotent).
        """
        with self._tx():
            self._conn.execute("""
                INSERT OR IGNORE INTO membership_tenure
                    (channel_hash, identity_hash, joined_at, left_at)
                VALUES (?, ?, ?, NULL)
            """, (channel_hash, identity_hash, joined_at))

    def close_tenure(self, channel_hash: str, identity_hash: str, left_at: float):
        """Close the most recent open tenure interval for an identity on a channel.

        Sets left_at on the row with the highest joined_at that still has
        left_at IS NULL.  If no open interval exists, does nothing.
        """
        with self._tx():
            self._conn.execute("""
                UPDATE membership_tenure
                SET left_at = ?
                WHERE channel_hash = ? AND identity_hash = ? AND left_at IS NULL
                  AND joined_at = (
                      SELECT MAX(joined_at) FROM membership_tenure
                      WHERE channel_hash = ? AND identity_hash = ? AND left_at IS NULL
                  )
            """, (left_at, channel_hash, identity_hash, channel_hash, identity_hash))

    def update_tenure(self, channel_hash: str, old_members: set[str],
                      new_members: set[str], published_at: float):
        """Diff old vs new member sets and update tenure records accordingly.

        Members in old_members but not new_members get their open interval
        closed at published_at.  Members in new_members but not old_members
        get a new open interval starting at published_at.  Members in both
        sets are unchanged.
        """
        removed = old_members - new_members
        added = new_members - old_members
        with self._tx():
            for ih in removed:
                self._conn.execute("""
                    UPDATE membership_tenure
                    SET left_at = ?
                    WHERE channel_hash = ? AND identity_hash = ? AND left_at IS NULL
                      AND joined_at = (
                          SELECT MAX(joined_at) FROM membership_tenure
                          WHERE channel_hash = ? AND identity_hash = ? AND left_at IS NULL
                      )
                """, (published_at, channel_hash, ih, channel_hash, ih))
            for ih in added:
                self._conn.execute("""
                    INSERT OR IGNORE INTO membership_tenure
                        (channel_hash, identity_hash, joined_at, left_at)
                    VALUES (?, ?, ?, NULL)
                """, (channel_hash, ih, published_at))

    def was_member_at(self, channel_hash: str, identity_hash: str,
                      timestamp: float) -> bool:
        """Return True if the identity was a member of the channel at *timestamp*.

        Checks whether any tenure interval covers the given timestamp:
        joined_at <= timestamp < left_at  (or left_at IS NULL for open intervals).
        Returns False if no tenure data exists for the identity (unknown history).
        """
        row = self._fetchone("""
            SELECT 1 FROM membership_tenure
            WHERE channel_hash = ? AND identity_hash = ?
              AND joined_at <= ? AND (left_at IS NULL OR left_at > ?)
            LIMIT 1
        """, (channel_hash, identity_hash, timestamp, timestamp))
        return row is not None

    def has_any_tenure(self, channel_hash: str) -> bool:
        """Return True if the membership_tenure table has any rows for this channel.

        Used to decide whether to apply tenure checks — if no tenure data exists
        (e.g. an open-join channel or a channel bootstrapped before this feature),
        tenure checks are skipped rather than incorrectly rejecting all messages.
        """
        row = self._fetchone(
            "SELECT 1 FROM membership_tenure WHERE channel_hash = ? LIMIT 1",
            (channel_hash,)
        )
        return row is not None

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

    # --- peer avatars ---

    def upsert_peer_avatar(self, identity_hash: str, avatar_data: bytes,
                           avatar_version: int) -> None:
        """Store or update the cached avatar for a peer identity."""
        with self._tx():
            self._conn.execute("""
                INSERT INTO peer_avatars
                    (identity_hash, avatar_data, avatar_version, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(identity_hash) DO UPDATE SET
                    avatar_data=excluded.avatar_data,
                    avatar_version=excluded.avatar_version,
                    updated_at=excluded.updated_at
            """, (identity_hash, avatar_data, avatar_version, time.time()))

    def get_peer_avatar(self, identity_hash: str) -> dict | None:
        """Return cached avatar info for a peer, or None if not stored.

        Result dict has keys: identity_hash, avatar_data, avatar_version, updated_at.
        """
        row = self._fetchone(
            "SELECT * FROM peer_avatars WHERE identity_hash = ?",
            (identity_hash,)
        )
        return dict(row) if row else None

    def delete_peer_avatar(self, identity_hash: str) -> None:
        """Remove a peer's cached avatar."""
        with self._tx():
            self._conn.execute(
                "DELETE FROM peer_avatars WHERE identity_hash = ?",
                (identity_hash,)
            )

    # --- avatar delivery tracking ---

    def upsert_avatar_delivery(self, identity_hash: str, avatar_version: int) -> None:
        """Record that a peer has received our avatar at the given version."""
        with self._tx():
            self._conn.execute("""
                INSERT INTO avatar_deliveries
                    (identity_hash, avatar_version, delivered_at)
                VALUES (?, ?, ?)
                ON CONFLICT(identity_hash) DO UPDATE SET
                    avatar_version=excluded.avatar_version,
                    delivered_at=excluded.delivered_at
            """, (identity_hash, avatar_version, time.time()))

    def get_avatar_delivery_version(self, identity_hash: str) -> int | None:
        """Return the avatar version last delivered to a peer, or None if never sent."""
        row = self._fetchone(
            "SELECT avatar_version FROM avatar_deliveries WHERE identity_hash = ?",
            (identity_hash,)
        )
        return row["avatar_version"] if row else None

    def clear_avatar_deliveries(self) -> None:
        """Remove all delivery records. Called when our own avatar changes."""
        with self._tx():
            self._conn.execute("DELETE FROM avatar_deliveries")

    # --- custom emojis ---

    def insert_emoji(self, emoji_hash: str, name: str,
                     image_data: bytes, added_at: float) -> bool:
        """Store a custom emoji. Returns True if inserted, False if the hash already exists."""
        try:
            with self._tx():
                self._conn.execute("""
                    INSERT INTO custom_emojis (emoji_hash, name, image_data, added_at)
                    VALUES (?, ?, ?, ?)
                """, (emoji_hash, name, image_data, added_at))
            return True
        except sqlite3.IntegrityError:
            return False

    def get_emoji(self, emoji_hash: str) -> sqlite3.Row | None:
        """Return the custom emoji row for a given hash, or None if not found."""
        return self._fetchone(
            "SELECT * FROM custom_emojis WHERE emoji_hash = ?", (emoji_hash,)
        )

    def search_emojis(self, query: str) -> list[sqlite3.Row]:
        """Return emojis whose name contains query (case-insensitive), up to 20 results."""
        return self._fetchall(
            "SELECT * FROM custom_emojis WHERE name LIKE ? ORDER BY name LIMIT 20",
            (f"%{query}%",),
        )

    def list_emojis(self) -> list[sqlite3.Row]:
        """Return all custom emojis ordered by name."""
        return self._fetchall("SELECT * FROM custom_emojis ORDER BY name")

    def delete_emoji(self, emoji_hash: str) -> None:
        """Remove a custom emoji from the local library."""
        with self._tx():
            self._conn.execute(
                "DELETE FROM custom_emojis WHERE emoji_hash = ?", (emoji_hash,)
            )

    def emoji_exists(self, emoji_hash: str) -> bool:
        """Return True if the emoji hash is already in the local library."""
        return self._fetchone(
            "SELECT 1 FROM custom_emojis WHERE emoji_hash = ?", (emoji_hash,)
        ) is not None

    # --- reactions ---

    def insert_reaction(self, message_id: str, emoji_hash: str,
                        reactor_hash: str, channel_hash: str,
                        reacted_at: float) -> bool:
        """Record a reaction. Returns True if inserted, False if it already exists."""
        try:
            with self._tx():
                self._conn.execute("""
                    INSERT INTO reactions
                        (message_id, emoji_hash, reactor_hash, channel_hash, reacted_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (message_id, emoji_hash, reactor_hash, channel_hash, reacted_at))
            return True
        except sqlite3.IntegrityError:
            return False

    def remove_reaction(self, message_id: str, emoji_hash: str,
                        reactor_hash: str) -> None:
        """Remove a reaction row."""
        with self._tx():
            self._conn.execute("""
                DELETE FROM reactions
                WHERE message_id = ? AND emoji_hash = ? AND reactor_hash = ?
            """, (message_id, emoji_hash, reactor_hash))

    def get_reactions(self, message_id: str) -> list[sqlite3.Row]:
        """Return all reactions for a message, ordered by earliest first."""
        return self._fetchall(
            "SELECT * FROM reactions WHERE message_id = ? ORDER BY reacted_at ASC",
            (message_id,),
        )
