"""
Gossip-based message gap sync.

Three mechanisms work together:
  1. Messaging.flush_pending()        — sender retries queued messages when peer reappears
  2. MT_MISSED_DELIVERY broadcast     — sender tells online peers which subscriber missed a message
  3. MT_SYNC_REQUEST / MT_SYNC_RESPONSE — reconnecting peer pulls missing messages from any peer

Flow when B reconnects:
  - PeerAnnounceHandler fires on_peer_appeared(B)
  - SyncManager calls messaging.flush_pending(B)      [Mechanism 1]
  - SyncManager sends MT_SYNC_REQUEST to B's channels [Mechanism 3 – B pulls from us]
  - B's own SyncManager sends MT_SYNC_REQUEST to us   [Mechanism 3 – B pulls from all peers]

Flow when A fails to deliver to B:
  - Messaging calls missed_delivery_callback(channel, B, msg_id, all_subs)
  - SyncManager sends MT_MISSED_DELIVERY to all online subscribers
  - Each peer stores the hint in missed_deliveries table

When B later sends MT_SYNC_REQUEST:
  - Peer checks missed_deliveries hints for B → sends exact missing messages
  - If no hints, falls back to timestamp sweep (get_messages_after)
"""

import time
import RNS
import LXMF
import msgpack

from trenchchat.core.identity import Identity
from trenchchat.core.messaging import Messaging
from trenchchat.core.permissions import is_open_join, permissions_from_json
from trenchchat.core.protocol import (
    F_CHANNEL_HASH, F_MSG_TYPE,
    F_SYNC_WINDOW_START, F_SYNC_MESSAGES,
    F_MISSED_FOR, F_MISSED_MSG_ID,
    MT_MISSED_DELIVERY, MT_SYNC_REQUEST, MT_SYNC_RESPONSE,
)
from trenchchat.core.storage import Storage
from trenchchat.network.router import Router

# Sync window: how far back to look for missing messages
SYNC_WINDOW_DAYS    = 7
SYNC_WINDOW_SECS    = SYNC_WINDOW_DAYS * 86400

# Maximum messages returned in a single sync response (LXMF size budget)
MAX_RESPONSE_MESSAGES = 50


