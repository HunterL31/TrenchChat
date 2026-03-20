"""
Channel management: create, announce, and discover channels.

A channel is an RNS.Destination(SINGLE) whose aspect path is:
    trenchchat.channel.<sanitised_name>

The channel hash is its globally unique address derived from the
creator's identity + the aspect path.
"""

import time
import RNS
import msgpack

from trenchchat import APP_NAME, APP_ASPECT_CHANNEL
from trenchchat.core.identity import Identity
from trenchchat.core.permissions import (
    PRESET_OPEN, PRESET_PRIVATE, PRESETS, ROLE_OWNER,
    is_discoverable, is_open_join, permissions_from_json,
)
from trenchchat.core.storage import Storage
from trenchchat.network.announce import ChannelAnnounceHandler


def _sanitise_name(name: str) -> str:
    """Lower-case, alphanumeric + hyphens only, max 32 chars."""
    sanitised = "".join(c if c.isalnum() or c == "-" else "-" for c in name.lower())
    return sanitised[:32].strip("-")


class ChannelManager:
    def __init__(self, identity: Identity, storage: Storage):
        self._identity = identity
        self._storage = storage
        self._owned_destinations: dict[str, RNS.Destination] = {}
        self._discovered_callbacks: list = []
        self._announce_handler = ChannelAnnounceHandler(self._on_channel_discovered)
        RNS.Transport.register_announce_handler(self._announce_handler)

    def add_channel_discovered_callback(self, callback):
        """callback(channel_hash_hex, channel_name) — fired when a new public channel is heard."""
        if callback not in self._discovered_callbacks:
            self._discovered_callbacks.append(callback)

    def remove_channel_discovered_callback(self, callback):
        if callback in self._discovered_callbacks:
            self._discovered_callbacks.remove(callback)

    # --- create ---

    def create_channel(self, name: str, description: str = "",
                       access_mode: str = "public",
                       permissions: dict | None = None) -> str:
        """Create a new channel owned by the local identity.

        *permissions* is the full permissions dict.  For backward compat,
        *access_mode* (``"public"`` / ``"invite"``) is also accepted and
        converted to the matching preset.

        Returns the channel hash hex string.
        """
        if permissions is None:
            permissions = PRESETS.get(
                {"public": "open", "invite": "private"}.get(access_mode, access_mode),
                PRESET_PRIVATE,
            )

        aspect = _sanitise_name(name)
        dest = RNS.Destination(
            self._identity.rns_identity,
            RNS.Destination.IN,
            RNS.Destination.SINGLE,
            APP_NAME,
            APP_ASPECT_CHANNEL,
            aspect,
        )
        hash_hex = dest.hash.hex()

        self._owned_destinations[hash_hex] = dest
        self._storage.upsert_channel(
            hash=hash_hex,
            name=name,
            description=description,
            creator_hash=self._identity.hash_hex,
            permissions=permissions,
            created_at=time.time(),
        )
        self._storage.subscribe(hash_hex)
        self._storage.upsert_member(
            channel_hash=hash_hex,
            identity_hash=self._identity.hash_hex,
            display_name=self._identity.display_name,
            role=ROLE_OWNER,
        )
        self.announce_channel(hash_hex)
        return hash_hex

    # --- announce ---

    def announce_channel(self, channel_hash_hex: str):
        dest = self._owned_destinations.get(channel_hash_hex)
        if dest is None:
            return
        channel = self._storage.get_channel(channel_hash_hex)
        if channel is None:
            return
        perms = permissions_from_json(channel["permissions"])
        access = "public" if is_open_join(perms) else "invite"
        app_data = msgpack.packb({
            "name": channel["name"],
            "description": channel["description"],
            "access": access,
            "creator": self._identity.hash_hex,
        }, use_bin_type=True)
        dest.announce(app_data=app_data)

    def announce_all_owned(self):
        for hash_hex in self._owned_destinations:
            self.announce_channel(hash_hex)

    # --- discover ---

    def _on_channel_discovered(self, destination_hash: bytes,
                                announced_identity: RNS.Identity,
                                metadata: dict):
        hash_hex = destination_hash.hex()
        name = metadata.get("name", hash_hex[:8])
        description = metadata.get("description", "")
        access_mode = metadata.get("access", "public")
        creator_hash = metadata.get("creator", announced_identity.hash.hex()
                                    if announced_identity else "")

        already_known = self._storage.get_channel(hash_hex) is not None
        self._storage.upsert_channel(
            hash=hash_hex,
            name=name,
            description=description,
            creator_hash=creator_hash,
            access_mode=access_mode,
            created_at=time.time(),
        )

        channel = self._storage.get_channel(hash_hex)
        perms = permissions_from_json(channel["permissions"]) if channel else {}
        if not already_known and is_discoverable(perms):
            for cb in self._discovered_callbacks:
                try:
                    cb(hash_hex, name)
                except Exception as e:
                    RNS.log(f"TrenchChat: channel discovered callback error: {e}",
                            RNS.LOG_ERROR)

    # --- owned channel destination lookup ---

    def get_owned_destination(self, channel_hash_hex: str) -> RNS.Destination | None:
        return self._owned_destinations.get(channel_hash_hex)

    def is_owner(self, channel_hash_hex: str) -> bool:
        return channel_hash_hex in self._owned_destinations

    def restore_owned_channels(self):
        """Re-create RNS destinations for channels we created (called on startup)."""
        for row in self._storage.get_all_channels():
            if row["creator_hash"] == self._identity.hash_hex:
                aspect = _sanitise_name(row["name"])
                dest = RNS.Destination(
                    self._identity.rns_identity,
                    RNS.Destination.IN,
                    RNS.Destination.SINGLE,
                    APP_NAME,
                    APP_ASPECT_CHANNEL,
                    aspect,
                )
                self._owned_destinations[row["hash"]] = dest
                self._storage.upsert_member(
                    channel_hash=row["hash"],
                    identity_hash=self._identity.hash_hex,
                    display_name=self._identity.display_name,
                    role=ROLE_OWNER,
                )
