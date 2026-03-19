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
                       access_mode: str = "public") -> str:
        """
        Create a new channel owned by the local identity.
        Returns the channel hash hex string.
        """
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
            access_mode=access_mode,
            created_at=time.time(),
        )
        self._storage.subscribe(hash_hex)
        # Always add the creator as an admin member so access checks work
        # immediately, even before a full member list document is published.
        self._storage.upsert_member(
            channel_hash=hash_hex,
            identity_hash=self._identity.hash_hex,
            display_name=self._identity.display_name,
            is_admin=True,
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
        app_data = msgpack.packb({
            "name": channel["name"],
            "description": channel["description"],
            "access": channel["access_mode"],
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

        # Notify UI about newly discovered public channels so user can choose to join.
        if not already_known and access_mode == "public":
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
                # Ensure creator is always present as admin in the members table
                self._storage.upsert_member(
                    channel_hash=row["hash"],
                    identity_hash=self._identity.hash_hex,
                    display_name=self._identity.display_name,
                    is_admin=True,
                )
