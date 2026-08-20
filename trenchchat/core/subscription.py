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

import re
import threading
import time
import RNS
import LXMF
import msgpack

from trenchchat.core.control_retry import ControlRetryQueue
from trenchchat.core.identity import Identity
from trenchchat.core.permissions import is_open_join, permissions_from_json
from trenchchat.core.protocol import (
    F_CHANNEL_HASH, F_MSG_TYPE, F_SUBSCRIBER_LIST,
    F_SUBSCRIBER_SIG, F_SUBSCRIBER_VERSION,
    MT_SUBSCRIBE, MT_UNSUBSCRIBE, MT_SUBSCRIBER_LIST,
    unpack_wire,
)
from trenchchat.core.storage import Storage
from trenchchat.network.router import Router

_IDENTITY_HEX_RE = re.compile(r"^[0-9a-f]{32}$")


def _is_identity_hex(value: object) -> bool:
    return isinstance(value, str) and bool(_IDENTITY_HEX_RE.match(value))


def _subscriber_payload(channel_hash_hex: str, version: int,
                        packed_list: bytes) -> bytes:
    return msgpack.packb(
        [bytes.fromhex(channel_hash_hex), version, packed_list],
        use_bin_type=True,
    )


def _sign(identity: RNS.Identity, data: bytes) -> bytes:
    return identity.sign(data)


def _verify(identity: RNS.Identity, data: bytes, signature: bytes) -> bool:
    try:
        return identity.validate(signature, data)
    except Exception:
        return False


