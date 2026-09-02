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
    0x80  friend_note       str         — optional intro line on a friend request
                                          (friends.py); never on a chat message

A direct message uses none of the fields above. It is a plain LXMF message --
its text in the ordinary content, its attachment in LXMF's own FIELD_IMAGE --
carrying TrenchChat's additions inside LXMF's custom-payload fields, where a
client that is not TrenchChat knows to ignore them. That is what lets a
conversation work with Sideband, NomadNet or anything else speaking LXMF.

It carries no conversation address at all. The receiver derives that from the
sender it has just authenticated, so a message that arrives with no fields
whatsoever still lands in the right conversation, and no peer can name one it
is not half of. The absence of a channel hash is what marks a message as
direct; see protocol.pack_dm_envelope.
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
    F_AUTHOR_SIG, DM_ENVELOPE_TYPE, DM_IMAGE_EXTENSION,
    LXMF_FIELD_CUSTOM_DATA, LXMF_FIELD_CUSTOM_TYPE, LXMF_FIELD_IMAGE,
    inbound_image, pack_dm_envelope, pack_fields, unpack_dm_envelope,
    wire_timestamp,
)
from trenchchat.core.authorship import resolve_author, sign_message, verify_message
from trenchchat.core.image import MAX_IMAGE_BYTES, inbound_image_is_sane
from trenchchat.core.naming import dm_hash_for
from trenchchat.core.storage import Storage
from trenchchat.network.router import Router

# Re-export field constants so existing importers of messaging.py continue to work
__all__ = [
    "F_CHANNEL_HASH", "F_DISPLAY_NAME", "F_TIMESTAMP", "F_MESSAGE_ID",
    "F_REPLY_TO", "F_LAST_SEEN_ID", "F_SYNC_WINDOW_START", "F_SYNC_MESSAGES",
    "F_MISSED_FOR", "F_MISSED_MSG_ID", "F_MSG_TYPE", "F_IMAGE_DATA",
    "CAUSAL_WINDOW_SECS", "Messaging", "cancel_pending_for_channel",
    "DELIVERY_PENDING", "DELIVERY_DELIVERED", "DELIVERY_FAILED",
    "DELIVERY_PROPAGATED",
]

# Threshold in seconds within which last_seen_id causal ordering is applied
CAUSAL_WINDOW_SECS = 5.0

# Delivery state of one of the local user's own outbound messages, aggregated
# across a group fan-out (see Messaging.get_delivery_state). Not a wire value:
# these are UI hints, never sent to a peer.
DELIVERY_PENDING = "pending"      # a recipient is queued: path unknown, or awaiting retry
DELIVERY_DELIVERED = "delivered"  # handed to the transport for every recipient, no failures
DELIVERY_FAILED = "failed"        # a recipient's delivery failed and cannot be retried
DELIVERY_PROPAGATED = "propagated"  # handed to a propagation node to hold until the
#                                     recipient collects it (direct messages only)

