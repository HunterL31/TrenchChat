"""
One-to-one conversations between two identities that hold each other as friends.

A conversation has no owner, no members table, no permissions document and no
announce. Its address is naming.dm_hash_for(a, b): derived from the two
identity hashes, order-independent, and the same width as a channel hash, so a
conversation's messages ride the ordinary message store and the ordinary chat
message format with nothing added to the wire.

That derivation is also the authorisation for the address itself. A receiver
recomputes it from the sender it has just authenticated; an inbound message
carrying any other conversation hash is not addressed to a conversation this
node is part of, and is dropped. Nothing has to be trusted for that check --
the address is the proof.

Who may talk is a separate question, and the answer is FriendsManager: only an
accepted friend. Both peers enforce it independently, so a message flows only
where both sides have agreed. There is no channel-wide sync behind a
conversation -- no third party holds its history -- so an undelivered direct
message is handled by the propagation node instead (see messaging.send_direct).

The other end does not have to be TrenchChat. A conversation is carried as a
plain LXMF message (see protocol.pack_dm_envelope), so Sideband, NomadNet or
anything else speaking LXMF can hold up its half, and the friendship gate is
unchanged for them: adding a contact is a local decision, and only an accepted
friend gets through. What such a peer cannot do is the TrenchChat-only extras,
so those are not sent to one -- see peer_is_trenchchat.
"""

import RNS

from trenchchat.core.naming import dm_hash_for
from trenchchat.core.presence import resolve_display_name

IDENTITY_HASH_HEX_LEN = 32


class DirectMessageManager:
    """Conversation bookkeeping and the mutual-friendship gate."""

    def __init__(self, identity, storage, friends_mgr, presence_mgr=None) -> None:
        self._identity = identity
        self._storage = storage
        self._friends = friends_mgr
        self._presence_mgr = presence_mgr

    # --- addressing ---

    def conversation_hash(self, peer_hash_hex: str) -> str | None:
        """The address we share with a peer, or None for a malformed hash."""
        if not self._is_valid_hash(peer_hash_hex):
            return None
        if peer_hash_hex == self._identity.hash_hex:
            return None
        return dm_hash_for(self._identity.hash_hex, peer_hash_hex)

    def is_conversation(self, channel_hash_hex: str) -> bool:
        return self._storage.is_dm(channel_hash_hex)

    def peer_for(self, channel_hash_hex: str) -> str | None:
        return self._storage.get_dm_peer(channel_hash_hex)

    def note_trenchchat_peer(self, peer_hash_hex: str) -> None:
        """Record that this peer runs TrenchChat, having heard it say so."""
        conversation = self.conversation_hash(peer_hash_hex)
        if conversation is not None:
            self._storage.set_dm_peer_is_trenchchat(conversation)

    def peer_is_trenchchat(self, channel_hash_hex: str) -> bool:
        """Whether the other end of a conversation understands TrenchChat's own
        messages. False for a plain LXMF client -- and for a TrenchChat peer we
        have not yet heard from, which costs only the extras until we do."""
        return self._storage.dm_peer_is_trenchchat(channel_hash_hex)

    # --- gate ---

    def may_dm(self, peer_hash_hex: str) -> bool:
        """Whether a direct message may pass between us and this peer.

        The same answer in both directions: an accepted friend, and nobody
        else. Applied before sending and again on everything received.
        """
        if not self._is_valid_hash(peer_hash_hex):
            return False
        if peer_hash_hex == self._identity.hash_hex:
            return False
        return self._friends.is_friend(peer_hash_hex)

    def hold_message_request(self, peer_hash_hex: str, body: str,
                             from_trenchchat: bool = False) -> bool:
        """Hold a message from a peer the gate just refused.

        The gate itself is unchanged -- this is what happens to what it turns
        away, so a sender that cannot ask any other way is not simply silenced.
        """
        return self._friends.hold_message_request(
            peer_hash_hex, body, from_trenchchat)

    # --- conversations ---

    def open_conversation(self, peer_hash_hex: str) -> str | None:
        """The conversation with a peer, created on first use.

        Returns None when the peer is not an accepted friend.
        """
        if not self.may_dm(peer_hash_hex):
            RNS.log(
                f"TrenchChat [dm]: refusing a conversation with "
                f"{peer_hash_hex[:12]}… — not an accepted friend",
                RNS.LOG_WARNING,
            )
            return None
        conversation_hash = self.conversation_hash(peer_hash_hex)
        if conversation_hash is None:
            return None
        self._storage.create_dm_conversation(conversation_hash, peer_hash_hex)
        return conversation_hash

    def conversations(self) -> list[dict]:
        """Every conversation, most recently active first."""
        results = []
        for row in self._storage.get_dm_conversations(self._identity.hash_hex):
            peer_hash = row["peer_hash"]
            results.append({
                "hash": row["conversation_hash"],
                "peer_hash": peer_hash,
                "display_name": resolve_display_name(
                    peer_hash, self._identity.hash_hex, self._storage
                ),
                "created_at": row["created_at"],
                "last_message_at": row["last_message_at"] or 0.0,
                "unread": row["unread"],
                "is_online": (self._presence_mgr.is_online(peer_hash)
                              if self._presence_mgr is not None else False),
                "is_friend": self._friends.is_friend(peer_hash),
                "peer_is_trenchchat": bool(row["peer_is_trenchchat"]),
            })
        return results

    def mark_read(self, channel_hash_hex: str) -> bool:
        """False if that address is not a conversation."""
        if not self._storage.is_dm(channel_hash_hex):
            return False
        self._storage.mark_dm_read(channel_hash_hex)
        return True

    def delete_conversation(self, channel_hash_hex: str) -> bool:
        """Forget a conversation and everything in it. False if unknown."""
        if not self._storage.is_dm(channel_hash_hex):
            return False
        self._storage.delete_dm_conversation(channel_hash_hex)
        return True

    @staticmethod
    def _is_valid_hash(value: str) -> bool:
        if not value or len(value) != IDENTITY_HASH_HEX_LEN:
            return False
        try:
            bytes.fromhex(value)
        except ValueError:
            return False
        return True