class SubscriptionManager:
    def __init__(self, identity: Identity, storage: Storage, router: Router):
        self._identity = identity
        self._storage = storage
        self._router = router

        # In-memory subscriber lists: channel_hash_hex -> set of identity_hash_hex.
        # Loaded from storage so a restart doesn't strand a public channel's
        # peer discovery (see storage.get_all_channel_subscribers); every
        # mutation below writes through to storage. Read path stays in
        # memory -- get_subscribers() never hits storage per call.
        self._subscribers: dict[str, set[str]] = {
            ch: set(ids) for ch, ids in storage.get_all_channel_subscribers().items()
        }
        # Monotonic per-channel counter; owners bump it, receivers reject
        # anything not newer than what they hold. Loaded from storage because
        # a counter that resets on restart is no replay defence at all: a
        # captured older list is still validly signed, and replaying it
        # resurrects removed subscribers -- which is what delivery is aimed at.
        self._subscriber_versions: dict[str, int] = dict(
            storage.get_all_subscriber_list_versions()
        )
        self._version_lock = threading.Lock()
        # A dropped MT_SUBSCRIBE left the owner unaware of a subscriber for
        # good: nothing else ever re-sends one, so the joiner was silently
        # absent from every send until they joined again.
        self._retry = ControlRetryQueue("subscription")

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
        """Return a snapshot of the current subscriber set.

        Returns a copy, not the live internal set: RNS/LXMF delivery
        callbacks mutate the internal set in place from background
        threads (_add_subscriber/_remove_subscriber), so callers that
        iterate the live set (including our own _broadcast_subscriber_list)
        can otherwise race a concurrent mutation and raise
        "set changed size during iteration".
        """
        return set(self._subscribers.get(channel_hash_hex, set()))

    def _add_subscriber(self, channel_hash_hex: str, identity_hex: str):
        if channel_hash_hex not in self._subscribers:
            self._subscribers[channel_hash_hex] = set()
        if identity_hex in self._subscribers[channel_hash_hex]:
            # Already known: re-broadcasting would let a peer resending
            # MT_SUBSCRIBE amplify one message into one per subscriber.
            return
        self._subscribers[channel_hash_hex].add(identity_hex)
        self._storage.add_channel_subscriber(channel_hash_hex, identity_hex)
        self._broadcast_subscriber_list(channel_hash_hex)

    def _remove_subscriber(self, channel_hash_hex: str, identity_hex: str):
        if channel_hash_hex in self._subscribers:
            self._subscribers[channel_hash_hex].discard(identity_hex)
        self._storage.remove_channel_subscriber(channel_hash_hex, identity_hex)

    def _broadcast_subscriber_list(self, channel_hash_hex: str):
        """Send the current subscriber list to all subscribers.

        The owner is never itself in self._subscribers (that set only tracks
        peers who sent MT_SUBSCRIBE), but is still a legitimate recipient of
        anything a subscriber broadcasts to the channel. Include it in the
        payload -- not doing so left every non-owner subscriber's local
        compute_channel_recipients() blind to the owner, so anything with no
        sync/backfill fallback (reactions, in particular -- chat messages
        happened to still arrive via the separate offline-sync mechanism)
        silently never reached the owner at all.
        """
        subs = self.get_subscribers(channel_hash_hex)
        recipients = set(subs) | {self._identity.hash_hex}
        packed = msgpack.packb(sorted(recipients), use_bin_type=True)

        version = self._next_subscriber_version(channel_hash_hex)
        signature = _sign(
            self._identity.rns_identity,
            _subscriber_payload(channel_hash_hex, version, packed),
        )

        for dest_hex in subs:
            if dest_hex == self._identity.hash_hex:
                continue
            self._send_raw(dest_hex, {
                F_MSG_TYPE:           MT_SUBSCRIBER_LIST,
                F_CHANNEL_HASH:       bytes.fromhex(channel_hash_hex),
                F_SUBSCRIBER_LIST:    packed,
                F_SUBSCRIBER_VERSION: version,
                F_SUBSCRIBER_SIG:     signature,
            })

    def _next_subscriber_version(self, channel_hash_hex: str) -> int:
        with self._version_lock:
            version = self._subscriber_versions.get(channel_hash_hex, 0) + 1
            self._subscriber_versions[channel_hash_hex] = version
            self._storage.set_subscriber_list_version(channel_hash_hex, version)
            return version

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
            # Subscribing is how an open-join channel is joined. An
            # invite-only one has a member list instead, so a subscribe there
            # asserts a membership nobody granted -- it delivers nothing,
            # since recipients come from get_members(), but it writes the
            # sender into the owner's persisted subscriber set.
            if channel and not is_open_join(
                    permissions_from_json(channel["permissions"])):
                RNS.log(
                    f"TrenchChat [subscription]: refusing subscribe from "
                    f"{sender_hex[:12]}… — {channel_hash_hex[:12]}… is not open-join",
                    RNS.LOG_WARNING,
                )
            elif channel and channel["creator_hash"] == self._identity.hash_hex:
                self._add_subscriber(channel_hash_hex, sender_hex)

        elif msg_type == MT_UNSUBSCRIBE:
            channel = self._storage.get_channel(channel_hash_hex)
            if channel and channel["creator_hash"] == self._identity.hash_hex:
                self._remove_subscriber(channel_hash_hex, sender_hex)

        elif msg_type == MT_SUBSCRIBER_LIST:
            self._handle_subscriber_list(fields, channel_hash_hex, sender_hex)

    def _handle_subscriber_list(self, fields: dict, channel_hash_hex: str,
                                sender_hex: str):
        channel = self._storage.get_channel(channel_hash_hex)
        if not channel or channel["creator_hash"] != sender_hex:
            RNS.log(
                f"TrenchChat: rejected subscriber_list for {channel_hash_hex} "
                f"from non-owner {sender_hex}",
                RNS.LOG_WARNING,
            )
            return

        packed = fields.get(F_SUBSCRIBER_LIST)
        if not packed:
            return

        version = fields.get(F_SUBSCRIBER_VERSION)
        signature = fields.get(F_SUBSCRIBER_SIG)
        if not isinstance(version, int) or not isinstance(signature, bytes):
            RNS.log(
                f"TrenchChat: rejected unsigned subscriber_list for "
                f"{channel_hash_hex[:12]}…",
                RNS.LOG_WARNING,
            )
            return

        last_seen = self._subscriber_versions.get(channel_hash_hex, 0)
        if version <= last_seen:
            RNS.log(
                f"TrenchChat: rejected replayed subscriber_list v{version} for "
                f"{channel_hash_hex[:12]}… (holding v{last_seen})",
                RNS.LOG_WARNING,
            )
            return

        owner_identity = self._recall_owner_identity(sender_hex)
        if owner_identity is None:
            return
        payload = _subscriber_payload(channel_hash_hex, version, packed)
        if not _verify(owner_identity, payload, signature):
            RNS.log(
                f"TrenchChat: rejected subscriber_list for "
                f"{channel_hash_hex[:12]}… — bad owner signature",
                RNS.LOG_WARNING,
            )
            return

        try:
            hashes = unpack_wire(packed)
        except Exception as e:
            RNS.log(f"TrenchChat: failed to parse subscriber list: {e}",
                    RNS.LOG_WARNING)
            return

        valid = {h for h in hashes if _is_identity_hex(h)}
        # Re-check the version under the lock that commits it. LXMF delivers on
        # background threads, so two lists can both pass the check above
        # against the same stale value and the loser then overwrite the
        # winner -- a silent rollback to an older signed roster.
        with self._version_lock:
            if version <= self._subscriber_versions.get(channel_hash_hex, 0):
                return
            self._subscriber_versions[channel_hash_hex] = version
            self._subscribers[channel_hash_hex] = valid
            self._storage.set_subscriber_list_version(channel_hash_hex, version)
            self._storage.replace_channel_subscribers(channel_hash_hex, valid)

    def _recall_owner_identity(self, owner_hex: str):
        if owner_hex == self._identity.hash_hex:
            return self._identity.rns_identity
        try:
            delivery_hash = RNS.Destination.hash(
                bytes.fromhex(owner_hex), "lxmf", "delivery"
            )
        except ValueError:
            return None
        return RNS.Identity.recall(delivery_hash)

    # --- helpers ---

    def _send_control(self, dest_hex: str, msg_type: str, channel_hash_hex: str):
        self._send_raw(dest_hex, {
            F_MSG_TYPE:     msg_type,
            F_CHANNEL_HASH: bytes.fromhex(channel_hash_hex),
        })

    def flush_pending(self, dest_hex: str) -> int:
        """Re-send control messages held while this peer had no known path."""
        return self._retry.flush(dest_hex, self._send_raw)

    def _send_raw(self, dest_hex: str, fields: dict) -> bool:
        """Send a control message. Returns False if it had to be queued instead."""
        try:
            identity_hash = bytes.fromhex(dest_hex)
            delivery_dest_hash = RNS.Destination.hash(identity_hash, "lxmf", "delivery")
            dest_identity = RNS.Identity.recall(delivery_dest_hash)
            if dest_identity is None:
                RNS.Transport.request_path(delivery_dest_hash)
                self._retry.queue(dest_hex, fields)
                return False
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
            return True
        except Exception as e:
            RNS.log(f"TrenchChat: subscription control send error: {e}", RNS.LOG_WARNING)
            return False
