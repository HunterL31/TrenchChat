"""
Send and receive channel messages over LXMF.

LXMF fields layout:
    0x01  channel_hash      bytes[16]   — which channel
    0x02  display_name      str         — sender display name
    0x03  timestamp         float       — sender wall-clock Unix epoch
    0x04  message_id        str         — hex SHA-256 of content+sender+timestamp
    0x05  reply_to          str|None    — hex message_id of the message being replied to
    0x06  last_seen_id      str|None    — hex message_id of the most recent msg sender had seen
    0x07  sync_window_start float       — unix timestamp: start of sync window (sync_request)
    0x08  sync_messages     bytes       — msgpack list[dict] of full message records (sync_response)
    0x09  missed_for        str         — identity hex of peer who missed a message (missed_delivery)
    0x0A  missed_msg_id     str         — message_id that was not delivered (missed_delivery)
"""

import hashlib
import time
import RNS
import LXMF

from trenchchat.core.identity import Identity
from trenchchat.core.permissions import SEND_MESSAGE, is_open_join, permissions_from_json
from trenchchat.core.protocol import (
    F_CHANNEL_HASH, F_DISPLAY_NAME, F_TIMESTAMP, F_MESSAGE_ID,
    F_REPLY_TO, F_LAST_SEEN_ID, F_SYNC_WINDOW_START, F_SYNC_MESSAGES,
    F_MISSED_FOR, F_MISSED_MSG_ID, F_MSG_TYPE,
)
from trenchchat.core.storage import Storage
from trenchchat.network.router import Router