class SyncManager:
    def __init__(self, identity: Identity, storage: Storage, router: Router,
                 messaging: Messaging, subscription_mgr, invite_mgr):
        self._identity = identity
        self._storage = storage
        self._router = router
        self._messaging = messaging
        self._subscription_mgr = subscription_mgr
        self._invite_mgr = invite_mgr

        messaging.set_missed_delivery_callback(self._on_missed_delivery_event)
        router.add_delivery_callback(self._on_lxmf_message)

        # Purge stale hints from previous sessions on startup
        self._storage.purge_old_missed_deliveries(time.time() - SYNC_WINDOW_SECS)

    # --- public API ---

    def request_sync_all(self):
        """
        On startup: send MT_SYNC_REQUEST for every subscribed channel to all
        known-online peers (those whose RNS path is already resolved).
        """
        for sub in self._storage.get_subscriptions():
            channel_hash_hex = sub["channel_hash"]
            since_ts = sub["last_sync_at"] or (time.time() - SYNC_WINDOW_SECS)
            peers = self._get_channel_peers(channel_hash_hex)
            for peer_hex in peers:
                self._send_sync_request(peer_hex, channel_hash_hex, since_ts)

    def on_peer_appeared(self, peer_hex: str):
        """
        Called by PeerAnnounceHandler when a peer broadcasts their delivery
        destination.  Flush any pending outbound messages for them, then ask
        them for anything we may have missed on shared channels.
        """
        if peer_hex == self._identity.hash_hex:
            return

        self._messaging.flush_pending(peer_hex)

        # Send sync requests for every channel we share with this peer
        for sub in self._storage.get_subscriptions():
            channel_hash_hex = sub["channel_hash"]
            if peer_hex not in self._get_channel_peers(channel_hash_hex):
                continue
            since_ts = sub["last_sync_at"] or (time.time() - SYNC_WINDOW_SECS)
            self._send_sync_request(peer_hex, channel_hash_hex, since_ts)

    # --- missed-delivery hint broadcast ---

    def _on_missed_delivery_event(self, channel_hash_hex: str, missed_peer_hex: str,
                                   msg_id: str, subscriber_hashes: list[str]):
        """
        Called by Messaging when delivery to missed_peer_hex failed.
        Broadcast a MT_MISSED_DELIVERY hint to all currently-reachable
        subscribers so they can serve the message when B reconnects.
        """
        for dest_hex in subscriber_hashes:
            if dest_hex in (self._identity.hash_hex, missed_peer_hex):
                continue
            self._send_raw(dest_hex, {
                F_MSG_TYPE:      MT_MISSED_DELIVERY,
                F_CHANNEL_HASH:  bytes.fromhex(channel_hash_hex),
                F_MISSED_FOR:    missed_peer_hex,
                F_MISSED_MSG_ID: msg_id,
            })

        # Record the hint locally too (we are also a potential responder)
        self._storage.record_missed_delivery(channel_hash_hex, missed_peer_hex, msg_id)

    # --- inbound message handler ---

    def _on_lxmf_message(self, message: LXMF.LXMessage):
        fields = message.fields or {}
        msg_type = fields.get(F_MSG_TYPE)
        if msg_type is None:
            return
        if isinstance(msg_type, bytes):
            msg_type = msg_type.decode(errors="replace")
        if msg_type not in (MT_MISSED_DELIVERY, MT_SYNC_REQUEST, MT_SYNC_RESPONSE):
            return

        channel_hash_bytes = fields.get(F_CHANNEL_HASH)
        if not channel_hash_bytes:
            return
        channel_hash_hex = (channel_hash_bytes.hex()
                            if isinstance(channel_hash_bytes, bytes)
                            else str(channel_hash_bytes))

        sender_identity = (RNS.Identity.recall(message.source_hash)
                           if message.source_hash else None)
        sender_hex = (sender_identity.hash.hex()
                      if sender_identity
                      else (message.source_hash.hex() if message.source_hash else ""))

        if msg_type == MT_MISSED_DELIVERY:
            self._handle_missed_delivery(fields, channel_hash_hex)
        elif msg_type == MT_SYNC_REQUEST:
            self._handle_sync_request(fields, channel_hash_hex, sender_hex)
        elif msg_type == MT_SYNC_RESPONSE:
            self._handle_sync_response(fields, channel_hash_hex)

    # --- handlers ---

    def _handle_missed_delivery(self, fields: dict, channel_hash_hex: str):
        missed_for = fields.get(F_MISSED_FOR, "")
        missed_msg_id = fields.get(F_MISSED_MSG_ID, "")
        if isinstance(missed_for, bytes):
            missed_for = missed_for.decode(errors="replace")
        if isinstance(missed_msg_id, bytes):
            missed_msg_id = missed_msg_id.decode(errors="replace")
        if missed_for and missed_msg_id:
            self._storage.record_missed_delivery(channel_hash_hex, missed_for, missed_msg_id)

    def _handle_sync_request(self, fields: dict, channel_hash_hex: str,
                              requester_hex: str):
        if not self._storage.is_subscribed(channel_hash_hex):
            return

        channel = self._storage.get_channel(channel_hash_hex)
        if channel and not is_open_join(permissions_from_json(channel["permissions"])):
            if not self._storage.is_member(channel_hash_hex, requester_hex):
                return

        window_start_raw = fields.get(F_SYNC_WINDOW_START, 0.0)
        try:
            window_start = float(window_start_raw)
        except (TypeError, ValueError):
            window_start = time.time() - SYNC_WINDOW_SECS
        # Never look back further than the configured sync window
        window_start = max(window_start, time.time() - SYNC_WINDOW_SECS)

        # Prefer hint-targeted lookup; fall back to timestamp sweep
        missed_ids = self._storage.get_missed_message_ids(channel_hash_hex, requester_hex)
        if missed_ids:
            rows = self._get_messages_by_ids(channel_hash_hex, missed_ids)
        else:
            rows = self._storage.get_messages_after(
                channel_hash_hex, window_start, MAX_RESPONSE_MESSAGES
            )

        if not rows:
            return

        packed = msgpack.packb(
            [self._row_to_dict(r) for r in rows],
            use_bin_type=True,
        )
        self._send_raw(requester_hex, {
            F_MSG_TYPE:      MT_SYNC_RESPONSE,
            F_CHANNEL_HASH:  bytes.fromhex(channel_hash_hex),
            F_SYNC_MESSAGES: packed,
        })

    def _handle_sync_response(self, fields: dict, channel_hash_hex: str):
        if not self._storage.is_subscribed(channel_hash_hex):
            return

        packed = fields.get(F_SYNC_MESSAGES)
        if not packed:
            return
        try:
            messages = msgpack.unpackb(packed, raw=False)
        except Exception as e:
            RNS.log(f"TrenchChat: sync_response unpack error: {e}", RNS.LOG_WARNING)
            return

        inserted_any = False
        for m in messages:
            try:
                inserted = self._storage.insert_message(
                    channel_hash=channel_hash_hex,
                    sender_hash=m.get("sender_hash", ""),
                    sender_name=m.get("sender_name", ""),
                    content=m.get("content", ""),
                    timestamp=float(m.get("timestamp", time.time())),
                    message_id=m.get("message_id", ""),
                    reply_to=m.get("reply_to"),
                    last_seen_id=m.get("last_seen_id"),
                    received_at=time.time(),
                )
                if inserted:
                    inserted_any = True
                    self._storage.touch_channel(channel_hash_hex)
                    self._messaging.notify_message_received(
                        channel_hash_hex, m.get("message_id", "")
                    )
            except Exception as e:
                RNS.log(f"TrenchChat: sync_response insert error: {e}", RNS.LOG_WARNING)

        if inserted_any:
            # Clear hints now that we have the messages
            self._storage.clear_missed_deliveries(channel_hash_hex, self._identity.hash_hex)
            self._storage.update_last_sync(channel_hash_hex)

    # --- helpers ---

    def _get_channel_peers(self, channel_hash_hex: str) -> set[str]:
        """Return identity hashes of all known peers on this channel (excl. self)."""
        peers: set[str] = set()

        # Public channels: subscribers tracked by SubscriptionManager
        subs = self._subscription_mgr.get_subscribers(channel_hash_hex)
        peers.update(subs)

        # Invite-only channels: from members table
        for row in self._storage.get_members(channel_hash_hex):
            peers.add(row["identity_hash"])

        # Channel owner (stored in channels table)
        channel = self._storage.get_channel(channel_hash_hex)
        if channel:
            peers.add(channel["creator_hash"])

        peers.discard(self._identity.hash_hex)
        return peers

    def _get_messages_by_ids(self, channel_hash_hex: str,
                              message_ids: list[str]) -> list:
        """Fetch message rows matching the given message_id list."""
        rows = self._storage.get_messages_after(
            channel_hash_hex,
            time.time() - SYNC_WINDOW_SECS,
            limit=len(message_ids) + MAX_RESPONSE_MESSAGES,
        )
        id_set = set(message_ids)
        return [r for r in rows if r["message_id"] in id_set][:MAX_RESPONSE_MESSAGES]

    @staticmethod
    def _row_to_dict(row) -> dict:
        return {
            "sender_hash":  row["sender_hash"],
            "sender_name":  row["sender_name"],
            "content":      row["content"],
            "timestamp":    row["timestamp"],
            "message_id":   row["message_id"],
            "reply_to":     row["reply_to"],
            "last_seen_id": row["last_seen_id"],
        }

    def _send_sync_request(self, dest_hex: str, channel_hash_hex: str, since_ts: float):
        self._send_raw(dest_hex, {
            F_MSG_TYPE:          MT_SYNC_REQUEST,
            F_CHANNEL_HASH:      bytes.fromhex(channel_hash_hex),
            F_SYNC_WINDOW_START: since_ts,
        })

    def _send_raw(self, dest_hex: str, fields: dict):
        try:
            identity_hash = bytes.fromhex(dest_hex)
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
            lxm.fields = fields
            self._router.send(lxm)
        except Exception as e:
            RNS.log(f"TrenchChat: sync send error to {dest_hex}: {e}", RNS.LOG_WARNING)
