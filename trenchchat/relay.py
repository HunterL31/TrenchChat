"""
TrenchChat Voice Relay daemon.

A headless, always-on process that hosts voice channels on behalf of channel
owners.  It has no GUI, no text messaging, and no Qt dependency.  It accepts
relay assignments from channel owners via LXMF control messages, creates RNS
voice destinations, performs N-1 audio mixing, and tears down when revoked.

Usage:
    python -m trenchchat.relay [--config /path/to/relay/config]

The relay has its own persistent Reticulum identity stored separately from any
user identity.  Multiple relays can coexist on the same machine using different
config directories.

Relay-local SQLite database (stored at ~/.trenchchat-relay/relay.db):
    relay_channels: assigned channel hashes, owner identities, member lists
"""

import os
import sys
import time
import signal
import sqlite3
import argparse
import threading
from pathlib import Path
from contextlib import contextmanager

import RNS
import LXMF
import msgpack

from trenchchat import APP_NAME, APP_ASPECT_RELAY
from trenchchat.config import Config
from trenchchat.core.identity import Identity
from trenchchat.core.voice import VoiceManager

_DEFAULT_RELAY_DATA_DIR = Path.home() / ".trenchchat-relay"
_ANNOUNCE_INTERVAL_SECS = 60


class RelayStorage:
    """Minimal SQLite store for relay-assigned channels.

    Used only by the relay daemon, not the main TrenchChat client.
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS relay_channels (
        channel_hash    TEXT PRIMARY KEY,
        owner_hash      TEXT NOT NULL,
        member_list_doc BLOB NOT NULL DEFAULT x'',
        permissions     TEXT NOT NULL DEFAULT '{}',
        assigned_at     REAL NOT NULL
    );
    """

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(self.SCHEMA)
        self._conn.commit()
        self._lock = threading.RLock()

    @contextmanager
    def _tx(self):
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def upsert_relay_channel(self, channel_hash: str, owner_hash: str,
                              member_list_doc: bytes) -> None:
        """Store or update a relay channel assignment."""
        with self._tx():
            self._conn.execute("""
                INSERT INTO relay_channels
                    (channel_hash, owner_hash, member_list_doc, assigned_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(channel_hash) DO UPDATE SET
                    owner_hash=excluded.owner_hash,
                    member_list_doc=excluded.member_list_doc,
                    assigned_at=excluded.assigned_at
            """, (channel_hash, owner_hash, member_list_doc, time.time()))

    def update_relay_member_list(self, channel_hash: str, member_list_doc: bytes) -> None:
        """Update the stored member list for an assigned channel."""
        with self._tx():
            self._conn.execute(
                "UPDATE relay_channels SET member_list_doc = ? WHERE channel_hash = ?",
                (member_list_doc, channel_hash),
            )

    def delete_relay_channel(self, channel_hash: str) -> None:
        """Remove a relay channel assignment."""
        with self._tx():
            self._conn.execute(
                "DELETE FROM relay_channels WHERE channel_hash = ?", (channel_hash,)
            )

    def get_all_relay_channels(self) -> list[sqlite3.Row]:
        """Return all assigned channel records."""
        with self._lock:
            return self._conn.execute("SELECT * FROM relay_channels").fetchall()

    def has_permission(self, channel_hash: str, identity_hash: str,
                       permission: str) -> bool:
        """Check whether an identity has permission on a relay-hosted channel.

        The channel owner always has all permissions. For other identities,
        parses the stored member list doc. Falls back to False if no data is
        available (safe default).
        """
        import json
        row = self._conn.execute(
            "SELECT owner_hash, member_list_doc, permissions FROM relay_channels "
            "WHERE channel_hash = ?",
            (channel_hash,)
        ).fetchone()
        if row is None:
            return False

        # The channel owner always has all permissions.
        if row["owner_hash"] == identity_hash:
            return True

        # Parse member list document to get the identity's role.
        doc_blob = row["member_list_doc"]
        if not doc_blob:
            return False
        try:
            doc = msgpack.unpackb(doc_blob, raw=True)
            members = doc.get(b"members", {})
            member_entry = members.get(identity_hash.encode())
            if member_entry is None:
                return False
            role = member_entry.get(b"role", b"member")
            if isinstance(role, bytes):
                role = role.decode(errors="replace")
        except Exception:
            return False

        # Owner role in member list also grants all permissions.
        if role == "owner":
            return True

        perms_json = row["permissions"] or "{}"
        try:
            perms = json.loads(perms_json)
        except Exception:
            return False

        return permission in perms.get(role, [])

    def get_member_list_version(self, channel_hash: str):
        """Stub for VoiceManager compatibility. Returns None (relay has no version table)."""
        return None

    def get_members(self, channel_hash: str) -> list:
        """Return a list of member dicts parsed from the stored member list document.

        Used by VoiceManager._broadcast_voice_state to enumerate recipients.
        """
        row = self._conn.execute(
            "SELECT member_list_doc FROM relay_channels WHERE channel_hash = ?",
            (channel_hash,)
        ).fetchone()
        if row is None or not row["member_list_doc"]:
            return []
        try:
            doc = msgpack.unpackb(row["member_list_doc"], raw=True)
            members = doc.get(b"members", {})
            result = []
            for ih_bytes, entry in members.items():
                ih = ih_bytes.decode(errors="replace") if isinstance(ih_bytes, bytes) else ih_bytes
                result.append({"identity_hash": ih, "display_name": ""})
            return result
        except Exception:
            return []

    def get_display_name_for_identity(self, identity_hash: str) -> str | None:
        """Relay does not store display names; return None."""
        return None

    def get_voice_channels(self) -> list[sqlite3.Row]:
        """Return all assigned relay channels (used during restore on startup)."""
        return self.get_all_relay_channels()

    def get_channel(self, channel_hash: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT channel_hash AS hash, owner_hash AS creator_hash, "
                "NULL AS relay_dest_hash, 'voice' AS channel_type "
                "FROM relay_channels WHERE channel_hash = ?",
                (channel_hash,)
            ).fetchone()

    def set_channel_relay(self, channel_hash: str, relay_dest_hash) -> None:
        """No-op for relay storage (relay doesn't assign sub-relays)."""

    def close(self) -> None:
        self._conn.close()


