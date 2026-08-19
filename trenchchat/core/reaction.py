"""
Emoji reaction management for TrenchChat.

A reaction attaches an emoji to a channel message.  The emoji is either a
standard unicode character or a custom image identified by its SHA-256 hash;
both are stored in the ``reactions`` table's ``emoji_hash`` column as the
reaction key.  The protocol uses three control message types:

  MT_REACTION      -- broadcast: I added/removed emoji Y on message X
  MT_EMOJI_REQUEST -- unicast:   please send me the image for emoji hash H
  MT_EMOJI_RESPONSE -- unicast:  here is the image for emoji hash H

Custom emoji images are stored in the local ``custom_emojis`` table.  When a
peer reacts with an emoji whose hash is not in our local library, or sends a
message containing a ``:name@hash:`` token we can't resolve, we automatically
fire an ``MT_EMOJI_REQUEST`` to that peer.  In-flight requests are tracked to
avoid spamming a peer, but they expire so a dropped request is retried rather
than abandoning the emoji for the life of the process.

Emoji images are capped at MAX_EMOJI_BYTES to keep them mesh-friendly.
"""

import hashlib
import re
import threading
import time

import RNS
import LXMF

from trenchchat.core.identity import Identity
from trenchchat.core.permissions import (
    SEND_MESSAGE, is_open_join, permissions_from_json,
)
from trenchchat.core.protocol import (
    F_MSG_TYPE, F_CHANNEL_HASH,
    F_EMOJI_HASH, F_EMOJI_DATA, F_EMOJI_NAME,
    F_REACTION_MSG_ID, F_REACTION_REMOVE, F_REACTION_UNICODE,
    MT_REACTION, MT_EMOJI_REQUEST, MT_EMOJI_RESPONSE,
)
from trenchchat.core.storage import Storage
from trenchchat.network.router import Router

MAX_EMOJI_BYTES = 65536   # 64 KB hard cap per emoji image

# Inbound emoji-request throttle.  Each request can pull up to MAX_EMOJI_BYTES
# back out, so this bounds the amplification a single peer can drive.
EMOJI_REQUEST_WINDOW_SECS = 60.0
EMOJI_REQUEST_BURST = 12

# How long an unanswered emoji request blocks a retry for the same hash. A
# responder drops requests silently (throttled, unknown hash, no shared
# channel), so this has to clear on its own or the emoji is never fetched
# again. Longer than EMOJI_REQUEST_WINDOW_SECS so a retry lands after the
# responder's own throttle window has drained.
EMOJI_REQUEST_RETRY_SECS = 90.0

# Emoji asked for per flush. Kept well under EMOJI_REQUEST_BURST so a large
# backlog drains over several flushes instead of re-tripping the responder's
# throttle every time and never converging.
EMOJI_FLUSH_BATCH = 6

# Minimum gap between sweeps of one peer's unresolved emoji. Deliberately
# shorter than EMOJI_REQUEST_RETRY_SECS: if the two matched, a sweep could keep
# landing just before the in-flight markers expire, find every hash still
# blocked, and send nothing for as long as the two stayed in step.
EMOJI_FLUSH_COOLDOWN_SECS = 30.0

# Matches :name@hexhash: (unambiguous) or :name: (legacy, name lookup only).
# Group 1 = name, group 2 = 64-char SHA-256 hex, absent on legacy tokens.
EMOJI_TOKEN_RE = re.compile(r":([a-zA-Z0-9_-]+)(?:@([0-9a-fA-F]{64}))?:")

_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

# Ceiling on how many requesters the throttle map tracks at once.
MAX_TRACKED_REQUESTERS = 512


def compute_emoji_hash(image_data: bytes) -> str:
    """Return the hex SHA-256 hash of raw emoji image bytes."""
    return hashlib.sha256(image_data).hexdigest()


