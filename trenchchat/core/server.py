"""
Server management: create and restore servers.

A server is a collection of channels that share one membership and one role
assignment -- one invite admits a peer to the server and therefore to every
channel in it. Its RNS.Destination aspect path is:
    trenchchat.server.<sanitised_name>

Servers are always invite-only and are never announced, so the destination
exists only to derive a hash that bakes in the creator's identity.

This manager deliberately registers no delivery callback. Server membership
travels in the same signed member-list document as channel membership, so all
wire handling -- validation, version ordering, roster materialisation -- stays
in InviteManager rather than being duplicated for a second scope kind.
"""

import time

import RNS

from trenchchat import APP_NAME, APP_ASPECT_SERVER
from trenchchat.core.identity import Identity
from trenchchat.core.naming import sanitise_name
from trenchchat.core.permissions import PRESET_SERVER, ROLE_OWNER
from trenchchat.core.storage import Storage


class ServerManager:
    def __init__(self, identity: Identity, storage: Storage):
        self._identity = identity
        self._storage = storage
        self._owned_destinations: dict[str, RNS.Destination] = {}

    def create_server(self, name: str, description: str = "",
                      permissions: dict | None = None) -> str:
        """Create a server owned by the local identity.

        Returns the server hash hex string.
        """
        if permissions is None:
            permissions = dict(PRESET_SERVER)

        dest = RNS.Destination(
            self._identity.rns_identity,
            RNS.Destination.IN,
            RNS.Destination.SINGLE,
            APP_NAME,
            APP_ASPECT_SERVER,
            sanitise_name(name),
        )
        hash_hex = dest.hash.hex()
        created_at = time.time()
        self._owned_destinations[hash_hex] = dest

        self._storage.upsert_server(
            hash=hash_hex,
            name=name,
            description=description,
            creator_hash=self._identity.hash_hex,
            permissions=permissions,
            created_at=created_at,
        )
        self._storage.upsert_member(
            channel_hash=hash_hex,
            identity_hash=self._identity.hash_hex,
            display_name=self._identity.display_name,
            role=ROLE_OWNER,
        )
        # Unconditional, unlike ChannelManager's open-join gate: servers are
        # always invite-only, so tenure always applies. Without it the owner's
        # own messages would be filtered out of every sync response.
        self._storage.open_tenure(hash_hex, self._identity.hash_hex, created_at)
        RNS.log(f"TrenchChat [server]: created '{name}' ({hash_hex[:12]}…)",
                RNS.LOG_NOTICE)
        return hash_hex

    def get_server(self, server_hash_hex: str):
        return self._storage.get_server(server_hash_hex)

    def list_servers(self) -> list:
        """Servers the local identity is a member of."""
        return [row for row in self._storage.get_all_servers()
                if self._storage.is_member(row["hash"], self._identity.hash_hex)]

    def is_owner(self, server_hash_hex: str) -> bool:
        return server_hash_hex in self._owned_destinations

    def restore_owned_servers(self) -> None:
        """Re-create RNS destinations for servers we created (called on startup)."""
        for row in self._storage.get_all_servers():
            if row["creator_hash"] != self._identity.hash_hex:
                continue
            dest = RNS.Destination(
                self._identity.rns_identity,
                RNS.Destination.IN,
                RNS.Destination.SINGLE,
                APP_NAME,
                APP_ASPECT_SERVER,
                sanitise_name(row["name"]),
            )
            self._owned_destinations[row["hash"]] = dest
