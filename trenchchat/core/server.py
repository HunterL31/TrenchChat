"""
Server management: create servers and say which ones this identity owns.

A server is a collection of channels that share one membership and one role
assignment -- one invite admits a peer to the server and therefore to every
channel in it. Its address is the hash of the aspect path
``trenchchat.server.<sanitised_name>`` under the creator's identity, derived
by core/naming.py.

Servers are always invite-only and are never announced, so there is no
destination to own: the hash is all that is needed, and it is computable
offline by anyone holding the creator's identity hash.

This manager deliberately registers no delivery callback. Server membership
travels in the same signed member-list document as channel membership, so all
wire handling -- validation, version ordering, roster materialisation -- stays
in InviteManager rather than being duplicated for a second scope kind.
"""

import time

import RNS

from trenchchat.core.identity import Identity
from trenchchat.core.naming import NameInUseError, server_hash_for
from trenchchat.core.permissions import PRESET_SERVER, ROLE_OWNER
from trenchchat.core.storage import Storage


class ServerManager:
    """Creates servers and answers what this identity owns."""

    def __init__(self, identity: Identity, storage: Storage):
        self._identity = identity
        self._storage = storage

    def create_server(self, name: str, description: str = "",
                      permissions: dict | None = None) -> str:
        """Create a server owned by the local identity.

        Returns the server hash hex string.

        Raises NameInUseError when this identity already has a server at the
        address *name* derives to.
        """
        if permissions is None:
            permissions = dict(PRESET_SERVER)

        hash_hex = server_hash_for(self._identity.hash, name)
        if self._storage.get_server(hash_hex) is not None:
            raise NameInUseError(f"you already have a server named '{name}'")

        created_at = time.time()
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
        """The stored record for one server, or None."""
        return self._storage.get_server(server_hash_hex)

    def list_servers(self) -> list:
        """Servers the local identity is a member of."""
        return [row for row in self._storage.get_all_servers()
                if self._storage.is_member(row["hash"], self._identity.hash_hex)]

    def is_owner(self, server_hash_hex: str) -> bool:
        """Whether this identity created the server, by the stored record."""
        row = self._storage.get_server(server_hash_hex)
        return row is not None and row["creator_hash"] == self._identity.hash_hex
