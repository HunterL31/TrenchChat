"""
Avatar management for TrenchChat user profile pictures.

Profile pictures are:
  - Stored locally as 48x48 JPEG blobs (own avatar in Config, peers in SQLite)
  - Transmitted as a dedicated LXMF control message (MT_AVATAR_UPDATE)
  - Sent once per peer per avatar version -- not attached to every chat message
  - Delivered immediately to reachable peers on change; deferred to
    flush_avatar() when a peer reappears after being offline

Send rate limiting:  no more than one avatar change per SEND_RATE_LIMIT_SECS.
Receive rate limiting: at most one inbound avatar update accepted per peer per
                       RECEIVE_RATE_LIMIT_SECS, regardless of version numbers.
"""

import io
import threading
import time

import RNS
import LXMF
from PIL import Image

from trenchchat.config import Config
from trenchchat.core.identity import Identity
from trenchchat.core.protocol import (
    F_MSG_TYPE, F_AVATAR_DATA, F_AVATAR_VERSION,
    MT_AVATAR_UPDATE,
)
from trenchchat.core.storage import Storage
from trenchchat.network.router import Router

AVATAR_SIZE_PX = 48
AVATAR_JPEG_QUALITY = 70
MAX_AVATAR_BYTES = 4096

SEND_RATE_LIMIT_SECS = 60
RECEIVE_RATE_LIMIT_SECS = 300


def compress_avatar(image_bytes: bytes) -> bytes:
    """Resize and JPEG-compress raw image bytes to a 48x48 avatar.

    Center-crops the source image to a square before resizing so the
    subject is not distorted.  Raises ValueError if the resulting JPEG
    exceeds MAX_AVATAR_BYTES after compression.
    """
    img = Image.open(io.BytesIO(image_bytes))
    img = img.convert("RGB")

    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    img = img.crop((left, top, left + side, top + side))
    img = img.resize((AVATAR_SIZE_PX, AVATAR_SIZE_PX), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=AVATAR_JPEG_QUALITY, optimize=True)
    result = buf.getvalue()

    if len(result) > MAX_AVATAR_BYTES:
        raise ValueError(
            f"Compressed avatar is {len(result)} bytes, exceeds {MAX_AVATAR_BYTES} limit"
        )
    return result


