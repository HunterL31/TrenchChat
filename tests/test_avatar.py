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
    def test_resizes_to_48x48(self):
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
        """A 200×100 image should produce a 48×48 output (not stretched)."""
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
    def _send_avatar_lxm(self, mgr: AvatarManager, sender_hex: str,
                         avatar_data: bytes, version: int = 1):
        """Simulate delivering an MT_AVATAR_UPDATE to the manager."""
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

    def test_remove_avatar_clears_peer_cache(self, avatar_mgr):
        """An MT_AVATAR_UPDATE with empty avatar_data removes the stored avatar."""
        jpeg = compress_avatar(_make_test_jpeg())
        sender = "ee" * 16

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

    def test_avatar_callback_fires_on_inbound(self, avatar_mgr):
        jpeg = compress_avatar(_make_test_jpeg())
        sender = "44" * 16
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