def is_custom_emoji_hash(reaction_key: str) -> bool:
    """True if a reaction key identifies a custom emoji rather than a unicode one."""
    return bool(_SHA256_HEX_RE.match(reaction_key))


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

        # emoji_hash hex -> time we last requested it, to avoid spamming the
        # same peer for the same asset. Entries older than
        # EMOJI_REQUEST_RETRY_SECS no longer block a retry.
        self._pending_emoji_requests: dict[str, float] = {}

        # requester identity hex -> recent request timestamps, for throttling
        # inbound emoji requests (see _allow_emoji_request).
        self._emoji_request_times: dict[str, list[float]] = {}

        # peer identity hex -> time we last swept their unresolved emoji, so
        # the periodic retry doesn't re-query on every tick.
        self._last_flush_by_peer: dict[str, float] = {}

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

    def request_missing_from_content(self, sender_hex: str, content: str) -> None:
        """Fetch any :name@hash: emoji in a message body we can't resolve locally.

        Runs for every inbound chat message and for messages arriving via sync,
        so an inline custom emoji reaches a client whose UI never triggers the
        fetch itself.  Legacy :name: tokens carry no hash and cannot be
        requested; they only resolve against an emoji already held.
        """
        if not sender_hex or ":" not in content:
            return
        for m in EMOJI_TOKEN_RE.finditer(content):
            emoji_hash = m.group(2)
            if not emoji_hash:
                continue
            emoji_hash = emoji_hash.lower()
            if self._storage.emoji_exists(emoji_hash):
                continue
            self._request_emoji(sender_hex, emoji_hash, name=m.group(1))

    def flush_pending_emoji(self, peer_hex: str) -> None:
        """Re-request emoji this peer reacted with that we still don't hold.

        Requests are dropped silently by a throttled or otherwise unwilling
        responder, so this is what eventually gets the image across.
        """
        if not peer_hex:
            return
        now = time.time()
        with self._lock:
            last = self._last_flush_by_peer.get(peer_hex)
            if last is not None and now - last < EMOJI_FLUSH_COOLDOWN_SECS:
                return
            self._last_flush_by_peer[peer_hex] = now

        missing = [h for h in self._storage.get_unresolved_reaction_emoji(peer_hex)
                   if is_custom_emoji_hash(h)]
        for emoji_hash in missing[:EMOJI_FLUSH_BATCH]:
            self._request_emoji(peer_hex, emoji_hash)

    def retry_pending_emoji(self) -> None:
        """Sweep every peer holding emoji we couldn't fetch and ask again.

        Called from the same periodic maintenance tick that prunes presence.
        A peer announce is too rare to rely on -- without a timer of its own,
        an emoji dropped by the responder's throttle waits on incidental
        traffic that may never come.
        """
        for peer_hex in self._storage.get_peers_with_unresolved_emoji():
            self.flush_pending_emoji(peer_hex)

    # ------------------------------------------------------------------
    # LXMF inbound
    # ------------------------------------------------------------------

    def _on_lxmf_message(self, message: LXMF.LXMessage) -> None:
        """Delivery callback -- handle reaction-related control messages."""
        fields = message.fields or {}
        msg_type = fields.get(F_MSG_TYPE)
        if msg_type is None:
            self._handle_chat_message(message)
            return
        if isinstance(msg_type, bytes):
            msg_type = msg_type.decode(errors="replace")

        if msg_type == MT_REACTION:
            self._handle_reaction(message, fields)
        elif msg_type == MT_EMOJI_REQUEST:
            self._handle_emoji_request(message, fields)
        elif msg_type == MT_EMOJI_RESPONSE:
            self._handle_emoji_response(message, fields)

    def _handle_chat_message(self, message: LXMF.LXMessage) -> None:
        """Pull any inline custom emoji a chat message references but we lack."""
        content = message.content or b""
        if isinstance(content, bytes):
            content = content.decode(errors="replace")
        if ":" not in content:
            return
        self.request_missing_from_content(self._resolve_sender_hex(message), content)

    def _shares_any_channel(self, peer_hex: str) -> bool:
        """True if peer_hex is a member of, or subscriber to, any channel we hold.

        An open-join channel still has to name the peer: answering for anyone
        merely because we are in some public channel makes the whole check
        vacuous, and lets an unrelated node enumerate the emoji library.
        """
        if not peer_hex:
            return False
        for sub in self._storage.get_subscriptions():
            ch = sub["channel_hash"]
            channel = self._storage.get_channel(ch)
            if channel is None:
                continue
            if is_open_join(permissions_from_json(channel["permissions"])):
                if (self._storage.is_channel_subscriber(ch, peer_hex)
                        or channel["creator_hash"] == peer_hex):
                    return True
                continue
            if self._storage.is_member(ch, peer_hex):
                return True
        return False

    def _allow_emoji_request(self, requester_hex: str) -> bool:
        """Token-bucket style throttle: EMOJI_REQUEST_BURST per window per peer."""
        now = time.time()
        with self._lock:
            times = self._emoji_request_times.setdefault(requester_hex, [])
            times[:] = [t for t in times if now - t < EMOJI_REQUEST_WINDOW_SECS]
            if len(times) >= EMOJI_REQUEST_BURST:
                return False
            times.append(now)
            if len(self._emoji_request_times) > MAX_TRACKED_REQUESTERS:
                # Bounded by design, not by how many identities exist.
                for stale in [h for h, ts in self._emoji_request_times.items()
                              if not ts or now - ts[-1] > EMOJI_REQUEST_WINDOW_SECS]:
                    del self._emoji_request_times[stale]
            return True

    def _may_react(self, channel_hash_hex: str, sender_hex: str) -> bool:
        """Mirror the inbound authorisation Messaging applies to chat messages."""
        if not sender_hex:
            return False
        channel = self._storage.get_channel(channel_hash_hex)
        if channel is None:
            return False
        if is_open_join(permissions_from_json(channel["permissions"])):
            return True
        if not self._storage.is_member(channel_hash_hex, sender_hex):
            return False
        return self._storage.has_permission(channel_hash_hex, sender_hex, SEND_MESSAGE)

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

        if not self._may_react(channel_hash_hex, sender_hex):
            RNS.log(
                f"TrenchChat [reaction]: dropping reaction on "
                f"{channel_hash_hex[:12]}… from unauthorised sender "
                f"{sender_hex[:12]}…",
                RNS.LOG_WARNING,
            )
            return

        msg_id = fields.get(F_REACTION_MSG_ID, "")
        if isinstance(msg_id, bytes):
            msg_id = msg_id.decode(errors="replace")
        if not msg_id:
            return

        emoji_hash = self._reaction_key_from_fields(fields)
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
            if is_custom_emoji_hash(emoji_hash) and \
                    not self._storage.emoji_exists(emoji_hash):
                self._request_emoji(sender_hex, emoji_hash)

        self._fire_reaction_callbacks(channel_hash_hex, msg_id)

    @staticmethod
    def _reaction_key_from_fields(fields: dict) -> str:
        """Recover a reaction key from an inbound MT_REACTION's fields.

        A custom emoji rides in F_EMOJI_HASH as raw SHA-256 bytes; a unicode
        emoji rides in F_REACTION_UNICODE as text. Reading the hash field first
        keeps a peer that sets both from smuggling a mismatched pair through.
        """
        raw_hash = fields.get(F_EMOJI_HASH, b"")
        if raw_hash:
            return raw_hash.hex() if isinstance(raw_hash, bytes) else str(raw_hash)

        raw_unicode = fields.get(F_REACTION_UNICODE, "")
        if isinstance(raw_unicode, bytes):
            raw_unicode = raw_unicode.decode(errors="replace")
        return str(raw_unicode)

    def _handle_emoji_request(self, message: LXMF.LXMessage, fields: dict) -> None:
        """Respond to an MT_EMOJI_REQUEST by sending the emoji image if we have it.

        The name from the request is echoed back so the receiver can store the
        emoji under the correct human-readable name.
        """
        requester_hex = self._resolve_sender_hex(message)
        if not requester_hex:
            return

        # One small request produces up to MAX_EMOJI_BYTES in reply.
        if not self._shares_any_channel(requester_hex):
            RNS.log(
                f"TrenchChat [reaction]: ignoring emoji request from "
                f"{requester_hex[:12]}… — no shared channel",
                RNS.LOG_DEBUG,
            )
            return
        if not self._allow_emoji_request(requester_hex):
            RNS.log(
                f"TrenchChat [reaction]: rate-limited emoji request from "
                f"{requester_hex[:12]}…",
                RNS.LOG_WARNING,
            )
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

        # Only in answer to a request we made. Storing unsolicited emoji lets
        # any authenticated peer write into the local library at will, and a
        # fresh hash per push defeats the emoji_exists de-duplication below.
        with self._lock:
            if emoji_hash not in self._pending_emoji_requests:
                RNS.log(
                    f"TrenchChat [reaction]: discarded unsolicited emoji "
                    f"{emoji_hash[:12]}…",
                    RNS.LOG_WARNING,
                )
                return
            self._pending_emoji_requests.pop(emoji_hash, None)

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
        own_hex = self._identity.hash_hex

        if is_custom_emoji_hash(emoji_hash):
            emoji_field = {F_EMOJI_HASH: bytes.fromhex(emoji_hash)}
        else:
            emoji_field = {F_REACTION_UNICODE: emoji_hash}

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
                    F_REACTION_REMOVE:   remove,
                    **emoji_field,
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
        now = time.time()
        with self._lock:
            last = self._pending_emoji_requests.get(emoji_hash)
            if last is not None and now - last < EMOJI_REQUEST_RETRY_SECS:
                return
            self._pending_emoji_requests[emoji_hash] = now

        try:
            identity_hash = bytes.fromhex(peer_hex)
            delivery_dest_hash = RNS.Destination.hash(identity_hash, "lxmf", "delivery")
            dest_identity = RNS.Identity.recall(delivery_dest_hash)
            if dest_identity is None:
                RNS.Transport.request_path(delivery_dest_hash)
                with self._lock:
                    self._pending_emoji_requests.pop(emoji_hash, None)
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
                self._pending_emoji_requests.pop(emoji_hash, None)
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
