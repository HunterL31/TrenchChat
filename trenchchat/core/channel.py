"""
Channel management: create and restore channels.

A channel is an RNS.Destination(SINGLE) whose aspect path is:
    trenchchat.channel.<sanitised_name>

The channel hash is its globally unique address derived from the
creator's identity + the aspect path.

Channels are always invite-only and are never announced. Membership travels
in a signed member-list document (core/invite.py), so the destination exists
only to derive a hash that bakes in the creator's identity, the same shape
servers have. Public chat is RRC and lives in core/rrc.py; see docs/rrc.md.
"""

import time
import RNS

from trenchchat import APP_NAME, APP_ASPECT_CHANNEL
from trenchchat.core.identity import Identity
from trenchchat.core.naming import NameInUseError, channel_hash_for, sanitise_name
from trenchchat.core.permissions import PRESET_PRIVATE, ROLE_OWNER
from trenchchat.core.storage import Storage

_sanitise_name = sanitise_name


class ChannelManager:
    def __init__(self, identity: Identity, storage: Storage):
        self._identity = identity
        self._storage = storage
        self._owned_destinations: dict[str, RNS.Destination] = {}

    # --- create ---

    def create_channel(self, name: str, description: str = "",
                       permissions: dict | None = None,
                       server_hash: str | None = None) -> str:
        """Create a new channel owned by the local identity.

        When *server_hash* is set the channel belongs to a server, which owns
        its membership, roles and tenure: no owner member row and no tenure
        interval are written here.

        Returns the channel hash hex string.

        Raises NameInUseError when this identity already has a channel at the
        address *name* derives to.
        """
        if permissions is None:
            permissions = dict(PRESET_PRIVATE)

        aspect = _sanitise_name(name)
        hash_hex = channel_hash_for(self._identity.hash, name)
        if hash_hex in self._owned_destinations or \
                self._storage.get_channel(hash_hex) is not None:
            raise NameInUseError(f"you already have a channel named '{name}'")

        dest = RNS.Destination(
            self._identity.rns_identity,
            RNS.Destination.IN,
            RNS.Destination.SINGLE,
            APP_NAME,
            APP_ASPECT_CHANNEL,
            aspect,
        )

        created_at = time.time()
        self._owned_destinations[hash_hex] = dest
        self._storage.upsert_channel(
            hash=hash_hex,
            name=name,
            description=description,
            creator_hash=self._identity.hash_hex,
            permissions=permissions,
            created_at=created_at,
            server_hash=server_hash,
        )
        self._storage.subscribe(hash_hex)
        if server_hash is not None:
            return hash_hex
        self._storage.upsert_member(
            channel_hash=hash_hex,
            identity_hash=self._identity.hash_hex,
            display_name=self._identity.display_name,
            role=ROLE_OWNER,
        )
        # Without this, the owner has no tenure record at all, and
        # was_member_at() treats "no tenure data" as "wasn't a member" --
        # silently dropping the owner's own messages from every sync
        # response to new members, regardless of when those members
        # actually joined. Uses created_at rather than a fresh time.time()
        # call so the tenure interval starts at the exact moment the channel
        # itself was created, not some microseconds-later timestamp.
        self._storage.open_tenure(hash_hex, self._identity.hash_hex, created_at)
        return hash_hex

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
                # A channel inside a server has no member rows of its own --
                # the server owns them, and writing one here would be invisible
                # to every resolving read anyway.
                if row["server_hash"]:
                    continue
                self._storage.upsert_member(
                    channel_hash=row["hash"],
                    identity_hash=self._identity.hash_hex,
                    display_name=self._identity.display_name,
                    role=ROLE_OWNER,
                )