class RelayAnnounceHandler:
    """Announce handler registering this node as a TrenchChat Voice Relay."""

    aspect_filter = f"{APP_NAME}.{APP_ASPECT_RELAY}"

    def received_announce(self, destination_hash: bytes,
                          announced_identity: RNS.Identity,
                          app_data: bytes) -> None:
        """Relay discovers other relays -- currently no action needed."""


def _run_relay(data_dir: Path, rns_config_dir: str | None = None) -> None:
    """Main relay daemon entry point."""
    data_dir.mkdir(parents=True, exist_ok=True)

    if rns_config_dir is None:
        rns_config_dir = str(data_dir / "rns_config")

    RNS.log(f"TrenchChat [relay]: starting, data dir: {data_dir}", RNS.LOG_NOTICE)
    RNS.log(f"TrenchChat [relay]: using RNS config: {rns_config_dir}", RNS.LOG_NOTICE)

    rns = RNS.Reticulum(configdir=rns_config_dir)

    # Relay identity is separate from any user identity.
    identity_path = data_dir / "relay_identity"
    if identity_path.exists():
        relay_identity = RNS.Identity.from_file(str(identity_path))
        RNS.log(
            f"TrenchChat [relay]: loaded identity {relay_identity.hash.hex()}",
            RNS.LOG_NOTICE,
        )
    else:
        relay_identity = RNS.Identity()
        relay_identity.to_file(str(identity_path))
        RNS.log(
            f"TrenchChat [relay]: created new identity {relay_identity.hash.hex()}",
            RNS.LOG_NOTICE,
        )

    # LXMF router for control messages.
    router = LXMF.LXMRouter(storagepath=str(data_dir / "messagestore"))
    delivery_dest = router.register_delivery_identity(relay_identity, display_name="TrenchChat Relay")
    RNS.log(
        f"TrenchChat [relay]: LXMF delivery destination {delivery_dest.hash.hex()}",
        RNS.LOG_NOTICE,
    )

    storage = RelayStorage(data_dir / "relay.db")

    # Minimal identity wrapper for VoiceManager.
    class _RelayIdentity:
        def __init__(self, rns_id: RNS.Identity):
            self.rns_identity = rns_id
            self.hash_hex = rns_id.hash.hex()
            self.display_name = "TrenchChat Relay"

    identity = _RelayIdentity(relay_identity)

    # Minimal router wrapper for VoiceManager._send_lxmf.
    class _RelayRouter:
        def __init__(self, lxm_router, dest):
            self._lxm_router = lxm_router
            self.delivery_destination = dest

        def send(self, lxm: LXMF.LXMessage) -> None:
            self._lxm_router.handle_outbound(lxm)

        def add_message_handler(self, handler) -> None:
            router.register_delivery_callback(handler)

    relay_router = _RelayRouter(router, delivery_dest)

    voice_mgr = VoiceManager(identity, storage, relay_router, host_only=True)

    # Wire up inbound LXMF handler.
    def _delivery_callback(message: LXMF.LXMessage) -> None:
        voice_mgr._on_lxmf_message(message)

    router.register_delivery_callback(_delivery_callback)

    # Restore previously assigned channels.
    for row in storage.get_all_relay_channels():
        channel_hash = row["channel_hash"]
        voice_mgr.create_voice_destination(channel_hash)
        RNS.log(
            f"TrenchChat [relay]: restored voice destination for channel {channel_hash[:8]}",
            RNS.LOG_NOTICE,
        )

    # Announce as a relay node.
    relay_dest = RNS.Destination(
        relay_identity,
        RNS.Destination.IN,
        RNS.Destination.SINGLE,
        APP_NAME,
        APP_ASPECT_RELAY,
    )
    relay_dest.announce()

    # Also announce the LXMF delivery destination so clients can resolve this
    # relay's identity via RNS.Identity.recall() before sending assignment messages.
    router.announce(delivery_dest.hash)

    RNS.log("TrenchChat [relay]: started, waiting for assignments", RNS.LOG_NOTICE)

    stop_event = threading.Event()

    def _sighandler(signum, frame):
        RNS.log("TrenchChat [relay]: shutting down", RNS.LOG_NOTICE)
        stop_event.set()

    signal.signal(signal.SIGINT, _sighandler)
    signal.signal(signal.SIGTERM, _sighandler)

    # Re-announce periodically so owners can discover the relay.
    while not stop_event.is_set():
        stop_event.wait(timeout=_ANNOUNCE_INTERVAL_SECS)
        if not stop_event.is_set():
            relay_dest.announce()
            router.announce(delivery_dest.hash)
            RNS.log("TrenchChat [relay]: re-announced", RNS.LOG_DEBUG)

    storage.close()
    RNS.log("TrenchChat [relay]: stopped", RNS.LOG_NOTICE)


def main() -> None:
    """Entry point for the Voice Relay daemon."""
    parser = argparse.ArgumentParser(description="TrenchChat Voice Relay daemon")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=_DEFAULT_RELAY_DATA_DIR,
        help=f"Directory for relay identity and database (default: {_DEFAULT_RELAY_DATA_DIR})",
    )
    parser.add_argument(
        "--rns-config",
        type=str,
        default=None,
        metavar="DIR",
        help=(
            "Reticulum config directory to use. "
            "Set this to the same config as your TrenchChat client so the relay and "
            "client can reach each other on the same machine (e.g. ~/.reticulum). "
            "Defaults to <data-dir>/rns_config (its own isolated RNS instance)."
        ),
    )
    args = parser.parse_args()
    _run_relay(args.data_dir, rns_config_dir=args.rns_config)


if __name__ == "__main__":
    main()