# Re-export field constants so existing importers of messaging.py continue to work
__all__ = [
    "F_CHANNEL_HASH", "F_DISPLAY_NAME", "F_TIMESTAMP", "F_MESSAGE_ID",
    "F_REPLY_TO", "F_LAST_SEEN_ID", "F_SYNC_WINDOW_START", "F_SYNC_MESSAGES",
    "F_MISSED_FOR", "F_MISSED_MSG_ID", "F_MSG_TYPE",
    "CAUSAL_WINDOW_SECS", "Messaging",
]

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
        self._missed_delivery_callback = None

        # dest_hex → list of message param dicts queued for offline peers
        self._pending: dict[str, list[dict]] = {}
        # msg_id → msg_params, kept so failed deliveries can be re-queued
        self._params_by_id: dict[str, dict] = {}

        router.add_delivery_callback(self._on_lxmf_message)

    def set_missed_delivery_callback(self, callback):
        """
        callback(channel_hash_hex, missed_peer_hex, msg_id, all_subscriber_hashes)
        Called when delivery to a peer fails (path unknown or LXMF failure).
        SyncManager uses this to broadcast missed-delivery hints.
        """
        self._missed_delivery_callback = callback

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

        # Params stored for pending retry and failure callbacks.
        # subscriber_hashes is included so flush_pending can re-register the
        # failed callback and broadcast missed-delivery hints if the retry fails.
        msg_params = {
            "channel_hash_hex":  channel_hash_hex,
            "content":           content,
            "timestamp":         ts,
            "msg_id":            msg_id,
            "display_name":      self._identity.display_name,
            "reply_to":          reply_to,
            "last_seen_id":      last_seen,
            "subscriber_hashes": list(subscriber_hashes),
        }

        # Keep params so failed-delivery callbacks can re-queue the message.
        # Prune old entries to avoid unbounded growth (keep the 200 most recent).
        self._params_by_id[msg_id] = msg_params
        if len(self._params_by_id) > 200:
            oldest = next(iter(self._params_by_id))
            del self._params_by_id[oldest]

        for dest_hex in subscriber_hashes:
            if dest_hex == self._identity.hash_hex:
                continue
            try:
                identity_hash = bytes.fromhex(dest_hex)
                delivery_dest_hash = RNS.Destination.hash(identity_hash, "lxmf", "delivery")
                dest_identity = RNS.Identity.recall(delivery_dest_hash)
                if dest_identity is None:
                    RNS.Transport.request_path(delivery_dest_hash)
                    self._pending.setdefault(dest_hex, []).append(msg_params)
                    self._notify_missed(channel_hash_hex, dest_hex, msg_id, subscriber_hashes)
                    continue

                lxm = self._build_lxm(dest_identity, msg_params)
                lxm.register_failed_callback(
                    lambda m, d=dest_hex, c=channel_hash_hex, mi=msg_id, subs=subscriber_hashes:
                        self._on_delivery_failed(d, c, mi, subs)
                )
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

    def flush_pending(self, dest_hex: str):
        """Attempt to deliver all queued messages for a peer whose path is now known."""
        queued = self._pending.pop(dest_hex, [])
        if not queued:
            return
        try:
            identity_hash = bytes.fromhex(dest_hex)
            delivery_dest_hash = RNS.Destination.hash(identity_hash, "lxmf", "delivery")
            dest_identity = RNS.Identity.recall(delivery_dest_hash)
            if dest_identity is None:
                # Still unreachable — put back
                self._pending[dest_hex] = queued
                return
            for params in queued:
                try:
                    lxm = self._build_lxm(dest_identity, params)
                    subs = params.get("subscriber_hashes", [])
                    lxm.register_failed_callback(
                        lambda m, d=dest_hex,
                               c=params["channel_hash_hex"],
                               mi=params["msg_id"],
                               s=subs:
                            self._on_delivery_failed(d, c, mi, s)
                    )
                    self._router.send(lxm)
                except Exception as e:
                    RNS.log(f"TrenchChat: flush_pending send error to {dest_hex}: {e}",
                            RNS.LOG_WARNING)
        except Exception as e:
            RNS.log(f"TrenchChat: flush_pending error for {dest_hex}: {e}", RNS.LOG_WARNING)

    def _build_lxm(self, dest_identity: RNS.Identity,
                   params: dict) -> LXMF.LXMessage:
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
            params["content"],
            desired_method=LXMF.LXMessage.DIRECT,
        )
        lxm.fields = {
            F_CHANNEL_HASH: bytes.fromhex(params["channel_hash_hex"]),
            F_DISPLAY_NAME: params["display_name"],
            F_TIMESTAMP:    params["timestamp"],
            F_MESSAGE_ID:   params["msg_id"],
            F_REPLY_TO:     params["reply_to"],
            F_LAST_SEEN_ID: params["last_seen_id"],
        }
        return lxm

    def _on_delivery_failed(self, dest_hex: str, channel_hash_hex: str,
                             msg_id: str, subscriber_hashes: list[str]):
        """Re-queue the message for retry when the peer's path returns, and record a missed hint."""
        params = self._params_by_id.get(msg_id)
        if params:
            RNS.log(
                f"TrenchChat: delivery failed to {dest_hex[:12]}…, re-queuing {msg_id[:12]}…",
                RNS.LOG_DEBUG,
            )
            # Request the path so flush_pending fires when it resolves
            identity_hash = bytes.fromhex(dest_hex)
            delivery_dest_hash = RNS.Destination.hash(identity_hash, "lxmf", "delivery")
            RNS.Transport.request_path(delivery_dest_hash)
            # Only re-queue if not already pending (avoid duplicates)
            pending_ids = {p["msg_id"] for p in self._pending.get(dest_hex, [])}
            if msg_id not in pending_ids:
                self._pending.setdefault(dest_hex, []).append(params)
        self._notify_missed(channel_hash_hex, dest_hex, msg_id, subscriber_hashes)

    def _notify_missed(self, channel_hash_hex: str, missed_peer_hex: str,
                       msg_id: str, subscriber_hashes: list[str]):
        if self._missed_delivery_callback:
            try:
                self._missed_delivery_callback(
                    channel_hash_hex, missed_peer_hex, msg_id, subscriber_hashes
                )
            except Exception as e:
                RNS.log(f"TrenchChat: missed_delivery_callback error: {e}", RNS.LOG_WARNING)

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

        # Resolve the sender's identity hash from the LXMF delivery destination hash.
        # message.source_hash is the delivery dest hash, not the raw identity hash.
        sender_identity = RNS.Identity.recall(message.source_hash) \
            if message.source_hash else None
        sender_hex = sender_identity.hash.hex() \
            if sender_identity else (message.source_hash.hex() if message.source_hash else "")

        channel = self._storage.get_channel(channel_hash_hex)
        if channel:
            perms = permissions_from_json(channel["permissions"])
            if not is_open_join(perms):
                if not self._storage.is_member(channel_hash_hex, sender_hex):
                    return
                if not self._storage.has_permission(channel_hash_hex, sender_hex, SEND_MESSAGE):
                    RNS.log(
                        f"TrenchChat: dropping message from {sender_hex[:12]}… — "
                        f"no {SEND_MESSAGE} permission on channel {channel_hash_hex[:12]}…",
                        RNS.LOG_WARNING,
                    )
                    return
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
            self.notify_message_received(channel_hash_hex, msg_id)

    def notify_message_received(self, channel_hash_hex: str, message_id: str) -> None:
        """Fire all registered message callbacks for a newly received message."""
        for cb in self._message_callbacks:
            try:
                cb(channel_hash_hex, message_id)
            except Exception as e:
                RNS.log(f"TrenchChat: message callback error: {e}", RNS.LOG_ERROR)

    def add_message_callback(self, callback):
        """callback(channel_hash_hex: str, message_id: str)"""
        if callback not in self._message_callbacks:
            self._message_callbacks.append(callback)

    def remove_message_callback(self, callback):
        if callback in self._message_callbacks:
            self._message_callbacks.remove(callback)
