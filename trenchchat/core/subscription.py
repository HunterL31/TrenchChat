"""
Subscription management.

For public channels:
  - Subscribe by saving the channel hash locally and sending a
    subscribe notification to the channel owner so they add us to
    their subscriber list.
  - Unsubscribe by removing the local record and notifying the owner.

For invite-only channels:
  - Subscription is granted via the invite flow (invite.py).
  - This module handles the local record side only.

Subscriber list sync:
  - The channel owner maintains the authoritative subscriber list.
  - When a new subscriber joins, the owner sends them the current list.
  - The list is an LXMF message with fields[0x30] = "subscriber_list".
"""

import time
import RNS
import LXMF
import msgpack

from trenchchat.core.identity import Identity
from trenchchat.core.protocol import (
    F_CHANNEL_HASH, F_MSG_TYPE, F_SUBSCRIBER_LIST,
    MT_SUBSCRIBE, MT_UNSUBSCRIBE, MT_SUBSCRIBER_LIST,
)
from trenchchat.core.storage import Storage
from trenchchat.network.router import Router


class SubscriptionManager:
    def __init__(self, identity: Identity, storage: Storage, router: Router):
        self._identity = identity
        self._storage = storage
        self._router = router

        # In-memory subscriber lists: channel_hash_hex -> set of identity_hash_hex
        self._subscribers: dict[str, set[str]] = {}

        router.add_delivery_callback(self._on_lxmf_message)

    # --- subscribe / unsubscribe (local node) ---

    def subscribe(self, channel_hash_hex: str, owner_hash_hex: str | None = None):
        """Subscribe to a channel and notify the owner if known."""
        self._storage.subscribe(channel_hash_hex)
        if owner_hash_hex and owner_hash_hex != self._identity.hash_hex:
            self._send_control(owner_hash_hex, MT_SUBSCRIBE, channel_hash_hex)

    def unsubscribe(self, channel_hash_hex: str, owner_hash_hex: str | None = None):
        """Unsubscribe from a channel and notify the owner if known."""
        self._storage.unsubscribe(channel_hash_hex)
        if owner_hash_hex and owner_hash_hex != self._identity.hash_hex:
            self._send_control(owner_hash_hex, MT_UNSUBSCRIBE, channel_hash_hex)

    # --- subscriber list (owner side) ---

    def get_subscribers(self, channel_hash_hex: str) -> set[str]:
        return self._subscribers.get(channel_hash_hex, set())

    def _add_subscriber(self, channel_hash_hex: str, identity_hex: str):
        if channel_hash_hex not in self._subscribers:
            self._subscribers[channel_hash_hex] = set()
        self._subscribers[channel_hash_hex].add(identity_hex)
        self._broadcast_subscriber_list(channel_hash_hex)

    def _remove_subscriber(self, channel_hash_hex: str, identity_hex: str):
        if channel_hash_hex in self._subscribers:
            self._subscribers[channel_hash_hex].discard(identity_hex)

    def _broadcast_subscriber_list(self, channel_hash_hex: str):
        """Send the current subscriber list to all subscribers."""
        subs = self.get_subscribers(channel_hash_hex)
        packed = msgpack.packb(list(subs), use_bin_type=True)
        for dest_hex in subs:
            if dest_hex == self._identity.hash_hex:
                continue
            self._send_raw(dest_hex, {
                F_MSG_TYPE:        MT_SUBSCRIBER_LIST,
                F_CHANNEL_HASH:    bytes.fromhex(channel_hash_hex),
                F_SUBSCRIBER_LIST: packed,
            })

    # --- inbound handler ---

    def _on_lxmf_message(self, message: LXMF.LXMessage):
        fields = message.fields or {}
        msg_type = fields.get(F_MSG_TYPE)
        if msg_type is None:
            return
        if isinstance(msg_type, bytes):
            msg_type = msg_type.decode(errors="replace")

        channel_hash_bytes = fields.get(F_CHANNEL_HASH)
        if not channel_hash_bytes:
            return
        channel_hash_hex = channel_hash_bytes.hex() \
            if isinstance(channel_hash_bytes, bytes) else str(channel_hash_bytes)

        # message.source_hash is the LXMF delivery destination hash.
        # Resolve it back to the sender's identity hash for owner comparisons.
        sender_delivery_hex = message.source_hash.hex() if message.source_hash else ""
        sender_identity = RNS.Identity.recall(message.source_hash) if message.source_hash else None
        sender_hex = sender_identity.hash.hex() if sender_identity else sender_delivery_hex

        if msg_type == MT_SUBSCRIBE:
            channel = self._storage.get_channel(channel_hash_hex)
            if channel and channel["creator_hash"] == self._identity.hash_hex:
                self._add_subscriber(channel_hash_hex, sender_hex)

        elif msg_type == MT_UNSUBSCRIBE:
            channel = self._storage.get_channel(channel_hash_hex)
            if channel and channel["creator_hash"] == self._identity.hash_hex:
                self._remove_subscriber(channel_hash_hex, sender_hex)

        elif msg_type == MT_SUBSCRIBER_LIST:
            channel = self._storage.get_channel(channel_hash_hex)
            if not channel or channel["creator_hash"] != sender_hex:
                RNS.log(
                    f"TrenchChat: rejected subscriber_list for {channel_hash_hex} "
                    f"from non-owner {sender_hex}",
                    RNS.LOG_WARNING,
                )
                return
            packed = fields.get(F_SUBSCRIBER_LIST)
            if packed:
                try:
                    hashes = msgpack.unpackb(packed, raw=False)
                    if channel_hash_hex not in self._subscribers:
                        self._subscribers[channel_hash_hex] = set()
                    self._subscribers[channel_hash_hex] = set(hashes)
                except Exception as e:
                    RNS.log(f"TrenchChat: failed to parse subscriber list: {e}",
                            RNS.LOG_WARNING)

    # --- helpers ---

    def _send_control(self, dest_hex: str, msg_type: str, channel_hash_hex: str):
        self._send_raw(dest_hex, {
            F_MSG_TYPE:     msg_type,
            F_CHANNEL_HASH: bytes.fromhex(channel_hash_hex),
        })

    def _send_raw(self, dest_hex: str, fields: dict):
        try:
            identity_hash = bytes.fromhex(dest_hex)
            delivery_dest_hash = RNS.Destination.hash(identity_hash, "lxmf", "delivery")
            dest_identity = RNS.Identity.recall(delivery_dest_hash)
            if dest_identity is None:
                RNS.Transport.request_path(delivery_dest_hash)
                return
            dest = RNS.Destination(
                dest_identity,
                RNS.Destination.OUT,
                RNS.Destination.SINGLE,
                "lxmf",
                "delivery",
            )
            lxm = LXMF.LXMessage(
                dest,
                self._router.delivery_destination,
                "",
                desired_method=LXMF.LXMessage.DIRECT,
            )
            lxm.fields = fields
            self._router.send(lxm)
        except Exception as e:
            RNS.log(f"TrenchChat: subscription control send error: {e}", RNS.LOG_WARNING)
