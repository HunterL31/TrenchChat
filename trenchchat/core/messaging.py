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
    0x0D  image_data        bytes|None  — JPEG image attachment payload (max 320 KB)
    0x15  invite_issued_ts  float       — when an invite token was issued, bound into
                                          its signature so a departure recorded after it
                                          invalidates it at every peer
    0x28  scope_kind        str         — "server" when a control message targets a
                                          server scope; absent means a single channel
    0x43  reaction_unicode  str         — reaction key for a standard unicode emoji;
                                          mutually exclusive with 0x0E emoji_hash,
                                          which only carries a custom emoji (reaction)
    0x50  sync_truncated    bool        — responder capped this batch and holds more
                                          history (sync_response)
    0x51  sync_scan_cursor  float       — furthest timestamp the responder's sweep
                                          reached, even if withheld outright; only set
                                          when truncated (sync_response)
    0x60  voice_state       str         — "joined" | "left" (voice signalling)
    0x61  voice_muted       bool        — sender's current mute state
    0x62  voice_joined_at   float       — when the sender joined the voice session
    0x63  voice_codec       str         — codec the sender transmits ("opus")
    0x70  author_sig        bytes[64]   — author's Ed25519 signature binding the
                                          message to its author (see authorship.py)
    0x71  author_keys       dict        — {author hex: public key} sent with a sync
                                          batch, so a relayed message stays checkable
                                          after its author leaves
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
    F_MISSED_FOR, F_MISSED_MSG_ID, F_MSG_TYPE, F_IMAGE_DATA,
    F_AUTHOR_SIG, wire_timestamp,
)
from trenchchat.core.authorship import sign_message, verify_message
from trenchchat.core.image import MAX_IMAGE_BYTES, inbound_image_is_sane
from trenchchat.core.storage import Storage
from trenchchat.network.router import Router

# Re-export field constants so existing importers of messaging.py continue to work
__all__ = [
    "F_CHANNEL_HASH", "F_DISPLAY_NAME", "F_TIMESTAMP", "F_MESSAGE_ID",
    "F_REPLY_TO", "F_LAST_SEEN_ID", "F_SYNC_WINDOW_START", "F_SYNC_MESSAGES",
    "F_MISSED_FOR", "F_MISSED_MSG_ID", "F_MSG_TYPE", "F_IMAGE_DATA",
    "CAUSAL_WINDOW_SECS", "Messaging", "cancel_pending_for_channel",
]

# Threshold in seconds within which last_seen_id causal ordering is applied
CAUSAL_WINDOW_SECS = 5.0


def _compute_message_id(content: str, sender_hex: str, timestamp: float) -> str:
    payload = f"{content}:{sender_hex}:{timestamp:.6f}".encode()
    return hashlib.sha256(payload).hexdigest()


