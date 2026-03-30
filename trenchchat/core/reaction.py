"""
Emoji reaction management for TrenchChat.

Reactions attach a custom emoji (identified by its SHA-256 hash) to a channel
message.  The protocol uses three control message types:

  MT_REACTION      -- broadcast: I added/removed emoji Y on message X
  MT_EMOJI_REQUEST -- unicast:   please send me the image for emoji hash H
  MT_EMOJI_RESPONSE -- unicast:  here is the image for emoji hash H

Custom emoji images are stored in the local ``custom_emojis`` table.  When a
peer reacts with an emoji whose hash is not in our local library, we
automatically fire an ``MT_EMOJI_REQUEST`` to that peer.  In-flight requests
are tracked to avoid duplicate requests for the same hash.

Emoji images are capped at MAX_EMOJI_BYTES to keep them mesh-friendly.
"""

import hashlib
import threading
import time

import RNS
import LXMF

from trenchchat.core.identity import Identity
from trenchchat.core.protocol import (
    F_MSG_TYPE, F_CHANNEL_HASH,
    F_EMOJI_HASH, F_EMOJI_DATA, F_EMOJI_NAME,
    F_REACTION_MSG_ID, F_REACTION_REMOVE,
    MT_REACTION, MT_EMOJI_REQUEST, MT_EMOJI_RESPONSE,
)
from trenchchat.core.storage import Storage
from trenchchat.network.router import Router

MAX_EMOJI_BYTES = 65536   # 64 KB hard cap per emoji image


def compute_emoji_hash(image_data: bytes) -> str:
    """Return the hex SHA-256 hash of raw emoji image bytes."""
    return hashlib.sha256(image_data).hexdigest()