class AvatarManager:
    """Manages sending, receiving, caching, and delivery tracking of user avatars."""

    def __init__(self, identity: Identity, config: Config,
                 storage: Storage, router: Router):
        self._identity = identity
        self._config = config
        self._storage = storage
        self._router = router

        self._avatar_callbacks: list = []
        self._lock = threading.Lock()

        # identity_hash_hex -> last accepted inbound avatar timestamp
        self._last_received: dict[str, float] = {}

        # track our own last change time for send rate limiting
        self._last_changed: float = 0.0

        router.add_delivery_callback(self._on_lxmf_message)

    # --- public API: own avatar ---

    def set_avatar(self, jpeg_bytes: bytes,
                   subscriber_lookup: "callable[[str], set[str]]") -> None:
        """Set our own avatar, increment the version, and push to all reachable peers.

        jpeg_bytes must already be compressed to <= MAX_AVATAR_BYTES (use
        compress_avatar() before calling).  subscriber_lookup is a callable
        that accepts a channel_hash_hex and returns the set of subscriber
        identity hashes for that channel; it is used to collect all peers to notify.

        Raises ValueError if the bytes are too large.
        Raises RuntimeError if the send rate limit has not elapsed.
        """
        if len(jpeg_bytes) > MAX_AVATAR_BYTES:
            raise ValueError(
                f"Avatar is {len(jpeg_bytes)} bytes, max is {MAX_AVATAR_BYTES}"
            )

        with self._lock:
            now = time.time()
            if now - self._last_changed < SEND_RATE_LIMIT_SECS:
                remaining = int(SEND_RATE_LIMIT_SECS - (now - self._last_changed))
                raise RuntimeError(
                    f"Avatar change rate limited: please wait {remaining}s"
                )
            self._last_changed = now

        new_version = self._config.avatar_version + 1
        self._config.avatar_bytes = jpeg_bytes
        self._config.avatar_version = new_version

        self._storage.clear_avatar_deliveries()

        peers = self._collect_all_peers(subscriber_lookup)
        for peer_hex in peers:
            self._send_avatar_to(peer_hex, jpeg_bytes, new_version)

        RNS.log(
            f"TrenchChat [avatar]: avatar updated to version {new_version}, "
            f"notifying {len(peers)} peer(s)",
            RNS.LOG_NOTICE,
        )

    def remove_avatar(self, subscriber_lookup: "callable[[str], set[str]]") -> None:
        """Clear our own avatar and notify all reachable peers.

        Sends an MT_AVATAR_UPDATE with empty avatar_data so peers know to
        remove our cached avatar.
        """
        with self._lock:
            now = time.time()
            if now - self._last_changed < SEND_RATE_LIMIT_SECS:
                remaining = int(SEND_RATE_LIMIT_SECS - (now - self._last_changed))
                raise RuntimeError(
                    f"Avatar change rate limited: please wait {remaining}s"
                )
            self._last_changed = now

        new_version = self._config.avatar_version + 1
        self._config.avatar_bytes = None
        self._config.avatar_version = new_version

        self._storage.clear_avatar_deliveries()

        peers = self._collect_all_peers(subscriber_lookup)
        for peer_hex in peers:
            self._send_avatar_to(peer_hex, b"", new_version)

        RNS.log(
            f"TrenchChat [avatar]: avatar removed (version {new_version}), "
            f"notifying {len(peers)} peer(s)",
            RNS.LOG_NOTICE,
        )

    def get_own_avatar(self) -> bytes | None:
        """Return our own avatar bytes, or None if not set."""
        return self._config.avatar_bytes

    # --- deferred delivery ---

    def flush_avatar(self, peer_hex: str) -> None:
        """Send our current avatar to a peer that just came online, if needed.

        Skips delivery if the peer has already received the current version.
        Called from the peer-appeared callback in main_window.py.
        """
        own_avatar = self._config.avatar_bytes
        if own_avatar is None:
            return

        current_version = self._config.avatar_version
        delivered_version = self._storage.get_avatar_delivery_version(peer_hex)
        if delivered_version == current_version:
            return

        self._send_avatar_to(peer_hex, own_avatar, current_version)
        RNS.log(
            f"TrenchChat [avatar]: flushed avatar (v{current_version}) to {peer_hex[:12]}…",
            RNS.LOG_DEBUG,
        )

    # --- callbacks ---

    def add_avatar_callback(self, cb) -> None:
        """Register a callback fired with (identity_hash_hex: str) when a peer avatar arrives."""
        self._avatar_callbacks.append(cb)

    # --- inbound ---

    def _on_lxmf_message(self, message: LXMF.LXMessage) -> None:
        """LXMF delivery callback -- handle MT_AVATAR_UPDATE control messages."""
        fields = message.fields or {}
        msg_type = fields.get(F_MSG_TYPE)
        if msg_type is None:
            return
        if isinstance(msg_type, bytes):
            msg_type = msg_type.decode(errors="replace")
        if msg_type != MT_AVATAR_UPDATE:
            return

        sender_identity = (
            RNS.Identity.recall(message.source_hash)
            if message.source_hash else None
        )
        sender_hex = (
            sender_identity.hash.hex() if sender_identity
            else (message.source_hash.hex() if message.source_hash else "")
        )
        if not sender_hex:
            RNS.log("TrenchChat [avatar]: received avatar update with unknown sender",
                    RNS.LOG_WARNING)
            return

        avatar_data = fields.get(F_AVATAR_DATA, b"")
        if isinstance(avatar_data, str):
            avatar_data = avatar_data.encode()

        avatar_version = fields.get(F_AVATAR_VERSION, 0)
        if isinstance(avatar_version, bytes):
            try:
                avatar_version = int.from_bytes(avatar_version, "big")
            except Exception:
                avatar_version = 0

        # Validate size before any processing
        if len(avatar_data) > MAX_AVATAR_BYTES:
            RNS.log(
                f"TrenchChat [avatar]: rejected oversized avatar from {sender_hex[:12]}… "
                f"({len(avatar_data)} bytes)",
                RNS.LOG_WARNING,
            )
            return

        # Receive rate limiting
        with self._lock:
            now = time.time()
            last = self._last_received.get(sender_hex, 0.0)
            if now - last < RECEIVE_RATE_LIMIT_SECS:
                RNS.log(
                    f"TrenchChat [avatar]: rate-limited inbound avatar from {sender_hex[:12]}…",
                    RNS.LOG_DEBUG,
                )
                return
            self._last_received[sender_hex] = now

        if avatar_data:
            self._storage.upsert_peer_avatar(sender_hex, avatar_data, avatar_version)
            RNS.log(
                f"TrenchChat [avatar]: stored avatar v{avatar_version} "
                f"from {sender_hex[:12]}…",
                RNS.LOG_NOTICE,
            )
        else:
            self._storage.delete_peer_avatar(sender_hex)
            RNS.log(
                f"TrenchChat [avatar]: peer {sender_hex[:12]}… removed their avatar",
                RNS.LOG_NOTICE,
            )

        for cb in self._avatar_callbacks:
            try:
                cb(sender_hex)
            except Exception as e:
                RNS.log(f"TrenchChat [avatar]: callback error: {e}", RNS.LOG_ERROR)

    # --- private helpers ---

    def _collect_all_peers(
        self, subscriber_lookup: "callable[[str], set[str]]"
    ) -> set[str]:
        """Return the union of all known peers across all subscribed channels."""
        own_hex = self._identity.hash_hex
        peers: set[str] = set()
        for sub in self._storage.get_subscriptions():
            channel_hash = sub["channel_hash"]
            # Invite-only: use member list
            members = self._storage.get_members(channel_hash)
            for row in members:
                ih = row["identity_hash"]
                if ih != own_hex:
                    peers.add(ih)
            # Public channels: also check subscriber list from subscription manager
            try:
                for ih in subscriber_lookup(channel_hash):
                    if ih != own_hex:
                        peers.add(ih)
            except Exception:
                pass
        return peers

    def _send_avatar_to(self, peer_hex: str, avatar_data: bytes,
                        avatar_version: int) -> None:
        """Attempt to send an MT_AVATAR_UPDATE control message to a single peer.

        Records the delivery if the send does not fail immediately.
        Silently skips peers whose RNS path is not yet known.
        """
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
            lxm.fields = {
                F_MSG_TYPE:      MT_AVATAR_UPDATE,
                F_AVATAR_DATA:   avatar_data,
                F_AVATAR_VERSION: avatar_version,
            }
            self._router.send(lxm)
            self._storage.upsert_avatar_delivery(peer_hex, avatar_version)
        except Exception as e:
            RNS.log(
                f"TrenchChat [avatar]: send error to {peer_hex[:12]}…: {e}",
                RNS.LOG_WARNING,
            )