# Messages held for one unreachable peer before the oldest is dropped, and
# peers tracked at once. _params_by_id is capped but _pending holds the same
# dicts -- including their image payloads -- and was bounded only by a
# successful flush, which a peer that never returns never produces.
MAX_PENDING_PER_PEER = 100
MAX_PENDING_PEERS = 256


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
                     subscriber_hashes: list[str] | None = None,
                     image_data: bytes | None = None):
        """
        Send a channel message to all known subscribers.

        subscriber_hashes: list of hex identity hashes to deliver to.
        If None, the caller is responsible for providing the list
        (retrieved from subscription.py).
        image_data: optional JPEG bytes to attach to the message.
        """
        if not subscriber_hashes:
            return

        ts = time.time()
        last_seen = self._storage.get_latest_message_id(channel_hash_hex)
        msg_id = _compute_message_id(content, self._identity.hash_hex, ts)
        author_sig = sign_message(
            self._identity.rns_identity, channel_hash_hex, msg_id, ts,
            content, reply_to, last_seen, image_data,
        )

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
            "image_data":        image_data,
            "author_sig":        author_sig,
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
                    self._queue_pending(dest_hex, msg_params)
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
            image_data=image_data,
            author_sig=author_sig,
        )

    def cancel_pending_for_channel(self, channel_hash_hex: str):
        """Discard all queued outbound messages for a specific channel.

        Called when the local identity is removed from a channel so messages
        composed during the gap period are not delivered if the peer later
        becomes reachable.
        """
        for dest_hex in list(self._pending.keys()):
            self._pending[dest_hex] = [
                p for p in self._pending[dest_hex]
                if p.get("channel_hash_hex") != channel_hash_hex
            ]
            if not self._pending[dest_hex]:
                del self._pending[dest_hex]

    def _may_receive(self, channel_hash_hex: str, dest_hex: str) -> bool:
        """Whether this peer is still entitled to receive the channel's messages.

        A queue survives a kick, so a message queued before one would otherwise
        be pushed at the peer the moment they reappear.
        """
        channel = self._storage.get_channel(channel_hash_hex)
        if channel is None:
            return False
        if is_open_join(permissions_from_json(channel["permissions"])):
            return True
        return self._storage.is_member(channel_hash_hex, dest_hex)

    def _queue_pending(self, dest_hex: str, msg_params: dict) -> None:
        """Hold a message for a peer we cannot address, within a fixed bound."""
        queue = self._pending.setdefault(dest_hex, [])
        if len(queue) >= MAX_PENDING_PER_PEER:
            queue.pop(0)
        queue.append(msg_params)
        if len(self._pending) > MAX_PENDING_PEERS:
            for stale in [p for p, q in self._pending.items() if not q]:
                del self._pending[stale]
            while len(self._pending) > MAX_PENDING_PEERS:
                del self._pending[next(iter(self._pending))]

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
                    if not self._may_receive(params["channel_hash_hex"], dest_hex):
                        RNS.log(
                            f"TrenchChat: dropping queued message for "
                            f"{dest_hex[:12]}… — no longer a member of "
                            f"{params['channel_hash_hex'][:12]}…",
                            RNS.LOG_WARNING,
                        )
                        continue
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
        fields = {
            F_CHANNEL_HASH: bytes.fromhex(params["channel_hash_hex"]),
            F_DISPLAY_NAME: params["display_name"],
            F_TIMESTAMP:    params["timestamp"],
            F_MESSAGE_ID:   params["msg_id"],
            F_REPLY_TO:     params["reply_to"],
            F_LAST_SEEN_ID: params["last_seen_id"],
        }
        if params.get("image_data"):
            fields[F_IMAGE_DATA] = params["image_data"]
        if params.get("author_sig"):
            fields[F_AUTHOR_SIG] = params["author_sig"]
        lxm.fields = fields
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
                self._queue_pending(dest_hex, params)
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
        if channel is None:
            # Subscribed with no channel row: the permission model has nothing
            # to check against, so there is no way to authorise this. Fail
            # closed, matching reaction.py's _may_react.
            RNS.log(
                f"TrenchChat: dropping message for unknown channel "
                f"{channel_hash_hex[:12]}… from {sender_hex[:12]}…",
                RNS.LOG_WARNING,
            )
            return

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

        # The signature covers the timestamp, so a signed message cannot have
        # its clock quietly corrected -- the author asserted that value and
        # signed it. An implausible one is rejected outright.
        timestamp = wire_timestamp(fields.get(F_TIMESTAMP))
        if timestamp is None:
            RNS.log(
                f"TrenchChat: dropping message from {sender_hex[:12]}… — "
                f"implausible timestamp {fields.get(F_TIMESTAMP)!r}",
                RNS.LOG_WARNING,
            )
            return
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

        image_data = fields.get(F_IMAGE_DATA)
        if isinstance(image_data, str):
            image_data = image_data.encode()
        if not image_data:
            image_data = None

        # The id *is* this hash, and a globally-UNIQUE column means the first
        # writer of one keeps it: a member who signs their own message under
        # an id they saw elsewhere makes the genuine copy a silent duplicate
        # forever. Recomputing makes squatting a preimage problem.
        expected_id = _compute_message_id(content, sender_hex, timestamp)
        if not msg_id:
            msg_id = expected_id
        elif msg_id != expected_id:
            RNS.log(
                f"TrenchChat: dropping message from {sender_hex[:12]}… — "
                f"message_id {msg_id[:12]}… is not the hash of its content",
                RNS.LOG_WARNING,
            )
            return

        # Checked against the payload exactly as it arrived, before any of it
        # is stripped below -- the signature covers the image, so re-checking
        # after would never match.
        image_stripped = False
        author_sig = fields.get(F_AUTHOR_SIG)
        if not verify_message(self._storage, sender_hex, author_sig,
                              channel_hash_hex, msg_id, timestamp, content,
                              reply_to, last_seen_id, image_data):
            RNS.log(
                f"TrenchChat: dropping message {msg_id[:12]}… from "
                f"{sender_hex[:12]}… — author signature missing or invalid",
                RNS.LOG_WARNING,
            )
            return

        if image_data is not None and (
                len(image_data) > MAX_IMAGE_BYTES
                or not inbound_image_is_sane(image_data)):
            RNS.log(
                f"TrenchChat: stripping image from {msg_id[:12]}… — oversized "
                f"or an implausible decode",
                RNS.LOG_WARNING,
            )
            # The signature covers the image we are refusing, so it no longer
            # describes what we store. Clear it rather than keep one that
            # cannot verify: the row stays readable and simply never relays.
            image_data = None
            author_sig = None
            image_stripped = True

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
            image_data=image_data,
            author_sig=author_sig,
            image_stripped=image_stripped,
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