class ReactionManager:
    """Send, receive, and store emoji reactions and custom emoji assets.

    Reacts are broadcast to all channel subscribers via MT_REACTION.  When a
    receiver does not have the emoji locally, it requests the image from the
    reactor via MT_EMOJI_REQUEST / MT_EMOJI_RESPONSE.
    """

    def __init__(self, identity: Identity, storage: Storage, router: Router):
        self._identity = identity
        self._storage = storage
        self._router = router

        self._reaction_callbacks: list = []
        self._emoji_callbacks: list = []
        self._lock = threading.Lock()

        # emoji_hash hex strings we have already sent a request for, to avoid
        # spamming the same peer for the same asset.
        self._pending_emoji_requests: set[str] = set()

        router.add_delivery_callback(self._on_lxmf_message)

    # ------------------------------------------------------------------
    # Public API: reactions
    # ------------------------------------------------------------------

    def add_reaction(self, channel_hash_hex: str, message_id: str,
                     emoji_hash: str, subscriber_hashes: list[str]) -> None:
        """Record and broadcast a new reaction.

        Stores the reaction locally first, then sends MT_REACTION to every
        subscriber in the channel (excluding ourselves).
        """
        self._storage.insert_reaction(
            message_id=message_id,
            emoji_hash=emoji_hash,
            reactor_hash=self._identity.hash_hex,
            channel_hash=channel_hash_hex,
            reacted_at=time.time(),
        )
        self._fire_reaction_callbacks(channel_hash_hex, message_id)
        self._broadcast_reaction(
            channel_hash_hex, message_id, emoji_hash,
            subscriber_hashes, remove=False,
        )

    def remove_reaction(self, channel_hash_hex: str, message_id: str,
                        emoji_hash: str, subscriber_hashes: list[str]) -> None:
        """Remove a reaction locally and broadcast the removal to the channel."""
        self._storage.remove_reaction(
            message_id=message_id,
            emoji_hash=emoji_hash,
            reactor_hash=self._identity.hash_hex,
        )
        self._fire_reaction_callbacks(channel_hash_hex, message_id)
        self._broadcast_reaction(
            channel_hash_hex, message_id, emoji_hash,
            subscriber_hashes, remove=True,
        )

    # ------------------------------------------------------------------
    # Public API: emoji library
    # ------------------------------------------------------------------

    def import_emoji(self, name: str, image_data: bytes) -> str:
        """Add a custom emoji to the local library.

        Returns the emoji_hash (hex SHA-256).  Raises ValueError if the image
        exceeds MAX_EMOJI_BYTES.  If the same hash already exists the name is
        not updated (idempotent import).
        """
        if len(image_data) > MAX_EMOJI_BYTES:
            raise ValueError(
                f"Emoji image is {len(image_data)} bytes, max is {MAX_EMOJI_BYTES}"
            )
        emoji_hash = compute_emoji_hash(image_data)
        self._storage.insert_emoji(emoji_hash, name, image_data, time.time())
        return emoji_hash

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def add_reaction_callback(self, cb) -> None:
        """Register cb(channel_hash_hex: str, message_id: str) for reaction changes."""
        self._reaction_callbacks.append(cb)

    def add_emoji_callback(self, cb) -> None:
        """Register cb(emoji_hash: str) fired when a new emoji image is received."""
        self._emoji_callbacks.append(cb)

    def request_emoji(self, peer_hex: str, emoji_hash: str,
                      name: str = "") -> None:
        """Request the emoji image for a specific hash from a peer.

        Used when a received message contains a :name@hash: token whose image
        is not yet stored locally.  *name* is passed along so the sender can
        include it in the response and the receiver stores the emoji under the
        correct human-readable name.
        """
        self._request_emoji(peer_hex, emoji_hash, name=name)

    # ------------------------------------------------------------------
    # LXMF inbound
    # ------------------------------------------------------------------

    def _on_lxmf_message(self, message: LXMF.LXMessage) -> None:
        """Delivery callback -- handle reaction-related control messages."""
        fields = message.fields or {}
        msg_type = fields.get(F_MSG_TYPE)
        if msg_type is None:
            return
        if isinstance(msg_type, bytes):
            msg_type = msg_type.decode(errors="replace")

        if msg_type == MT_REACTION:
            self._handle_reaction(message, fields)
        elif msg_type == MT_EMOJI_REQUEST:
            self._handle_emoji_request(message, fields)
        elif msg_type == MT_EMOJI_RESPONSE:
            self._handle_emoji_response(message, fields)

    def _handle_reaction(self, message: LXMF.LXMessage, fields: dict) -> None:
        """Process an incoming MT_REACTION from a peer."""
        sender_hex = self._resolve_sender_hex(message)
        if not sender_hex:
            RNS.log("TrenchChat [reaction]: MT_REACTION with unknown sender", RNS.LOG_WARNING)
            return

        channel_hash_bytes = fields.get(F_CHANNEL_HASH)
        if not channel_hash_bytes:
            return
        channel_hash_hex = (
            channel_hash_bytes.hex()
            if isinstance(channel_hash_bytes, bytes)
            else str(channel_hash_bytes)
        )

        if not self._storage.is_subscribed(channel_hash_hex):
            return

        msg_id = fields.get(F_REACTION_MSG_ID, "")
        if isinstance(msg_id, bytes):
            msg_id = msg_id.decode(errors="replace")
        if not msg_id:
            return

        emoji_hash = fields.get(F_EMOJI_HASH, b"")
        if isinstance(emoji_hash, bytes):
            emoji_hash = emoji_hash.hex()
        if not emoji_hash:
            return

        remove = bool(fields.get(F_REACTION_REMOVE, False))

        if remove:
            self._storage.remove_reaction(msg_id, emoji_hash, sender_hex)
        else:
            self._storage.insert_reaction(
                message_id=msg_id,
                emoji_hash=emoji_hash,
                reactor_hash=sender_hex,
                channel_hash=channel_hash_hex,
                reacted_at=time.time(),
            )
            if not self._storage.emoji_exists(emoji_hash):
                self._request_emoji(sender_hex, emoji_hash)

        self._fire_reaction_callbacks(channel_hash_hex, msg_id)

    def _handle_emoji_request(self, message: LXMF.LXMessage, fields: dict) -> None:
        """Respond to an MT_EMOJI_REQUEST by sending the emoji image if we have it.

        The name from the request is echoed back so the receiver can store the
        emoji under the correct human-readable name.
        """
        requester_hex = self._resolve_sender_hex(message)
        if not requester_hex:
            return

        emoji_hash_raw = fields.get(F_EMOJI_HASH, b"")
        if isinstance(emoji_hash_raw, bytes):
            emoji_hash = emoji_hash_raw.hex()
        else:
            emoji_hash = str(emoji_hash_raw)
        if not emoji_hash:
            return

        # Recover the name the requester sent so we can echo it in the response.
        name_raw = fields.get(F_EMOJI_NAME, "")
        if isinstance(name_raw, bytes):
            name_raw = name_raw.decode(errors="replace")
        requested_name = str(name_raw)

        row = self._storage.get_emoji(emoji_hash)
        if not row:
            RNS.log(
                f"TrenchChat [reaction]: emoji request for unknown hash {emoji_hash[:12]}…",
                RNS.LOG_DEBUG,
            )
            return

        # Prefer the sender's own stored name; fall back to what the requester asked for.
        name = row["name"] or requested_name
        self._send_emoji_response(requester_hex, emoji_hash, bytes(row["image_data"]), name)

    def _handle_emoji_response(self, message: LXMF.LXMessage, fields: dict) -> None:
        """Store a received emoji image in the local library."""
        emoji_hash_raw = fields.get(F_EMOJI_HASH, b"")
        if isinstance(emoji_hash_raw, bytes):
            emoji_hash = emoji_hash_raw.hex()
        else:
            emoji_hash = str(emoji_hash_raw)

        emoji_data = fields.get(F_EMOJI_DATA, b"")
        if isinstance(emoji_data, str):
            emoji_data = emoji_data.encode()
        if not emoji_data or not emoji_hash:
            return

        if len(emoji_data) > MAX_EMOJI_BYTES:
            RNS.log(
                f"TrenchChat [reaction]: rejected oversized emoji {emoji_hash[:12]}… "
                f"({len(emoji_data)} bytes)",
                RNS.LOG_WARNING,
            )
            return

        actual_hash = compute_emoji_hash(emoji_data)
        if actual_hash != emoji_hash:
            RNS.log(
                f"TrenchChat [reaction]: emoji hash mismatch for {emoji_hash[:12]}…, discarding",
                RNS.LOG_WARNING,
            )
            return

        with self._lock:
            self._pending_emoji_requests.discard(emoji_hash)

        name_raw = fields.get(F_EMOJI_NAME, "")
        if isinstance(name_raw, bytes):
            name_raw = name_raw.decode(errors="replace")
        name = str(name_raw) or emoji_hash[:8]

        if not self._storage.emoji_exists(emoji_hash):
            self._storage.insert_emoji(
                emoji_hash, name, emoji_data, time.time()
            )
            RNS.log(
                f"TrenchChat [reaction]: stored new emoji {emoji_hash[:12]}…",
                RNS.LOG_NOTICE,
            )
            for cb in self._emoji_callbacks:
                try:
                    cb(emoji_hash)
                except Exception as e:
                    RNS.log(f"TrenchChat [reaction]: emoji callback error: {e}", RNS.LOG_ERROR)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _broadcast_reaction(self, channel_hash_hex: str, message_id: str,
                            emoji_hash: str, subscriber_hashes: list[str],
                            remove: bool) -> None:
        """Send MT_REACTION to all reachable channel subscribers."""
        channel_hash_bytes = bytes.fromhex(channel_hash_hex)
        emoji_hash_bytes = bytes.fromhex(emoji_hash)
        own_hex = self._identity.hash_hex

        for peer_hex in subscriber_hashes:
            if peer_hex == own_hex:
                continue
            try:
                identity_hash = bytes.fromhex(peer_hex)
                delivery_dest_hash = RNS.Destination.hash(identity_hash, "lxmf", "delivery")
                dest_identity = RNS.Identity.recall(delivery_dest_hash)
                if dest_identity is None:
                    RNS.Transport.request_path(delivery_dest_hash)
                    continue

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
                lxm.fields = {
                    F_MSG_TYPE:          MT_REACTION,
                    F_CHANNEL_HASH:      channel_hash_bytes,
                    F_REACTION_MSG_ID:   message_id,
                    F_EMOJI_HASH:        emoji_hash_bytes,
                    F_REACTION_REMOVE:   remove,
                }
                self._router.send(lxm)
            except Exception as e:
                RNS.log(
                    f"TrenchChat [reaction]: send error to {peer_hex[:12]}…: {e}",
                    RNS.LOG_WARNING,
                )

    def _request_emoji(self, peer_hex: str, emoji_hash: str,
                       name: str = "") -> None:
        """Send MT_EMOJI_REQUEST to a peer to obtain the emoji image bytes.

        *name* is included so the sender echoes it back in the response, letting
        the receiver store the emoji under the correct human-readable name.
        """
        with self._lock:
            if emoji_hash in self._pending_emoji_requests:
                return
            self._pending_emoji_requests.add(emoji_hash)

        try:
            identity_hash = bytes.fromhex(peer_hex)
            delivery_dest_hash = RNS.Destination.hash(identity_hash, "lxmf", "delivery")
            dest_identity = RNS.Identity.recall(delivery_dest_hash)
            if dest_identity is None:
                with self._lock:
                    self._pending_emoji_requests.discard(emoji_hash)
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
            fields = {
                F_MSG_TYPE:   MT_EMOJI_REQUEST,
                F_EMOJI_HASH: bytes.fromhex(emoji_hash),
            }
            if name:
                fields[F_EMOJI_NAME] = name
            lxm.fields = fields
            self._router.send(lxm)
            RNS.log(
                f"TrenchChat [reaction]: requested emoji {emoji_hash[:12]}… from {peer_hex[:12]}…",
                RNS.LOG_DEBUG,
            )
        except Exception as e:
            with self._lock:
                self._pending_emoji_requests.discard(emoji_hash)
            RNS.log(
                f"TrenchChat [reaction]: emoji request error to {peer_hex[:12]}…: {e}",
                RNS.LOG_WARNING,
            )

    def _send_emoji_response(self, peer_hex: str, emoji_hash: str,
                             image_data: bytes, name: str = "") -> None:
        """Send MT_EMOJI_RESPONSE with the emoji image and name to a requesting peer."""
        try:
            identity_hash = bytes.fromhex(peer_hex)
            delivery_dest_hash = RNS.Destination.hash(identity_hash, "lxmf", "delivery")
            dest_identity = RNS.Identity.recall(delivery_dest_hash)
            if dest_identity is None:
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
            fields = {
                F_MSG_TYPE:   MT_EMOJI_RESPONSE,
                F_EMOJI_HASH: bytes.fromhex(emoji_hash),
                F_EMOJI_DATA: image_data,
            }
            if name:
                fields[F_EMOJI_NAME] = name
            lxm.fields = fields
            self._router.send(lxm)
            RNS.log(
                f"TrenchChat [reaction]: sent emoji {emoji_hash[:12]}… to {peer_hex[:12]}…",
                RNS.LOG_DEBUG,
            )
        except Exception as e:
            RNS.log(
                f"TrenchChat [reaction]: emoji response error to {peer_hex[:12]}…: {e}",
                RNS.LOG_WARNING,
            )

    def _resolve_sender_hex(self, message: LXMF.LXMessage) -> str:
        """Resolve the sender's identity hash hex from an inbound LXMF message."""
        sender_identity = (
            RNS.Identity.recall(message.source_hash)
            if message.source_hash else None
        )
        return (
            sender_identity.hash.hex() if sender_identity
            else (message.source_hash.hex() if message.source_hash else "")
        )

    def _fire_reaction_callbacks(self, channel_hash_hex: str, message_id: str) -> None:
        """Invoke all registered reaction callbacks."""
        for cb in self._reaction_callbacks:
            try:
                cb(channel_hash_hex, message_id)
            except Exception as e:
                RNS.log(f"TrenchChat [reaction]: callback error: {e}", RNS.LOG_ERROR)
