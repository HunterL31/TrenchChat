"""
Send and receive channel messages over LXMF.

LXMF fields layout:
    0x01  channel_hash   bytes[16]  — which channel
    0x02  display_name   str        — sender display name
    0x03  timestamp      float      — sender wall-clock Unix epoch
    0x04  message_id     str        — hex SHA-256 of content+sender+timestamp
    0x05  reply_to       str|None   — hex message_id of the message being replied to
    0x06  last_seen_id   str|None   — hex message_id of the most recent msg sender had seen
"""

import hashlib
import time
import RNS
import LXMF

from trenchchat.core.identity import Identity
from trenchchat.core.storage import Storage
from trenchchat.network.router import Router

# LXMF field keys
F_CHANNEL_HASH = 0x01
F_DISPLAY_NAME = 0x02
F_TIMESTAMP    = 0x03
F_MESSAGE_ID   = 0x04
F_REPLY_TO     = 0x05
F_LAST_SEEN_ID = 0x06

# Control message type field (used by invite / member-list subsystem)
F_MSG_TYPE     = 0x10

# Threshold in seconds within which last_seen_id causal ordering is applied
CAUSAL_WINDOW_SECS = 5.0


def _compute_message_id(content: str, sender_hex: str, timestamp: float) -> str:
    payload = f"{content}:{sender_hex}:{timestamp:.6f}".encode()
    return hashlib.sha256(payload).hexdigest()


class Messaging:
    def __init__(self, identity: Identity, storage: Storage, router: Router):
        self._identity = identity
        self._storage = storage
        self._router = router
        self._message_callbacks: list = []

        router.add_delivery_callback(self._on_lxmf_message)

    # --- send ---

    def send_message(self, channel_hash_hex: str, content: str,
                     reply_to: str | None = None,
                     subscriber_hashes: list[str] | None = None):
        """
        Send a channel message to all known subscribers.

        subscriber_hashes: list of hex identity hashes to deliver to.
        If None, the caller is responsible for providing the list
        (retrieved from subscription.py).
        """
        if not subscriber_hashes:
            return

        ts = time.time()
        last_seen = self._storage.get_latest_message_id(channel_hash_hex)
        msg_id = _compute_message_id(content, self._identity.hash_hex, ts)

        for dest_hex in subscriber_hashes:
            if dest_hex == self._identity.hash_hex:
                continue
            try:
                dest_hash = bytes.fromhex(dest_hex)
                dest_identity = RNS.Identity.recall(dest_hash)
                if dest_identity is None:
                    RNS.Transport.request_path(dest_hash)
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
                    content,
                    desired_method=LXMF.LXMessage.DIRECT,
                )
                lxm.fields = {
                    F_CHANNEL_HASH: bytes.fromhex(channel_hash_hex),
                    F_DISPLAY_NAME: self._identity.display_name,
                    F_TIMESTAMP:    ts,
                    F_MESSAGE_ID:   msg_id,
                    F_REPLY_TO:     reply_to,
                    F_LAST_SEEN_ID: last_seen,
                }
                self._router.send(lxm)
            except Exception as e:
                RNS.log(f"TrenchChat: failed to send to {dest_hex}: {e}", RNS.LOG_WARNING)

        # Store our own message locally immediately.
        self._storage.insert_message(
            channel_hash=channel_hash_hex,
            sender_hash=self._identity.hash_hex,
            sender_name=self._identity.display_name,
            content=content,
            timestamp=ts,
            message_id=msg_id,
            reply_to=reply_to,
            last_seen_id=last_seen,
            received_at=ts,
        )

    # --- receive ---

    def _on_lxmf_message(self, message: LXMF.LXMessage):
        fields = message.fields or {}

        # Skip control messages (handled by invite.py)
        if F_MSG_TYPE in fields:
            return

        channel_hash_bytes = fields.get(F_CHANNEL_HASH)
        if not channel_hash_bytes:
            return

        channel_hash_hex = channel_hash_bytes.hex() \
            if isinstance(channel_hash_bytes, bytes) else str(channel_hash_bytes)

        if not self._storage.is_subscribed(channel_hash_hex):
            return

        channel = self._storage.get_channel(channel_hash_hex)
        if channel and channel["access_mode"] == "invite":
            sender_hex = message.source_hash.hex() \
                if message.source_hash else ""
            if not self._storage.is_member(channel_hash_hex, sender_hex):
                return

        sender_hex = message.source_hash.hex() if message.source_hash else ""
        sender_name = fields.get(F_DISPLAY_NAME, "")
        if isinstance(sender_name, bytes):
            sender_name = sender_name.decode(errors="replace")

        timestamp = fields.get(F_TIMESTAMP) or time.time()
        msg_id = fields.get(F_MESSAGE_ID, "")
        if isinstance(msg_id, bytes):
            msg_id = msg_id.decode(errors="replace")

        reply_to = fields.get(F_REPLY_TO)
        if isinstance(reply_to, bytes):
            reply_to = reply_to.decode(errors="replace")

        last_seen_id = fields.get(F_LAST_SEEN_ID)
        if isinstance(last_seen_id, bytes):
            last_seen_id = last_seen_id.decode(errors="replace")

        content = message.content or ""
        if isinstance(content, bytes):
            content = content.decode(errors="replace")

        if not msg_id:
            msg_id = _compute_message_id(content, sender_hex, timestamp)

        inserted = self._storage.insert_message(
            channel_hash=channel_hash_hex,
            sender_hash=sender_hex,
            sender_name=sender_name,
            content=content,
            timestamp=timestamp,
            message_id=msg_id,
            reply_to=reply_to,
            last_seen_id=last_seen_id,
            received_at=time.time(),
        )

        if inserted:
            self._storage.touch_channel(channel_hash_hex)
            for cb in self._message_callbacks:
                try:
                    cb(channel_hash_hex, msg_id)
                except Exception as e:
                    RNS.log(f"TrenchChat: message callback error: {e}", RNS.LOG_ERROR)

    def add_message_callback(self, callback):
        """callback(channel_hash_hex: str, message_id: str)"""
        if callback not in self._message_callbacks:
            self._message_callbacks.append(callback)

    def remove_message_callback(self, callback):
        if callback in self._message_callbacks:
            self._message_callbacks.remove(callback)
