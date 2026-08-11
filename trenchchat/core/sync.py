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

A request reaching further back than SYNC_WINDOW_SECS ("deep" backfill) is
still answered -- there's no hard wall -- but rate-limited per (channel,
peer): DEEP_SYNC_COOLDOWN_SECS between deep sweeps this responder will serve
a given peer, so a flood of requests can't repeatedly force a full
timestamp sweep. A request within the recent window is unaffected and
always answered immediately.
"""

import threading
import time
import RNS
import LXMF
import msgpack

from trenchchat.core.identity import Identity
from trenchchat.core.image import MAX_IMAGE_BYTES
from trenchchat.core.messaging import Messaging
from trenchchat.core.permissions import (
    FULL_SYNC, has_permission, is_open_join, permissions_from_json,
)
from trenchchat.core.protocol import (
    F_CHANNEL_HASH, F_MSG_TYPE,
    F_SYNC_WINDOW_START, F_SYNC_MESSAGES,
    F_MISSED_FOR, F_MISSED_MSG_ID,
    MT_MISSED_DELIVERY, MT_SYNC_REQUEST, MT_SYNC_RESPONSE,
    SYNC_WINDOW_SECS,
    unpack_wire,
)
from trenchchat.core.storage import Storage
from trenchchat.network.router import Router

# Maximum messages returned in a single sync response (LXMF size budget)
MAX_RESPONSE_MESSAGES = 50

# How long an issued sync request stays answerable.  A response arriving
# outside this window is treated as unsolicited.  Generous enough to cover a
# slow multi-hop mesh round trip without leaving the window open indefinitely.
SYNC_RESPONSE_WINDOW_SECS = 300

# How often a single peer may trigger a deep (pre-SYNC_WINDOW_SECS) backfill
# sweep on a given channel from this responder. A soft mitigation against a
# flood of costly full timestamp sweeps, not a hard security boundary --
# tenure and full_sync already gate what a peer is authorised to see; this
# only paces how fast they can pull it.
DEEP_SYNC_COOLDOWN_SECS = 60

# How long an idle (channel, peer) cooldown entry is kept before being
# pruned, so the cooldown map doesn't grow unbounded over a long session
# with many distinct peers.
DEEP_SYNC_COOLDOWN_PRUNE_SECS = 24 * 3600


class SyncManager:
    def __init__(self, identity: Identity, storage: Storage, router: Router,
                 messaging: Messaging, subscription_mgr, invite_mgr):
        self._identity = identity
        self._storage = storage
        self._router = router
        self._messaging = messaging
        self._subscription_mgr = subscription_mgr
        self._invite_mgr = invite_mgr

        # (channel_hash_hex, peer_hex) -> time the request was issued.  A sync
        # response is only applied if it answers one of these.
        self._pending_requests: dict[tuple[str, str], float] = {}
        self._pending_requests_lock = threading.Lock()

        # (channel_hash_hex, peer_hex) -> time we last served that peer a
        # deep backfill sweep for that channel.  In-memory only; a restart
        # resets it, which is acceptable for a soft rate limit.
        self._deep_sync_last_served: dict[tuple[str, str], float] = {}
        self._deep_sync_lock = threading.Lock()

        messaging.set_missed_delivery_callback(self._on_missed_delivery_event)
        router.add_delivery_callback(self._on_lxmf_message)
        invite_mgr.add_member_list_callback(self._on_member_list_updated)
        invite_mgr.add_channel_joined_callback(self._on_channel_joined)

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
            self._request_sync_for_channel(channel_hash_hex, sub["last_sync_at"])

    def _on_channel_joined(self, channel_hash_hex: str, channel_name: str):
        """
        Fired when we auto-join a channel via an accepted invite. Without
        this, a new member never sees any message sent before they joined:
        request_sync_all() only runs once, 3s after app startup, over
        whatever channels are already subscribed at that moment -- a
        channel joined later in the session is never covered by anything.

        A fresh join has no last_sync_at yet, so ask for everything (0.0) --
        the responder decides how far back it's actually willing to look;
        see _handle_sync_request's deep-sync cooldown.
        """
        self._request_sync_for_channel(channel_hash_hex, 0.0)

    def _request_sync_for_channel(self, channel_hash_hex: str, since_ts: float):
        peers = self._get_channel_peers(channel_hash_hex)
        for peer_hex in peers:
            self._send_sync_request(peer_hex, channel_hash_hex, since_ts)

    def _on_member_list_updated(self, channel_hash_hex: str):
        """Clear pending outbound messages for this channel if we were removed.

        When a member list update is accepted and the local identity is no
        longer in the roster, any queued messages for that channel cannot be
        delivered to legitimate members.  Discarding them prevents the kicked
        client from accumulating messages during the gap that could later be
        replayed after re-admission.
        """
        my_hex = self._identity.hash_hex
        if not self._storage.is_member(channel_hash_hex, my_hex):
            self._messaging.cancel_pending_for_channel(channel_hash_hex)
            RNS.log(
                f"TrenchChat [sync]: local identity removed from "
                f"{channel_hash_hex[:12]}… — pending outbound messages cleared",
                RNS.LOG_NOTICE,
            )

    def on_peer_appeared(self, peer_hex: str):
        """
        Called by PeerAnnounceHandler when a peer broadcasts their delivery
        destination.  Flush any pending outbound messages for them, then ask
        them for anything we may have missed on shared channels.
        """
        if peer_hex == self._identity.hash_hex:
            return

        # The announce carries this peer's identity, so anything of theirs we
        # quarantined can now be verified.
        self._router.release_quarantined(peer_hex)

        self._messaging.flush_pending(peer_hex)

        # Send sync requests for every channel we share with this peer
        for sub in self._storage.get_subscriptions():
            channel_hash_hex = sub["channel_hash"]
            if peer_hex not in self._get_channel_peers(channel_hash_hex):
                continue
            self._send_sync_request(peer_hex, channel_hash_hex, sub["last_sync_at"])

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
            self._handle_missed_delivery(fields, channel_hash_hex, sender_hex)
        elif msg_type == MT_SYNC_REQUEST:
            self._handle_sync_request(fields, channel_hash_hex, sender_hex)
        elif msg_type == MT_SYNC_RESPONSE:
            self._handle_sync_response(fields, channel_hash_hex, sender_hex)

    # --- handlers ---

    def _peer_may_participate(self, channel_hash_hex: str, peer_hex: str) -> bool:
        """Return True if peer_hex is entitled to take part in this channel's sync.

        An unknown channel is treated as closed, so a missing record can never
        widen access.
        """
        if not peer_hex:
            return False
        channel = self._storage.get_channel(channel_hash_hex)
        if channel is None:
            return False
        if is_open_join(permissions_from_json(channel["permissions"])):
            return True
        return self._storage.is_member(channel_hash_hex, peer_hex)

    def _handle_missed_delivery(self, fields: dict, channel_hash_hex: str,
                                sender_hex: str):
        if not self._peer_may_participate(channel_hash_hex, sender_hex):
            return
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

        # Fails closed on an unknown channel.
        if not self._peer_may_participate(channel_hash_hex, requester_hex):
            return
        channel = self._storage.get_channel(channel_hash_hex)

        window_start_raw = fields.get(F_SYNC_WINDOW_START, 0.0)
        try:
            window_start = float(window_start_raw)
        except (TypeError, ValueError):
            window_start = time.time() - SYNC_WINDOW_SECS
        window_start = max(window_start, 0.0)

        # Prefer hint-targeted lookup; fall back to timestamp sweep
        missed_ids = self._storage.get_missed_message_ids(channel_hash_hex, requester_hex)
        if missed_ids:
            rows = self._get_messages_by_ids(channel_hash_hex, missed_ids)
        else:
            # A request reaching further back than the recent window is a
            # "deep" backfill; rate-limited so a flood of requests can't
            # repeatedly force a full timestamp sweep. A recent request is
            # unaffected and always answered immediately.
            if window_start < time.time() - SYNC_WINDOW_SECS and not \
                    self._deep_sync_allowed(channel_hash_hex, requester_hex):
                RNS.log(
                    f"TrenchChat [sync]: deep sync request from "
                    f"{requester_hex[:12]}… for {channel_hash_hex[:12]}… "
                    f"throttled — cooldown active",
                    RNS.LOG_DEBUG,
                )
                return
            rows = self._storage.get_messages_after(
                channel_hash_hex, window_start, MAX_RESPONSE_MESSAGES
            )

        if not rows:
            return

        # Filter sync-response rows against tenure. Only applied when tenure
        # data exists for the channel (skips open-join channels and channels
        # bootstrapped before this feature). Two independent checks:
        #   - sender: the claimed author must actually have been a member at
        #     that timestamp, or the message could be a kicked member's
        #     replay or an outright forgery.
        #   - requester (unless they hold the full_sync permission): the peer
        #     asking for sync must themselves have been a member at that
        #     timestamp, or sync becomes a way to backfill history from
        #     before they ever joined. full_sync is a per-role permission
        #     (like send_message/invite/...), off by default -- an admin
        #     grants it to whichever role(s) should be able to backfill full
        #     history, e.g. admin but not member.
        has_tenure = self._storage.has_any_tenure(channel_hash_hex)
        if has_tenure:
            perms = permissions_from_json(channel["permissions"]) if channel else {}
            requester_role = self._storage.get_role(channel_hash_hex, requester_hex)
            full_sync = has_permission(perms, requester_role, FULL_SYNC)
            valid_rows = []
            for r in rows:
                if not self._storage.was_member_at(channel_hash_hex, r["sender_hash"],
                                                    r["timestamp"]):
                    RNS.log(
                        f"TrenchChat [sync]: omitting message {r['message_id'][:12]}… "
                        f"from sync response — sender {r['sender_hash'][:12]}… "
                        f"was not a member at ts={r['timestamp']:.0f}",
                        RNS.LOG_WARNING,
                    )
                    continue
                if not full_sync and not self._storage.was_member_at(
                    channel_hash_hex, requester_hex, r["timestamp"]
                ):
                    RNS.log(
                        f"TrenchChat [sync]: omitting message {r['message_id'][:12]}… "
                        f"from sync response — requester {requester_hex[:12]}… "
                        f"was not yet a member at ts={r['timestamp']:.0f}",
                        RNS.LOG_DEBUG,
                    )
                    continue
                valid_rows.append(r)
            rows = valid_rows

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

    def _handle_sync_response(self, fields: dict, channel_hash_hex: str,
                              responder_hex: str = ""):
        if not self._storage.is_subscribed(channel_hash_hex):
            return

        # The gate is that we asked this peer for this channel, not that they
        # are a member: by design any reachable peer may serve history and our
        # local roster need not list them.
        if not self._claim_pending_request(channel_hash_hex, responder_hex):
            RNS.log(
                f"TrenchChat [sync]: dropping unsolicited sync response for "
                f"{channel_hash_hex[:12]}… from {responder_hex[:12]}…",
                RNS.LOG_WARNING,
            )
            return

        packed = fields.get(F_SYNC_MESSAGES)
        if not packed:
            return
        try:
            messages = unpack_wire(packed)
        except Exception as e:
            RNS.log(f"TrenchChat: sync_response unpack error: {e}", RNS.LOG_WARNING)
            return

        has_tenure = self._storage.has_any_tenure(channel_hash_hex)
        my_hex = self._identity.hash_hex
        full_sync = False
        if has_tenure:
            channel = self._storage.get_channel(channel_hash_hex)
            if channel:
                perms = permissions_from_json(channel["permissions"])
                my_role = self._storage.get_role(channel_hash_hex, my_hex)
                full_sync = has_permission(perms, my_role, FULL_SYNC)
        inserted_any = False
        newest_ts = 0.0
        for m in messages:
            try:
                sender_hash = m.get("sender_hash", "")
                msg_ts = float(m.get("timestamp", time.time()))
                newest_ts = max(newest_ts, msg_ts)

                # Validate tenure for invite-only channels with tenure data.
                # Mirrors _handle_sync_request's two checks -- applied again
                # here on receipt (not just by whoever responded) so a
                # malicious or buggy responder can't hand us history we
                # aren't entitled to just by skipping its own filtering.
                if has_tenure and not self._storage.was_member_at(
                    channel_hash_hex, sender_hash, msg_ts
                ):
                    RNS.log(
                        f"TrenchChat [sync]: dropping synced message "
                        f"{str(m.get('message_id', ''))[:12]}… — sender "
                        f"{sender_hash[:12]}… was not a member at ts={msg_ts:.0f}",
                        RNS.LOG_WARNING,
                    )
                    continue
                if has_tenure and not full_sync and not self._storage.was_member_at(
                    channel_hash_hex, my_hex, msg_ts
                ):
                    RNS.log(
                        f"TrenchChat [sync]: dropping synced message "
                        f"{str(m.get('message_id', ''))[:12]}… — we were not "
                        f"yet a member at ts={msg_ts:.0f}",
                        RNS.LOG_DEBUG,
                    )
                    continue

                image_data = m.get("image_data")
                if isinstance(image_data, str):
                    image_data = image_data.encode()
                if not image_data:
                    image_data = None
                elif len(image_data) > MAX_IMAGE_BYTES:
                    image_data = None

                inserted = self._storage.insert_message(
                    channel_hash=channel_hash_hex,
                    sender_hash=sender_hash,
                    sender_name=m.get("sender_name", ""),
                    content=m.get("content", ""),
                    timestamp=msg_ts,
                    message_id=m.get("message_id", ""),
                    reply_to=m.get("reply_to"),
                    last_seen_id=m.get("last_seen_id"),
                    received_at=time.time(),
                    image_data=image_data,
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

        if newest_ts > 0.0:
            # Advance to the newest message actually present in this batch,
            # not wall-clock time -- a response capped at
            # MAX_RESPONSE_MESSAGES otherwise strands everything past the
            # cap forever, since the next request would start from "now"
            # instead of resuming right after this batch.
            self._storage.update_last_sync(channel_hash_hex, newest_ts)

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
        d = {
            "sender_hash":  row["sender_hash"],
            "sender_name":  row["sender_name"],
            "content":      row["content"],
            "timestamp":    row["timestamp"],
            "message_id":   row["message_id"],
            "reply_to":     row["reply_to"],
            "last_seen_id": row["last_seen_id"],
        }
        image_data = row["image_data"] if "image_data" in row.keys() else None
        if image_data:
            d["image_data"] = bytes(image_data)
        return d

    def _send_sync_request(self, dest_hex: str, channel_hash_hex: str, since_ts: float):
        self._record_pending_request(channel_hash_hex, dest_hex)
        self._send_raw(dest_hex, {
            F_MSG_TYPE:          MT_SYNC_REQUEST,
            F_CHANNEL_HASH:      bytes.fromhex(channel_hash_hex),
            F_SYNC_WINDOW_START: since_ts,
        })

    # --- outstanding sync request tracking ---

    def _peer_key_forms(self, peer_hex: str) -> list[str]:
        """Return every hex form an inbound message may identify this peer by.

        Handlers resolve the sender via RNS.Identity.recall() but fall back to
        the raw source_hash, which is the delivery destination hash; these are
        different values for the same peer.
        """
        forms = [peer_hex]
        try:
            delivery = RNS.Destination.hash(bytes.fromhex(peer_hex), "lxmf", "delivery")
            forms.append(delivery.hex())
        except (ValueError, TypeError):
            pass
        return forms

    def _record_pending_request(self, channel_hash_hex: str, dest_hex: str):
        """Remember that we asked dest_hex for history on this channel."""
        now = time.time()
        with self._pending_requests_lock:
            for form in self._peer_key_forms(dest_hex):
                self._pending_requests[(channel_hash_hex, form)] = now

    def _claim_pending_request(self, channel_hash_hex: str, responder_hex: str) -> bool:
        """Consume the outstanding request this response claims to answer.

        Consuming the entry makes a single request answerable only once.
        """
        now = time.time()
        with self._pending_requests_lock:
            for stale_key, ts in list(self._pending_requests.items()):
                if now - ts > SYNC_RESPONSE_WINDOW_SECS:
                    del self._pending_requests[stale_key]
            claimed = False
            for form in self._peer_key_forms(responder_hex):
                if self._pending_requests.pop((channel_hash_hex, form), None) is not None:
                    claimed = True
            return claimed

    def _deep_sync_allowed(self, channel_hash_hex: str, requester_hex: str) -> bool:
        """Rate-limit deep (pre-SYNC_WINDOW_SECS) backfill sweeps per (channel, peer).

        Records this attempt and returns True the first time a given peer
        asks for a deep sweep on a channel, then False for any further
        attempt within DEEP_SYNC_COOLDOWN_SECS. Only guards the timestamp-
        sweep fallback -- hint-targeted responses are already small and
        exact, not a bulk-sweep concern.
        """
        now = time.time()
        key = (channel_hash_hex, requester_hex)
        with self._deep_sync_lock:
            for stale_key, ts in list(self._deep_sync_last_served.items()):
                if now - ts > DEEP_SYNC_COOLDOWN_PRUNE_SECS:
                    del self._deep_sync_last_served[stale_key]
            last = self._deep_sync_last_served.get(key)
            if last is not None and now - last < DEEP_SYNC_COOLDOWN_SECS:
                return False
            self._deep_sync_last_served[key] = now
            return True

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
