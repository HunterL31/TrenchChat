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

from trenchchat.core.identity import Identity
from trenchchat.core.naming import NameInUseError, channel_hash_for, sanitise_name
from trenchchat.core.permissions import (
    PRESET_OPEN, PRESET_PRIVATE, PRESETS, ROLE_OWNER,
    is_discoverable, is_open_join, permissions_from_json,
)
from trenchchat.core.storage import Storage
from trenchchat.network.router import Router

_sanitise_name = sanitise_name


class ChannelManager:
    """Creates, announces and discovers channels.

    The address a channel is announced under belongs to the transport; this
    manager owns which channels exist, what their metadata says, and whether
    any of it may be broadcast at all.
    """

    def __init__(self, identity: Identity, storage: Storage, router: Router):
        self._identity = identity
        self._storage = storage
        self._router = router
        self._owned: set[str] = set()
        self._discovered_callbacks: list = []
        router.add_channel_discovered_callback(self._on_channel_discovered)
        router.add_announce_callback(self.announce_all_owned)

    def add_channel_discovered_callback(self, callback):
        """callback(channel_hash_hex, channel_name): fired when a new public channel is heard."""
        if callback not in self._discovered_callbacks:
            self._discovered_callbacks.append(callback)

    def remove_channel_discovered_callback(self, callback):
        if callback in self._discovered_callbacks:
            self._discovered_callbacks.remove(callback)

    # --- create ---

    def create_channel(self, name: str, description: str = "",
                       access_mode: str = "public",
                       permissions: dict | None = None,
                       server_hash: str | None = None) -> str:
        """Create a new channel owned by the local identity.

        *permissions* is the full permissions dict.  For backward compat,
        *access_mode* (``"public"`` / ``"invite"``) is also accepted and
        converted to the matching preset.

        When *server_hash* is set the channel belongs to a server, which owns
        its membership, roles and tenure: no owner member row and no tenure
        interval are written here, and the channel is never announced.

        Returns the channel hash hex string.

        Raises NameInUseError when this identity already has a channel at the
        address *name* derives to.
        """
        if permissions is None:
            permissions = PRESETS.get(
                {"public": "open", "invite": "private"}.get(access_mode, access_mode),
                PRESET_PRIVATE,
            )

        aspect = _sanitise_name(name)
        hash_hex = channel_hash_for(self._identity.hash, name)
        if hash_hex in self._owned or \
                self._storage.get_channel(hash_hex) is not None:
            raise NameInUseError(f"you already have a channel named '{name}'")

        created_at = time.time()
        self._owned.add(hash_hex)
        self._router.register_channel(hash_hex, aspect)
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
        #
        # Gated to non-open-join channels only: public channels never use
        # the member-list/tenure system at all (membership there is tracked
        # by SubscriptionManager instead), so giving the owner a tenure row
        # would make has_any_tenure() true and wrongly engage tenure
        # filtering -- including the requester-side check -- for peers who
        # joined via subscription and have no tenure data of their own,
        # rejecting their sync requests entirely.
        if not is_open_join(permissions):
            self._storage.open_tenure(hash_hex, self._identity.hash_hex, created_at)
        self.announce_channel(hash_hex)
        return hash_hex

    # --- announce ---

    def announce_channel(self, channel_hash_hex: str,
                         attached_interface=None) -> None:
        """Announce a single owned channel.

        If attached_interface is given the announce is sent only on that
        interface; otherwise it is broadcast on all interfaces. Invite-only
        channels are never announced regardless of the discoverable flag --
        broadcasting them would leak their name/description/creator to any
        peer listening for trenchchat.channel announces, defeating the point
        of using a signed member-list document instead of mesh-wide
        discovery for them. discoverable and open_join are stored as
        independent flags (ChannelPermissionsDialog exposes both), so
        open_join must be checked here too rather than trusting discoverable
        alone -- otherwise toggling "Discoverable" on in the permissions
        dialog broadcasts an invite-only channel's existence to the whole
        mesh even though open_join stays off.
        """
        if channel_hash_hex not in self._owned:
            return
        channel = self._storage.get_channel(channel_hash_hex)
        if channel is None:
            return
        perms = permissions_from_json(channel["permissions"])
        if not is_discoverable(perms) or not is_open_join(perms):
            return
        access = "public" if is_open_join(perms) else "invite"
        app_data = msgpack.packb({
            "name": channel["name"],
            "description": channel["description"],
            "access": access,
            "creator": self._identity.hash_hex,
        }, use_bin_type=True)
        self._router.announce_channel(channel_hash_hex, app_data,
                                      attached_interface=attached_interface)

    def announce_all_owned(self, attached_interface=None) -> None:
        """Announce all owned channels.

        If attached_interface is given the announce is sent only on that
        interface; otherwise it is broadcast on all interfaces.
        """
        for hash_hex in list(self._owned):
            self.announce_channel(hash_hex, attached_interface=attached_interface)

    # --- discover ---

    def _on_channel_discovered(self, hash_hex: str, creator_hash: str,
                               metadata: dict, iface=None):
        """Record a channel heard on the mesh.

        creator_hash comes from the announcing identity, never from the
        payload: the destination hash is bound to that identity, while
        "creator" in the metadata is unsigned text -- and creator_hash goes on
        to serve as a trusted-signer fallback when validating member list
        documents.
        """
        name = metadata.get("name", hash_hex[:8])
        description = metadata.get("description", "")
        access_mode = metadata.get("access", "public")

        already_known = self._storage.get_channel(hash_hex) is not None
        if already_known:
            # Discovery metadata is unsigned and unversioned, so it may refresh
            # the presentation fields but never the permissions column: that is
            # governed by signed member list documents behind MANAGE_CHANNEL,
            # and letting an announce rewrite it turns open_join on for a
            # private channel -- after which the inbound message handler stops
            # checking membership at all.
            self._storage.update_discovered_metadata(hash_hex, name, description)
        else:
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

    # --- owned channels ---

    def is_owner(self, channel_hash_hex: str) -> bool:
        """Whether this node created the channel and holds its address."""
        return channel_hash_hex in self._owned

    def restore_owned_channels(self):
        """Re-claim the addresses of channels we created (called on startup)."""
        for row in self._storage.get_all_channels():
            if row["creator_hash"] == self._identity.hash_hex:
                aspect = _sanitise_name(row["name"])
                self._owned.add(row["hash"])
                self._router.register_channel(row["hash"], aspect)
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
