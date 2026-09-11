"""
Who this node will hold a direct IP session with.

A direct session discloses this node's addresses to the peer on the other end,
so it is offered to someone an admin vetted and invited and to nobody else: a
peer is eligible when this node and that peer are both current members of one
invite-only channel, or of one server, by the stored members table. Servers
are always invite-only. An open-join channel never qualifies whatever its
subscriber list says, because anyone can join one, and an accepted friendship
does not qualify either in this first cut.

This is the core enforcement layer of that gate, called directly by
IPTransport the moment an inbound HELLO proves an identity and before any
frame of theirs is read. Phase 3 adds the outbound guard and the client gate
over it.
"""

from trenchchat.core.permissions import is_open_join, permissions_from_json
from trenchchat.core.storage import Storage


def is_eligible(storage: Storage, self_hex: str, peer_hex: str) -> bool:
    """Whether this node and a peer share an invite-only channel or a server."""
    if not self_hex or not peer_hex or self_hex == peer_hex:
        return False
    shared = storage.member_scopes(self_hex) & storage.member_scopes(peer_hex)
    for scope_hash in shared:
        if storage.get_server(scope_hash) is not None:
            return True
        channel = storage.get_channel(scope_hash)
        if channel is None:
            continue
        if not is_open_join(permissions_from_json(channel["permissions"])):
            return True
    return False