# Outbound messages whose delivery state is tracked before the oldest is dropped.
MAX_TRACKED_DELIVERIES = 200


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
        self._delivery_status_callbacks: list = []
        self._missed_delivery_callback = None
        # Set by the frontend wiring once both exist; direct messaging is
        # inert without them, which is what a channels-only build gets.
        self._direct_mgr = None
        self._presence_mgr = None

        # dest_hex → list of message param dicts queued for offline peers
        self._pending: dict[str, list[dict]] = {}
        # msg_id → msg_params, kept so failed deliveries can be re-queued
        self._params_by_id: dict[str, dict] = {}
        # msg_id → {"channel": str, "recipients": {dest_hex: state}, "aggregate": str|None}
        # for our own outbound messages, so the UI can show a per-message
        # delivered/pending/failed indicator. In-memory only, like _pending:
        # it tracks live sends, not history, and is meaningless after a restart.
        self._delivery: dict[str, dict] = {}

        router.add_delivery_callback(self._on_lxmf_message)

    def set_direct_manager(self, direct_mgr) -> None:
        """Attach the DirectMessageManager that owns conversations and the gate.

        Set after construction because the two managers need each other: this
        one sends and stores, that one decides whether it may.
        """
        self._direct_mgr = direct_mgr

    def set_presence_manager(self, presence_mgr) -> None:
        """Attach presence, used to choose direct delivery over propagation."""
        self._presence_mgr = presence_mgr

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
                    self._track_delivery(msg_id, channel_hash_hex, dest_hex, DELIVERY_PENDING)
                    self._notify_missed(channel_hash_hex, dest_hex, msg_id, subscriber_hashes)
                    continue

                lxm = self._build_lxm(dest_identity, msg_params)
                lxm.register_delivery_callback(
                    lambda m, d=dest_hex, c=channel_hash_hex, mi=msg_id:
                        self._track_delivery(mi, c, d, DELIVERY_DELIVERED)
                )
                lxm.register_failed_callback(
                    lambda m, d=dest_hex, c=channel_hash_hex, mi=msg_id, subs=subscriber_hashes:
                        self._on_delivery_failed(d, c, mi, subs)
                )
                self._router.send(lxm)
                self._track_delivery(msg_id, channel_hash_hex, dest_hex, DELIVERY_DELIVERED)
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

    # --- direct messages ---

    def send_direct(self, peer_hex: str, content: str,
                    reply_to: str | None = None,
                    image_data: bytes | None = None) -> str | None:
        """Send a direct message to an accepted friend. Returns its message id.

        Returns None when direct messaging is not wired up, or the peer is not
        an accepted friend -- a silent no-op, matching the channel send path.
        """
        if self._direct_mgr is None:
            return None
        conversation = self._direct_mgr.open_conversation(peer_hex)
        if conversation is None:
            return None

        ts = time.time()
        last_seen = self._storage.get_latest_message_id(conversation)
        msg_id = _compute_message_id(content, self._identity.hash_hex, ts)
        author_sig = sign_message(
            self._identity.rns_identity, conversation, msg_id, ts,
            content, reply_to, last_seen, image_data,
        )
        msg_params = {
            "channel_hash_hex":  conversation,
            "content":           content,
            "timestamp":         ts,
            "msg_id":            msg_id,
            "display_name":      self._identity.display_name,
            "reply_to":          reply_to,
            "last_seen_id":      last_seen,
            "subscriber_hashes": [peer_hex],
            "image_data":        image_data,
            "author_sig":        author_sig,
            # Marks this as a conversation rather than a channel, so it is
            # built in the interoperable form below.
            "dm_peer_hex":       peer_hex,
        }
        self._params_by_id[msg_id] = msg_params
        if len(self._params_by_id) > MAX_TRACKED_DELIVERIES:
            del self._params_by_id[next(iter(self._params_by_id))]

        self._deliver_direct(peer_hex, msg_params)

        self._storage.insert_message(
            channel_hash=conversation,
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
        return msg_id

    def _deliver_direct(self, peer_hex: str, params: dict,
                        propagate_only: bool = False) -> None:
        """Hand one direct message to the transport.

        Direct while the peer is there to receive it; through a propagation
        node when they are not, because a conversation has no other member who
        could serve it later the way a channel does. Falls back to the ordinary
        pending queue when neither is possible.
        """
        conversation = params["channel_hash_hex"]
        msg_id = params["msg_id"]

        dest_identity = resolve_author(self._storage, peer_hex)
        if dest_identity is None:
            delivery_dest_hash = RNS.Destination.hash(
                bytes.fromhex(peer_hex), "lxmf", "delivery")
            RNS.Transport.request_path(delivery_dest_hash)
            self._queue_pending(peer_hex, params)
            self._track_delivery(msg_id, conversation, peer_hex, DELIVERY_PENDING)
            return

        if not propagate_only and self._peer_is_reachable(peer_hex):
            if self._send_direct_lxm(dest_identity, peer_hex, params):
                return

        if self._send_propagated_lxm(dest_identity, peer_hex, params):
            return

        self._queue_pending(peer_hex, params)
        self._track_delivery(msg_id, conversation, peer_hex, DELIVERY_PENDING)

    def _peer_is_reachable(self, peer_hex: str) -> bool:
        """Whether a direct attempt is worth making right now.

        Presence answers for a peer that sends beacons. It cannot answer for
        one that does not -- a bot, or anyone on another LXMF client -- and
        for them "not online" only ever meant "never heard of", which sent
        every first message to a propagation node to be pulled by a peer that
        may never pull. So presence decides for a peer it knows to be
        TrenchChat, and a resolved path decides for everyone else.

        Guessing wrong this way is cheap: a failed direct attempt falls back
        to propagation on its own (_on_direct_failed). Guessing wrong the
        other way is a message nobody collects.
        """
        if self._presence_mgr is not None:
            if self._presence_mgr.is_online(peer_hex):
                return True
            if self._peer_speaks_trenchchat(peer_hex):
                return False
        delivery_dest_hash = RNS.Destination.hash(
            bytes.fromhex(peer_hex), "lxmf", "delivery")
        return RNS.Identity.recall(delivery_dest_hash) is not None

    def _peer_speaks_trenchchat(self, peer_hex: str) -> bool:
        """Whether this peer has ever identified itself as TrenchChat, and so
        is one presence can speak for."""
        if self._direct_mgr is None:
            return False
        conversation = self._direct_mgr.conversation_hash(peer_hex)
        if conversation is None:
            return False
        return self._direct_mgr.peer_is_trenchchat(conversation)

    def _send_direct_lxm(self, dest_identity: RNS.Identity, peer_hex: str,
                         params: dict) -> bool:
        conversation = params["channel_hash_hex"]
        msg_id = params["msg_id"]
        try:
            lxm = self._build_lxm(dest_identity, params, LXMF.LXMessage.DIRECT)
            lxm.register_delivery_callback(
                lambda m, d=peer_hex, c=conversation, mi=msg_id:
                    self._track_delivery(mi, c, d, DELIVERY_DELIVERED)
            )
            lxm.register_failed_callback(
                lambda m, d=peer_hex, c=conversation, mi=msg_id:
                    self._on_direct_failed(d, mi)
            )
            self._router.send(lxm)
            self._track_delivery(msg_id, conversation, peer_hex, DELIVERY_DELIVERED)
            return True
        except Exception as e:
            RNS.log(f"TrenchChat [dm]: direct send to {peer_hex[:12]}… failed: {e}",
                    RNS.LOG_WARNING)
            return False

    def _send_propagated_lxm(self, dest_identity: RNS.Identity, peer_hex: str,
                             params: dict) -> bool:
        """Hand the message to a propagation node. False if there is no node.

        The node is checked first rather than caught afterwards: LXMF raises
        from handle_outbound when none is configured, and fails the message on
        the way out.
        """
        if getattr(self._router, "outbound_propagation_node", None) is None:
            return False
        conversation = params["channel_hash_hex"]
        msg_id = params["msg_id"]
        try:
            lxm = self._build_lxm(dest_identity, params, LXMF.LXMessage.PROPAGATED)
            lxm.register_delivery_callback(
                lambda m, d=peer_hex, c=conversation, mi=msg_id:
                    self._track_delivery(mi, c, d, DELIVERY_PROPAGATED)
            )
            lxm.register_failed_callback(
                lambda m, d=peer_hex, c=conversation, mi=msg_id:
                    self._track_delivery(mi, c, d, DELIVERY_FAILED)
            )
            self._router.send(lxm)
            self._track_delivery(msg_id, conversation, peer_hex, DELIVERY_PROPAGATED)
            RNS.log(
                f"TrenchChat [dm]: {msg_id[:12]}… handed to a propagation node "
                f"for {peer_hex[:12]}…",
                RNS.LOG_NOTICE,
            )
            return True
        except Exception as e:
            RNS.log(f"TrenchChat [dm]: propagated send to {peer_hex[:12]}… "
                    f"failed: {e}", RNS.LOG_WARNING)
            return False

    def _on_direct_failed(self, peer_hex: str, msg_id: str) -> None:
        """A direct attempt failed: try propagation, then the pending queue.

        No missed-delivery hint is ever broadcast for a conversation -- there
        is no third member who could serve it, and naming the pair to one would
        be the only thing such a hint achieved.
        """
        params = self._params_by_id.get(msg_id)
        if params is None:
            return
        self._deliver_direct(peer_hex, params, propagate_only=True)

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
        be pushed at the peer the moment they reappear. The same applies to a
        conversation the user has since ended by removing the friend.
        """
        if self._direct_mgr is not None and self._direct_mgr.is_conversation(
                channel_hash_hex):
            return self._direct_mgr.may_dm(dest_hex)
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
                    channel = params["channel_hash_hex"]
                    mid = params["msg_id"]
                    lxm.register_delivery_callback(
                        lambda m, d=dest_hex, c=channel, mi=mid:
                            self._track_delivery(mi, c, d, DELIVERY_DELIVERED)
                    )
                    lxm.register_failed_callback(
                        lambda m, d=dest_hex, c=channel, mi=mid, s=subs:
                            self._on_delivery_failed(d, c, mi, s)
                    )
                    self._router.send(lxm)
                    self._track_delivery(mid, channel, dest_hex, DELIVERY_DELIVERED)
                except Exception as e:
                    RNS.log(f"TrenchChat: flush_pending send error to {dest_hex}: {e}",
                            RNS.LOG_WARNING)
        except Exception as e:
            RNS.log(f"TrenchChat: flush_pending error for {dest_hex}: {e}", RNS.LOG_WARNING)

    def _build_lxm(self, dest_identity: RNS.Identity, params: dict,
                   desired_method: int | None = None) -> LXMF.LXMessage:
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
            desired_method=desired_method or LXMF.LXMessage.DIRECT,
        )
        lxm.fields = (self._dm_fields(params) if params.get("dm_peer_hex")
                      else pack_fields(self._channel_fields(params)))
        return lxm

    @staticmethod
    def _channel_fields(params: dict) -> dict:
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
        return fields

    @staticmethod
    def _dm_fields(params: dict) -> dict:
        """A conversation's fields: standard where a standard exists.

        No channel hash -- its absence is what says "direct", and the address
        is the receiver's to derive. The text rides in the ordinary content, so
        any LXMF client displays it whether or not it understands the rest.
        """
        fields = {
            LXMF_FIELD_CUSTOM_TYPE: DM_ENVELOPE_TYPE,
            LXMF_FIELD_CUSTOM_DATA: pack_dm_envelope(
                message_id=params["msg_id"],
                timestamp=params["timestamp"],
                display_name=params["display_name"],
                reply_to=params["reply_to"],
                last_seen_id=params["last_seen_id"],
                author_sig=params.get("author_sig"),
            ),
        }
        if params.get("image_data"):
            fields[LXMF_FIELD_IMAGE] = [DM_IMAGE_EXTENSION, params["image_data"]]
        return fields

    def _on_delivery_failed(self, dest_hex: str, channel_hash_hex: str,
                             msg_id: str, subscriber_hashes: list[str]):
        """Re-queue the message for retry when the peer's path returns, and record a missed hint.

        A failure is retriable only while its params are still held; once they
        have aged out of _params_by_id there is nothing left to resend, so the
        recipient is marked failed rather than pending.
        """
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
            self._track_delivery(msg_id, channel_hash_hex, dest_hex, DELIVERY_PENDING)
        else:
            self._track_delivery(msg_id, channel_hash_hex, dest_hex, DELIVERY_FAILED)
        self._notify_missed(channel_hash_hex, dest_hex, msg_id, subscriber_hashes)

    # --- delivery state ---

    def get_delivery_state(self, msg_id: str) -> str | None:
        """Aggregate delivery state of one of our own outbound messages.

        DELIVERY_PENDING if any recipient is still queued, DELIVERY_FAILED if a
        recipient failed with nothing left to retry, DELIVERY_DELIVERED once
        every recipient has been handed to the transport (or confirmed), or None
        if this message is not tracked -- not sent by us this session, or it had
        no recipients other than ourselves.
        """
        entry = self._delivery.get(msg_id)
        return self._aggregate(entry) if entry is not None else None

    @staticmethod
    def _aggregate(entry: dict) -> str | None:
        states = entry["recipients"].values()
        if not states:
            return None
        if any(s == DELIVERY_PENDING for s in states):
            return DELIVERY_PENDING
        if any(s == DELIVERY_FAILED for s in states):
            return DELIVERY_FAILED
        # A propagated message has left us but nobody has read it yet, so it is
        # deliberately not reported as delivered.
        if any(s == DELIVERY_PROPAGATED for s in states):
            return DELIVERY_PROPAGATED
        return DELIVERY_DELIVERED

    def _track_delivery(self, msg_id: str, channel_hash_hex: str,
                        dest_hex: str, state: str) -> None:
        """Record one recipient's delivery state and fire the status callback if
        the message's aggregate state changed."""
        entry = self._delivery.get(msg_id)
        if entry is None:
            entry = {"channel": channel_hash_hex, "recipients": {}, "aggregate": None}
            self._delivery[msg_id] = entry
            while len(self._delivery) > MAX_TRACKED_DELIVERIES:
                del self._delivery[next(iter(self._delivery))]
        entry["recipients"][dest_hex] = state
        new_state = self._aggregate(entry)
        if new_state is not None and new_state != entry["aggregate"]:
            entry["aggregate"] = new_state
            self._fire_delivery_status(channel_hash_hex, msg_id, new_state)

    def add_delivery_status_callback(self, callback):
        """callback(channel_hash_hex: str, message_id: str, delivery_state: str)

        Fired when the aggregate delivery state of one of our own messages
        changes (pending → delivered, delivered → failed, and so on).
        """
        if callback not in self._delivery_status_callbacks:
            self._delivery_status_callbacks.append(callback)

    def remove_delivery_status_callback(self, callback):
        if callback in self._delivery_status_callbacks:
            self._delivery_status_callbacks.remove(callback)

    def _fire_delivery_status(self, channel_hash_hex: str, msg_id: str,
                             state: str) -> None:
        for cb in self._delivery_status_callbacks:
            try:
                cb(channel_hash_hex, msg_id, state)
            except Exception as e:
                RNS.log(f"TrenchChat: delivery status callback error: {e}", RNS.LOG_ERROR)

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

        # Resolve the sender's identity hash from the LXMF delivery destination hash.
        # message.source_hash is the delivery dest hash, not the raw identity hash.
        sender_identity = RNS.Identity.recall(message.source_hash) \
            if message.source_hash else None
        sender_hex = sender_identity.hash.hex() \
            if sender_identity else (message.source_hash.hex() if message.source_hash else "")

        # Only fields the Router unwrapped from our envelope can name a
        # channel; a foreign message's LXMF field keys (0x01 is embedded
        # messages there) must not be misread as one.
        channel_hash_bytes = fields.get(F_CHANNEL_HASH) \
            if getattr(message, "trenchchat_protocol", False) else None
        if not channel_hash_bytes:
            # No channel means a conversation -- including a plain message from
            # a client that is not TrenchChat and sent no fields at all.
            self._on_direct_message(message, fields, sender_hex)
            return

        channel_hash_hex = channel_hash_bytes.hex() \
            if isinstance(channel_hash_bytes, bytes) else str(channel_hash_bytes)

        if not self._storage.is_subscribed(channel_hash_hex):
            return

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

        self._store_chat_message(message, fields, channel_hash_hex, sender_hex)

    def _on_direct_message(self, message: LXMF.LXMessage, fields: dict,
                           sender_hex: str) -> None:
        """Store an inbound direct message, if we hold its sender as a friend.

        Works for a TrenchChat peer and for any other LXMF client alike: the
        difference is only how much of the message is described. A TrenchChat
        peer sends an envelope with the id, timestamp and author signature it
        computed; anything else sends words, and those are filled in here.

        From anyone else the message is held as a request rather than stored --
        see FriendsManager.hold_message_request. The gate is unchanged; what
        changed is that being refused is no longer the same as never happening.
        """
        if self._direct_mgr is None:
            return

        content = message.content or ""
        if isinstance(content, bytes):
            content = content.decode(errors="replace")

        envelope = unpack_dm_envelope(fields)

        if not self._direct_mgr.may_dm(sender_hex):
            # Held rather than dropped: a client that speaks only plain LXMF
            # cannot send a friend request, so silence here left it no way to
            # reach anyone who had not already added it. The attachment is
            # deliberately not carried over -- an unknown sender's binary
            # payload is the half worth refusing.
            self._direct_mgr.hold_message_request(
                sender_hex, content, from_trenchchat=envelope is not None)
            return

        conversation = self._direct_mgr.open_conversation(sender_hex)
        if conversation is None:
            return

        if envelope is not None:
            self._direct_mgr.note_trenchchat_peer(sender_hex)
            values = self._dm_values_from_envelope(envelope, content, sender_hex)
            if values is None:
                return
        else:
            values = self._dm_values_from_plain(message, content, sender_hex)

        image_data = inbound_image(fields)
        self._insert_chat_message(
            channel_hash_hex=conversation,
            sender_hex=sender_hex,
            image_data=image_data,
            # A directly delivered message is already signed by its sender at
            # the LXMF layer, which the router verified before anything here
            # ran. The author signature exists for messages that arrive by
            # relay -- sync, where the peer handing it over is not the author --
            # and a conversation is never relayed. So it is required from a
            # TrenchChat peer, who has one, and not from a client that does not
            # implement it; neither case is trusted any less than the other.
            require_author_signature=envelope is not None,
            **values,
        )

    def _dm_values_from_envelope(self, envelope: dict, content: str,
                                 sender_hex: str) -> dict | None:
        """The message as its TrenchChat sender described it, or None if not plausible."""
        timestamp = wire_timestamp(envelope.get("timestamp"))
        if timestamp is None:
            RNS.log(
                f"TrenchChat [dm]: dropping a message from {sender_hex[:12]}… — "
                f"implausible timestamp {envelope.get('timestamp')!r}",
                RNS.LOG_WARNING,
            )
            return None
        msg_id = self._text(envelope.get("message_id"))
        expected_id = _compute_message_id(content, sender_hex, timestamp)
        if not msg_id:
            msg_id = expected_id
        elif msg_id != expected_id:
            RNS.log(
                f"TrenchChat [dm]: dropping a message from {sender_hex[:12]}… — "
                f"message_id is not the hash of its content",
                RNS.LOG_WARNING,
            )
            return None
        author_sig = envelope.get("author_sig")
        return {
            "sender_name":  self._text(envelope.get("display_name")),
            "timestamp":    timestamp,
            "msg_id":       msg_id,
            "content":      content,
            "reply_to":     self._text(envelope.get("reply_to")) or None,
            "last_seen_id": self._text(envelope.get("last_seen_id")) or None,
            "author_sig":   author_sig if isinstance(author_sig, bytes) else None,
        }

    def store_held_message(self, conversation_hash_hex: str, sender_hex: str,
                           content: str, timestamp: float) -> None:
        """File a message that was held while its sender was not yet accepted.

        The words are already ours -- LXMF authenticated the sender before they
        were held -- so this is the plain-client path with the arrival time it
        was held at, and no author signature to require of a peer that may not
        implement one.
        """
        self._insert_chat_message(
            channel_hash_hex=conversation_hash_hex,
            sender_hex=sender_hex,
            sender_name="",
            timestamp=timestamp,
            msg_id=_compute_message_id(content, sender_hex, timestamp),
            content=content,
            reply_to=None,
            last_seen_id=None,
            image_data=None,
            author_sig=None,
            require_author_signature=False,
        )

    def _dm_values_from_plain(self, message: LXMF.LXMessage, content: str,
                              sender_hex: str) -> dict:
        """The message as any other LXMF client sent it.

        Everything TrenchChat adds is derived here rather than trusted: the
        timestamp is LXMF's own, and the id is computed the same way it would
        have been at the other end.
        """
        timestamp = wire_timestamp(getattr(message, "timestamp", None)) or time.time()
        return {
            "sender_name":  "",
            "timestamp":    timestamp,
            "msg_id":       _compute_message_id(content, sender_hex, timestamp),
            "content":      content,
            "reply_to":     None,
            "last_seen_id": None,
            "author_sig":   None,
        }

    @staticmethod
    def _text(value) -> str:
        if isinstance(value, bytes):
            return value.decode(errors="replace")
        return value if isinstance(value, str) else ""

    def _store_chat_message(self, message: LXMF.LXMessage, fields: dict,
                            channel_hash_hex: str, sender_hex: str) -> None:
        """Read a channel message's own fields, then validate and store it."""
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

        self._insert_chat_message(
            channel_hash_hex=channel_hash_hex,
            sender_hex=sender_hex,
            sender_name=sender_name,
            timestamp=timestamp,
            msg_id=msg_id,
            content=content,
            reply_to=reply_to,
            last_seen_id=last_seen_id,
            image_data=image_data,
            author_sig=fields.get(F_AUTHOR_SIG),
        )

    def _insert_chat_message(self, *, channel_hash_hex: str, sender_hex: str,
                             sender_name: str, timestamp: float, msg_id: str,
                             content: str, reply_to: str | None,
                             last_seen_id: str | None, image_data: bytes | None,
                             author_sig: bytes | None,
                             require_author_signature: bool = True) -> None:
        """Check what a message claims, then store it.

        Shared by channels and conversations: whoever is allowed to send is
        decided before this, and what is checked about what they sent is the
        same either way. The one exception is the author signature, which only
        means anything where a message can arrive by relay -- see the caller.
        """
        # Checked against the payload exactly as it arrived, before any of it
        # is stripped below -- the signature covers the image, so re-checking
        # after would never match.
        image_stripped = False
        if require_author_signature and not verify_message(
                self._storage, sender_hex, author_sig, channel_hash_hex, msg_id,
                timestamp, content, reply_to, last_seen_id, image_data):
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
