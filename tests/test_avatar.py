"""
Tests for avatar image processing, storage, delivery tracking, and rate limiting.

Covers:
  - compress_avatar() resizing and JPEG output
  - AvatarManager.set_avatar() config persistence and version incrementing
  - Send rate limiting on set_avatar() / remove_avatar()
  - Receive rate limiting on inbound MT_AVATAR_UPDATE messages
  - Delivery tracking (clear on change, flush_avatar deferred delivery)
  - Inbound avatar storage in peer_avatars table
"""

import io
import time
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from trenchchat.config import Config
from trenchchat.core.avatar import (
    AvatarManager,
    AVATAR_SIZE_PX,
    MAX_AVATAR_BYTES,
    RECEIVE_RATE_LIMIT_SECS,
    SEND_RATE_LIMIT_SECS,
    compress_avatar,
)
from trenchchat.core.protocol import F_MSG_TYPE, F_AVATAR_DATA, F_AVATAR_VERSION, MT_AVATAR_UPDATE
from trenchchat.core.permissions import PRESET_PRIVATE, ROLE_MEMBER
from trenchchat.core.storage import Storage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_test_jpeg(width: int = 100, height: int = 100,
                    color: tuple = (180, 100, 60)) -> bytes:
    """Return a minimal JPEG image as bytes."""
    img = Image.new("RGB", (width, height), color=color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _make_identity_mock(hex_str: str):
    """Return a minimal mock that has .hash_hex and .hash."""
    m = MagicMock()
    m.hash_hex = hex_str
    m.hash = bytes.fromhex(hex_str)
    return m


def _make_lxm(fields: dict, source_hash_hex: str | None = None):
    """Return a minimal mock LXMessage."""
    lxm = MagicMock()
    lxm.fields = fields
    if source_hash_hex:
        lxm.source_hash = bytes.fromhex(source_hash_hex)
    else:
        lxm.source_hash = None
    return lxm


@pytest.fixture
def db(tmp_path) -> Storage:
    s = Storage(db_path=tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(data_dir=tmp_path)


def _share_channel_with(mgr, sender_hex: str) -> None:
    """Put a peer in a channel we hold; storing their avatar requires it."""
    channel_hex = "cc" * 16
    mgr._storage.upsert_channel(
        hash=channel_hex, name="test", description="",
        creator_hash="aa" * 16, permissions=PRESET_PRIVATE, created_at=0.0,
    )
    mgr._storage.subscribe(channel_hex)
    mgr._storage.upsert_member(channel_hex, sender_hex, "Member", role=ROLE_MEMBER)


@pytest.fixture
def avatar_mgr(tmp_path, config):
    """AvatarManager with mocked identity and router."""
    identity = _make_identity_mock("aa" * 16)
    storage = Storage(db_path=tmp_path / "av.db")
    router = MagicMock()
    router.delivery_destination = MagicMock()
    mgr = AvatarManager(identity, config, storage, router)
    yield mgr
    storage.close()


# ---------------------------------------------------------------------------
# compress_avatar
# ---------------------------------------------------------------------------

class TestCompressAvatar:
    def test_resizes_to_avatar_size(self):
        jpeg = _make_test_jpeg(200, 300)
        result = compress_avatar(jpeg)
        img = Image.open(io.BytesIO(result))
        assert img.size == (AVATAR_SIZE_PX, AVATAR_SIZE_PX)

    def test_output_is_jpeg(self):
        jpeg = _make_test_jpeg(64, 64)
        result = compress_avatar(jpeg)
        img = Image.open(io.BytesIO(result))
        assert img.format == "JPEG"

    def test_output_within_size_limit(self):
        jpeg = _make_test_jpeg(100, 100)
        result = compress_avatar(jpeg)
        assert len(result) <= MAX_AVATAR_BYTES

    def test_center_crops_non_square(self):
        """A 200×100 image should produce a square output at AVATAR_SIZE_PX (not stretched)."""
        jpeg = _make_test_jpeg(200, 100)
        result = compress_avatar(jpeg)
        img = Image.open(io.BytesIO(result))
        assert img.size == (AVATAR_SIZE_PX, AVATAR_SIZE_PX)


# ---------------------------------------------------------------------------
# AvatarManager.set_avatar -- config persistence and version
# ---------------------------------------------------------------------------

class TestSetAvatar:
    def test_set_avatar_updates_config(self, avatar_mgr, config):
        jpeg = _make_test_jpeg()
        result = compress_avatar(jpeg)
        avatar_mgr.set_avatar(result, lambda _: set())
        assert config.avatar_bytes == result

    def test_set_avatar_increments_version(self, avatar_mgr, config):
        jpeg = _make_test_jpeg()
        result = compress_avatar(jpeg)
        initial_version = config.avatar_version
        avatar_mgr.set_avatar(result, lambda _: set())
        assert config.avatar_version == initial_version + 1

    def test_remove_avatar_clears_bytes(self, avatar_mgr, config):
        jpeg = _make_test_jpeg()
        result = compress_avatar(jpeg)
        avatar_mgr.set_avatar(result, lambda _: set())
        # Advance mock time so rate limit doesn't fire
        avatar_mgr._last_changed = 0.0
        avatar_mgr.remove_avatar(lambda _: set())
        assert config.avatar_bytes is None

    def test_set_avatar_rejects_oversized(self, avatar_mgr):
        oversized = b"x" * (MAX_AVATAR_BYTES + 1)
        with pytest.raises(ValueError, match="max is"):
            avatar_mgr.set_avatar(oversized, lambda _: set())

    def test_set_avatar_fires_callback_with_own_identity(self, avatar_mgr):
        fired = []
        avatar_mgr.add_avatar_callback(fired.append)
        jpeg = compress_avatar(_make_test_jpeg())
        avatar_mgr.set_avatar(jpeg, lambda _: set())
        assert fired == [avatar_mgr._identity.hash_hex]

    def test_remove_avatar_fires_callback_with_own_identity(self, avatar_mgr, config):
        jpeg = compress_avatar(_make_test_jpeg())
        avatar_mgr.set_avatar(jpeg, lambda _: set())
        avatar_mgr._last_changed = 0.0
        fired = []
        avatar_mgr.add_avatar_callback(fired.append)
        avatar_mgr.remove_avatar(lambda _: set())
        assert fired == [avatar_mgr._identity.hash_hex]

    def test_set_avatar_clears_delivery_records(self, avatar_mgr):
        jpeg = compress_avatar(_make_test_jpeg())
        peer_hex = "bb" * 16
        avatar_mgr._storage.upsert_avatar_delivery(peer_hex, 1)
        assert avatar_mgr._storage.get_avatar_delivery_version(peer_hex) == 1

        avatar_mgr.set_avatar(jpeg, lambda _: set())
        assert avatar_mgr._storage.get_avatar_delivery_version(peer_hex) is None


# ---------------------------------------------------------------------------
# Send rate limiting
# ---------------------------------------------------------------------------

class TestSendRateLimit:
    def test_second_set_avatar_within_rate_limit_raises(self, avatar_mgr):
        jpeg = compress_avatar(_make_test_jpeg())
        avatar_mgr.set_avatar(jpeg, lambda _: set())
        with pytest.raises(RuntimeError, match="rate limited"):
            avatar_mgr.set_avatar(jpeg, lambda _: set())

    def test_set_avatar_allowed_after_rate_limit_elapsed(self, avatar_mgr):
        jpeg = compress_avatar(_make_test_jpeg())
        avatar_mgr.set_avatar(jpeg, lambda _: set())
        # Manually backdate the last change to simulate time passing
        avatar_mgr._last_changed = time.time() - SEND_RATE_LIMIT_SECS - 1
        # Should not raise
        avatar_mgr.set_avatar(jpeg, lambda _: set())

    def test_remove_avatar_also_rate_limited(self, avatar_mgr):
        jpeg = compress_avatar(_make_test_jpeg())
        avatar_mgr.set_avatar(jpeg, lambda _: set())
        with pytest.raises(RuntimeError, match="rate limited"):
            avatar_mgr.remove_avatar(lambda _: set())


# ---------------------------------------------------------------------------
# Receive rate limiting
# ---------------------------------------------------------------------------

class TestReceiveRateLimit:
    def _share_channel(self, mgr: AvatarManager, sender_hex: str) -> None:
        """Put the sender in a channel we hold.

        Storing an avatar is gated on that: nothing else requires membership
        to reach us, and the per-sender rate limit bounds one identity rather
        than the table, which identities being free to mint makes moot.
        """
        _share_channel_with(mgr, sender_hex)

    def _send_avatar_lxm(self, mgr: AvatarManager, sender_hex: str,
                         avatar_data: bytes, version: int = 1,
                         share: bool = True):
        """Simulate delivering an MT_AVATAR_UPDATE to the manager.

        *share* puts the sender in a channel we hold first, which storing an
        avatar now requires; pass False to deliver as a stranger.
        """
        if share:
            self._share_channel(mgr, sender_hex)
        lxm = _make_lxm(
            {
                F_MSG_TYPE: MT_AVATAR_UPDATE,
                F_AVATAR_DATA: avatar_data,
                F_AVATAR_VERSION: version,
            },
            source_hash_hex=None,
        )
        # Patch RNS.Identity.recall to return a mock identity
        mock_identity = MagicMock()
        mock_identity.hash = bytes.fromhex(sender_hex)
        lxm.source_hash = bytes.fromhex(sender_hex)
        with patch("trenchchat.core.avatar.RNS.Identity.recall", return_value=mock_identity):
            mgr._on_lxmf_message(lxm)

    def test_first_avatar_accepted(self, avatar_mgr):
        jpeg = compress_avatar(_make_test_jpeg())
        sender = "cc" * 16
        self._send_avatar_lxm(avatar_mgr, sender, jpeg, version=1)
        row = avatar_mgr._storage.get_peer_avatar(sender)
        assert row is not None
        assert bytes(row["avatar_data"]) == jpeg

    def test_second_avatar_within_rate_limit_rejected(self, avatar_mgr):
        jpeg = compress_avatar(_make_test_jpeg())
        sender = "cc" * 16
        self._send_avatar_lxm(avatar_mgr, sender, jpeg, version=1)
        # Second update immediately: should be rate-limited
        jpeg2 = compress_avatar(_make_test_jpeg(color=(10, 20, 30)))
        self._send_avatar_lxm(avatar_mgr, sender, jpeg2, version=2)
        # DB should still have first avatar
        row = avatar_mgr._storage.get_peer_avatar(sender)
        assert bytes(row["avatar_data"]) == jpeg

    def test_receive_limit_no_stricter_than_send_limit(self):
        """
        Regression test: RECEIVE_RATE_LIMIT_SECS was 300 while
        SEND_RATE_LIMIT_SECS was 60, so a sender doing exactly what its own
        throttle allows (e.g. set, then remove 60s later) had the second
        change silently dropped by every receiver for up to 5 minutes -- a
        legitimate, correctly-throttled update looked identical to an
        abusive one from the receiver's side.
        """
        assert RECEIVE_RATE_LIMIT_SECS <= SEND_RATE_LIMIT_SECS

    def test_second_avatar_after_rate_limit_accepted(self, avatar_mgr):
        jpeg = compress_avatar(_make_test_jpeg())
        sender = "cc" * 16
        self._send_avatar_lxm(avatar_mgr, sender, jpeg, version=1)
        # Backdate the last-received time to simulate rate limit window elapsed
        with avatar_mgr._lock:
            avatar_mgr._last_received[sender] = (
                time.time() - RECEIVE_RATE_LIMIT_SECS - 1
            )
        jpeg2 = compress_avatar(_make_test_jpeg(color=(10, 20, 30)))
        self._send_avatar_lxm(avatar_mgr, sender, jpeg2, version=2)
        row = avatar_mgr._storage.get_peer_avatar(sender)
        assert bytes(row["avatar_data"]) == jpeg2

    def test_oversized_avatar_rejected(self, avatar_mgr):
        sender = "dd" * 16
        oversized = b"x" * (MAX_AVATAR_BYTES + 1)
        lxm = _make_lxm(
            {
                F_MSG_TYPE: MT_AVATAR_UPDATE,
                F_AVATAR_DATA: oversized,
                F_AVATAR_VERSION: 1,
            }
        )
        mock_identity = MagicMock()
        mock_identity.hash = bytes.fromhex(sender)
        lxm.source_hash = bytes.fromhex(sender)
        with patch("trenchchat.core.avatar.RNS.Identity.recall", return_value=mock_identity):
            avatar_mgr._on_lxmf_message(lxm)
        assert avatar_mgr._storage.get_peer_avatar(sender) is None

    def test_older_avatar_version_cannot_overwrite_newer(self, avatar_mgr):
        """
        avatar_version is documented as a monotonic counter but was never
        compared against what is already stored, so an older -- or replayed --
        update silently replaced a newer avatar.
        """
        jpeg = compress_avatar(_make_test_jpeg())
        sender = "ef" * 16
        self._share_channel(avatar_mgr, sender)
        mock_identity = MagicMock()
        mock_identity.hash = bytes.fromhex(sender)

        lxm1 = _make_lxm(
            {F_MSG_TYPE: MT_AVATAR_UPDATE, F_AVATAR_DATA: jpeg, F_AVATAR_VERSION: 7}
        )
        lxm1.source_hash = bytes.fromhex(sender)
        with patch("trenchchat.core.avatar.RNS.Identity.recall", return_value=mock_identity):
            avatar_mgr._on_lxmf_message(lxm1)
        assert avatar_mgr._storage.get_peer_avatar(sender)["avatar_version"] == 7

        with avatar_mgr._lock:
            avatar_mgr._last_received[sender] = (
                time.time() - RECEIVE_RATE_LIMIT_SECS - 1
            )

        # A rollback to an earlier version, and a removal at that version,
        # must both be ignored.
        lxm2 = _make_lxm(
            {F_MSG_TYPE: MT_AVATAR_UPDATE, F_AVATAR_DATA: b"", F_AVATAR_VERSION: 3}
        )
        lxm2.source_hash = bytes.fromhex(sender)
        with patch("trenchchat.core.avatar.RNS.Identity.recall", return_value=mock_identity):
            avatar_mgr._on_lxmf_message(lxm2)

        row = avatar_mgr._storage.get_peer_avatar(sender)
        assert row is not None and row["avatar_version"] == 7, \
            "An older avatar version overwrote a newer one"

    def test_remove_avatar_clears_peer_cache(self, avatar_mgr):
        """An MT_AVATAR_UPDATE with empty avatar_data removes the stored avatar."""
        jpeg = compress_avatar(_make_test_jpeg())
        sender = "ee" * 16
        self._share_channel(avatar_mgr, sender)

        # Store initial avatar
        lxm1 = _make_lxm(
            {F_MSG_TYPE: MT_AVATAR_UPDATE, F_AVATAR_DATA: jpeg, F_AVATAR_VERSION: 1}
        )
        mock_identity = MagicMock()
        mock_identity.hash = bytes.fromhex(sender)
        lxm1.source_hash = bytes.fromhex(sender)
        with patch("trenchchat.core.avatar.RNS.Identity.recall", return_value=mock_identity):
            avatar_mgr._on_lxmf_message(lxm1)
        assert avatar_mgr._storage.get_peer_avatar(sender) is not None

        # Backdate so rate limit doesn't block
        with avatar_mgr._lock:
            avatar_mgr._last_received[sender] = (
                time.time() - RECEIVE_RATE_LIMIT_SECS - 1
            )

        # Remove avatar (empty bytes)
        lxm2 = _make_lxm(
            {F_MSG_TYPE: MT_AVATAR_UPDATE, F_AVATAR_DATA: b"", F_AVATAR_VERSION: 2}
        )
        lxm2.source_hash = bytes.fromhex(sender)
        with patch("trenchchat.core.avatar.RNS.Identity.recall", return_value=mock_identity):
            avatar_mgr._on_lxmf_message(lxm2)
        assert avatar_mgr._storage.get_peer_avatar(sender) is None


# ---------------------------------------------------------------------------
# Delivery tracking and flush_avatar
# ---------------------------------------------------------------------------

class TestDeliveryTracking:
    def test_flush_avatar_skips_already_delivered(self, avatar_mgr, config):
        jpeg = compress_avatar(_make_test_jpeg())
        config.avatar_bytes = jpeg
        config.avatar_version = 3
        peer_hex = "ff" * 16
        avatar_mgr._storage.upsert_avatar_delivery(peer_hex, 3)

        sent = []
        avatar_mgr._send_avatar_to = lambda h, d, v: sent.append(h)
        avatar_mgr.flush_avatar(peer_hex)
        assert sent == [], "Should not send if peer already has current version"

    def test_flush_avatar_sends_to_undelivered_peer(self, avatar_mgr, config):
        jpeg = compress_avatar(_make_test_jpeg())
        config.avatar_bytes = jpeg
        config.avatar_version = 5
        peer_hex = "11" * 16
        # No delivery record exists
        sent = []
        avatar_mgr._send_avatar_to = lambda h, d, v: sent.append((h, v))
        avatar_mgr.flush_avatar(peer_hex)
        assert sent == [(peer_hex, 5)]

    def test_flush_avatar_sends_to_outdated_peer(self, avatar_mgr, config):
        jpeg = compress_avatar(_make_test_jpeg())
        config.avatar_bytes = jpeg
        config.avatar_version = 7
        peer_hex = "22" * 16
        avatar_mgr._storage.upsert_avatar_delivery(peer_hex, 4)  # old version

        sent = []
        avatar_mgr._send_avatar_to = lambda h, d, v: sent.append((h, v))
        avatar_mgr.flush_avatar(peer_hex)
        assert sent == [(peer_hex, 7)]

    def test_flush_avatar_noop_when_no_own_avatar(self, avatar_mgr, config):
        config.avatar_bytes = None
        sent = []
        avatar_mgr._send_avatar_to = lambda h, d, v: sent.append(h)
        avatar_mgr.flush_avatar("33" * 16)
        assert sent == []

    def test_flush_avatar_sends_removal_after_real_removal_flow(self, avatar_mgr, config):
        """
        Regression test: flush_avatar() used to bail out unconditionally when
        own_avatar was None, so a peer who only learns about us via announce
        (never received remove_avatar()'s own direct push, e.g. because we
        shared no channel with them at the time) would never find out we
        removed our avatar -- stuck with the stale cached copy forever.

        Goes through the real remove_avatar() path deliberately: it calls
        clear_avatar_deliveries(), wiping delivery records for every peer, so
        a fix that only checked "does this peer have a stale delivered
        version" would never fire here -- delivered_version looks identical
        to a peer who was never told anything. The guard has to be based on
        whether an avatar was ever set (version 0 vs not), not on delivery
        history, which this flow always clears.
        """
        config.avatar_bytes = _make_test_jpeg()
        config.avatar_version = 1
        peer_hex = "55" * 16
        avatar_mgr._storage.upsert_avatar_delivery(peer_hex, 1)  # had the old avatar

        avatar_mgr.remove_avatar(subscriber_lookup=lambda ch: set())  # no shared channels

        sent = []
        avatar_mgr._send_avatar_to = lambda h, d, v: sent.append((h, d, v))
        avatar_mgr.flush_avatar(peer_hex)
        assert len(sent) == 1 and sent[0][0] == peer_hex and sent[0][1] == b"", \
            "removal was not flushed to a peer after remove_avatar() cleared delivery tracking"

    def test_avatar_callback_fires_on_inbound(self, avatar_mgr):
        jpeg = compress_avatar(_make_test_jpeg())
        sender = "44" * 16
        _share_channel_with(avatar_mgr, sender)
        received: list[str] = []
        avatar_mgr.add_avatar_callback(received.append)

        lxm = _make_lxm(
            {F_MSG_TYPE: MT_AVATAR_UPDATE, F_AVATAR_DATA: jpeg, F_AVATAR_VERSION: 1}
        )
        mock_identity = MagicMock()
        mock_identity.hash = bytes.fromhex(sender)
        lxm.source_hash = bytes.fromhex(sender)
        with patch("trenchchat.core.avatar.RNS.Identity.recall", return_value=mock_identity):
            avatar_mgr._on_lxmf_message(lxm)

        assert received == [sender]


class TestDeliveryRetry:
    """Bug 80: a failed/unconfirmed delivery must be retried, not leave the
    peer stuck on a stale avatar until the next avatar change."""

    def test_failed_delivery_undoes_record_and_queues_retry(self, avatar_mgr, config):
        config.avatar_bytes = _make_test_jpeg()
        config.avatar_version = 2
        peer_hex = "77" * 16
        avatar_mgr._storage.upsert_avatar_delivery(peer_hex, 2)

        with patch("trenchchat.core.avatar.RNS.Transport.request_path"):
            avatar_mgr._on_delivery_failed(peer_hex, 2)

        assert avatar_mgr._storage.get_avatar_delivery_version(peer_hex) is None
        assert peer_hex in avatar_mgr._pending

    def test_flush_retries_a_pending_peer(self, avatar_mgr, config):
        config.avatar_bytes = _make_test_jpeg()
        config.avatar_version = 2
        peer_hex = "88" * 16

        with patch("trenchchat.core.avatar.RNS.Transport.request_path"):
            avatar_mgr._on_delivery_failed(peer_hex, 2)

        sent = []
        avatar_mgr._send_avatar_to = lambda h, d, v: sent.append((h, v))
        avatar_mgr.flush_avatar(peer_hex)
        assert sent == [(peer_hex, 2)]

    def test_flush_retries_even_if_delivery_record_matches(self, avatar_mgr, config):
        """A queued peer is retried even when a stale delivery record claims
        the current version was delivered."""
        config.avatar_bytes = _make_test_jpeg()
        config.avatar_version = 3
        peer_hex = "99" * 16
        avatar_mgr._storage.upsert_avatar_delivery(peer_hex, 3)
        avatar_mgr._queue_pending(peer_hex, 3)

        sent = []
        avatar_mgr._send_avatar_to = lambda h, d, v: sent.append((h, v))
        avatar_mgr.flush_avatar(peer_hex)
        assert sent == [(peer_hex, 3)]

    def test_unknown_path_queues_pending_and_requests_path(self, avatar_mgr):
        peer_hex = "aa" * 16
        with patch("trenchchat.core.avatar.RNS.Identity.recall", return_value=None), \
                patch("trenchchat.core.avatar.RNS.Transport.request_path") as req:
            avatar_mgr._send_avatar_to(peer_hex, b"data", 1)
        assert peer_hex in avatar_mgr._pending
        assert avatar_mgr._storage.get_avatar_delivery_version(peer_hex) is None
        req.assert_called_once()

    def test_successful_send_clears_pending(self, avatar_mgr):
        peer_hex = "bc" * 16
        avatar_mgr._queue_pending(peer_hex, 1)

        mock_identity = MagicMock()
        with patch("trenchchat.core.avatar.RNS.Identity.recall", return_value=mock_identity), \
                patch("trenchchat.core.avatar.RNS.Destination"), \
                patch("trenchchat.core.avatar.LXMF.LXMessage"):
            avatar_mgr._send_avatar_to(peer_hex, b"data", 1)

        assert peer_hex not in avatar_mgr._pending
        assert avatar_mgr._storage.get_avatar_delivery_version(peer_hex) == 1
        avatar_mgr._router.send.assert_called_once()


class TestAvatarStorageIsBounded:
    """Nothing requires membership to reach us, and identities are free to
    mint, so a per-sender rate limit bounds one peer rather than the table."""

    def test_a_stranger_avatar_is_not_stored(self, avatar_mgr):
        jpeg = compress_avatar(_make_test_jpeg())
        sender = "ab" * 16

        mock_identity = MagicMock()
        mock_identity.hash = bytes.fromhex(sender)
        lxm = _make_lxm(
            {F_MSG_TYPE: MT_AVATAR_UPDATE, F_AVATAR_DATA: jpeg, F_AVATAR_VERSION: 1}
        )
        lxm.source_hash = bytes.fromhex(sender)
        with patch("trenchchat.core.avatar.RNS.Identity.recall", return_value=mock_identity):
            avatar_mgr._on_lxmf_message(lxm)

        assert avatar_mgr._storage.get_peer_avatar(sender) is None

    def test_the_cache_is_capped(self, avatar_mgr):
        from trenchchat.core.avatar import MAX_CACHED_PEER_AVATARS

        jpeg = compress_avatar(_make_test_jpeg())
        for i in range(MAX_CACHED_PEER_AVATARS + 5):
            avatar_mgr._storage.upsert_peer_avatar(f"{i:032x}", jpeg, 1)
        avatar_mgr._storage.prune_peer_avatars(MAX_CACHED_PEER_AVATARS)

        remaining = avatar_mgr._storage._fetchall(
            "SELECT identity_hash FROM peer_avatars")
        assert len(remaining) == MAX_CACHED_PEER_AVATARS
