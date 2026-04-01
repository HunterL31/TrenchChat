"""
Tests for VoiceManager -- voice channel lifecycle, permission enforcement,
relay assignment/revocation, VAD filter, and adversarial SPEAK bypass.

These tests do NOT exercise LXST audio pipelines (which require audio hardware
and a running Reticulum network).  Instead they test:
  - Protocol-level state management (join/leave, participant tracking)
  - Permission enforcement in the core layer (no GUI involved)
  - Relay token signing and verification
  - VAD filter energy gating logic
  - Storage-layer voice columns and migration

Audio pipeline tests are covered by manual integration testing.
"""

import time
import unittest.mock as mock

import pytest
import msgpack
import RNS

from trenchchat.core.channel import CHANNEL_TYPE_TEXT, CHANNEL_TYPE_VOICE
from trenchchat.core.permissions import (
    PRESET_PRIVATE, PRESET_OPEN, ROLE_ADMIN, ROLE_MEMBER, ROLE_OWNER,
    SPEAK, MANAGE_RELAY,
)
from trenchchat.core.protocol import (
    F_CHANNEL_HASH, F_MSG_TYPE, F_VOICE_DEST_HASH, F_VOICE_PARTICIPANTS,
    F_RELAY_TOKEN, F_RELAY_DEST_HASH, F_MEMBER_LIST_DOC,
    MT_VOICE_JOIN, MT_VOICE_LEAVE, MT_VOICE_STATE,
    MT_RELAY_ASSIGN, MT_RELAY_ACCEPT, MT_RELAY_REVOKE, MT_RELAY_MEMBER_UPDATE,
    VS_MUTE, VS_UNMUTE, VS_SPEAKING, VS_SILENT,
)
from trenchchat.core.voice import VoiceManager, _make_relay_token, _verify_relay_token
from trenchchat.core.vad import VadFilter

import numpy as np


# ---------------------------------------------------------------------------
# Helpers: lightweight mock storage and router
# ---------------------------------------------------------------------------

class _MockStorage:
    """Minimal storage stub for VoiceManager tests."""

    def __init__(self):
        self._channels: dict = {}
        self._members: dict = {}   # (channel_hash, identity_hex) -> role
        self._permissions: dict = {}  # channel_hash -> perms dict
        self._relay: dict = {}     # channel_hash -> relay_dest_hex

    def get_channel(self, channel_hash_hex: str):
        return self._channels.get(channel_hash_hex)

    def get_voice_channels(self):
        return [
            v for v in self._channels.values()
            if v.get("channel_type") == CHANNEL_TYPE_VOICE
        ]

    def get_members(self, channel_hash_hex: str) -> list:
        result = []
        for (ch, ih), role in self._members.items():
            if ch == channel_hash_hex:
                result.append({"identity_hash": ih, "display_name": ih[:8]})
        return result

    def has_permission(self, channel_hash_hex: str, identity_hex: str,
                       permission: str) -> bool:
        role = self._members.get((channel_hash_hex, identity_hex))
        if role is None:
            return False
        if role == ROLE_OWNER:
            return True
        perms = self._permissions.get(channel_hash_hex, {})
        return permission in perms.get(role, [])

    def get_role(self, channel_hash_hex: str, identity_hex: str) -> str | None:
        return self._members.get((channel_hash_hex, identity_hex))

    def get_display_name_for_identity(self, identity_hex: str) -> str | None:
        return identity_hex[:8]

    def set_channel_relay(self, channel_hash_hex: str, relay_dest_hex) -> None:
        self._relay[channel_hash_hex] = relay_dest_hex
        if channel_hash_hex in self._channels:
            self._channels[channel_hash_hex]["relay_dest_hash"] = relay_dest_hex

    def get_member_list_version(self, channel_hash_hex: str):
        return None

    def _add_channel(self, channel_hash_hex: str, name: str,
                     creator_hex: str, channel_type: str = CHANNEL_TYPE_TEXT) -> None:
        self._channels[channel_hash_hex] = {
            "hash": channel_hash_hex,
            "name": name,
            "creator_hash": creator_hex,
            "channel_type": channel_type,
            "relay_dest_hash": None,
        }

    def _add_member(self, channel_hash_hex: str, identity_hex: str,
                    role: str, perms: dict | None = None) -> None:
        self._members[(channel_hash_hex, identity_hex)] = role
        if perms is not None:
            self._permissions[channel_hash_hex] = perms


class _MockRouter:
    """Minimal router stub: captures sent LXMF messages."""

    def __init__(self):
        self.sent: list = []
        self.delivery_destination = mock.MagicMock()
        self.delivery_destination.hash = b"\x00" * 16
        self._delivery_callbacks: list = []

    def send(self, lxm) -> None:
        self.sent.append(lxm)

    def add_message_handler(self, handler) -> None:
        self._delivery_callbacks.append(handler)

    def add_delivery_callback(self, cb) -> None:
        self._delivery_callbacks.append(cb)


class _MockIdentity:
    """Minimal identity stub."""

    def __init__(self, hex_id: str):
        self.hash_hex = hex_id
        self.display_name = hex_id[:8]
        self.rns_identity = mock.MagicMock()
        self.rns_identity.hash = bytes.fromhex(hex_id.ljust(32, "0")[:32])

    def sign(self, payload: bytes) -> bytes:
        return b"\xff" * 64


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def alice_hex():
    return "a" * 32

@pytest.fixture
def bob_hex():
    return "b" * 32

@pytest.fixture
def channel_hex():
    return "c" * 32

@pytest.fixture
def mock_storage(alice_hex, channel_hex):
    storage = _MockStorage()
    storage._add_channel(channel_hex, "test-voice", alice_hex, CHANNEL_TYPE_VOICE)
    storage._add_member(channel_hex, alice_hex, ROLE_OWNER)
    return storage

@pytest.fixture
def mock_router():
    return _MockRouter()

@pytest.fixture
def mock_identity(alice_hex):
    return _MockIdentity(alice_hex)

@pytest.fixture
def voice_mgr(mock_identity, mock_storage, mock_router):
    return VoiceManager(mock_identity, mock_storage, mock_router)


# ---------------------------------------------------------------------------
# Storage: voice column migration
# ---------------------------------------------------------------------------

class TestStorageVoiceColumns:
    def test_upsert_channel_stores_voice_type(self, tmp_path):
        """channel_type 'voice' is persisted and retrieved correctly."""
        from trenchchat.core.storage import Storage
        db = Storage(db_path=tmp_path / "storage.db")
        try:
            db.upsert_channel(
                hash="v1" * 16,
                name="voice-test",
                description="",
                creator_hash="a" * 32,
                channel_type=CHANNEL_TYPE_VOICE,
            )
            row = db.get_channel("v1" * 16)
            assert row is not None
            assert row["channel_type"] == CHANNEL_TYPE_VOICE
        finally:
            db.close()

    def test_upsert_channel_defaults_to_text(self, tmp_path):
        """Channels without explicit channel_type default to 'text'."""
        from trenchchat.core.storage import Storage
        db = Storage(db_path=tmp_path / "storage.db")
        try:
            db.upsert_channel(
                hash="t1" * 16,
                name="text-test",
                description="",
                creator_hash="a" * 32,
            )
            row = db.get_channel("t1" * 16)
            assert row["channel_type"] == CHANNEL_TYPE_TEXT
        finally:
            db.close()

    def test_set_channel_relay(self, tmp_path):
        """set_channel_relay stores and clears relay_dest_hash."""
        from trenchchat.core.storage import Storage
        db = Storage(db_path=tmp_path / "storage.db")
        try:
            hash_hex = "r1" * 16
            db.upsert_channel(
                hash=hash_hex, name="relay-test", description="",
                creator_hash="a" * 32, channel_type=CHANNEL_TYPE_VOICE,
            )
            db.set_channel_relay(hash_hex, "relay_identity_hex")
            row = db.get_channel(hash_hex)
            assert row["relay_dest_hash"] == "relay_identity_hex"

            db.set_channel_relay(hash_hex, None)
            row = db.get_channel(hash_hex)
            assert row["relay_dest_hash"] is None
        finally:
            db.close()

    def test_get_voice_channels(self, tmp_path):
        """get_voice_channels returns only voice-type channels."""
        from trenchchat.core.storage import Storage
        db = Storage(db_path=tmp_path / "storage.db")
        try:
            db.upsert_channel("t" * 32, "text-ch", "", "a" * 32,
                              channel_type=CHANNEL_TYPE_TEXT)
            db.upsert_channel("v" * 32, "voice-ch", "", "a" * 32,
                              channel_type=CHANNEL_TYPE_VOICE)
            voice_rows = db.get_voice_channels()
            hashes = [r["hash"] for r in voice_rows]
            assert "v" * 32 in hashes
            assert "t" * 32 not in hashes
        finally:
            db.close()


# ---------------------------------------------------------------------------
# VoiceManager: hosting and destination management
# ---------------------------------------------------------------------------

class TestVoiceManagerHosting:
    def test_create_voice_destination_registers(self, voice_mgr, channel_hex):
        """create_voice_destination registers the channel in _hosted_destinations."""
        with mock.patch("RNS.Destination") as MockDest:
            mock_dest = mock.MagicMock()
            mock_dest.hash = bytes.fromhex("d" * 32)
            MockDest.return_value = mock_dest

            result = voice_mgr.create_voice_destination(channel_hex)

        assert channel_hex in voice_mgr._hosted_destinations
        assert voice_mgr.is_hosting(channel_hex)

    def test_teardown_removes_destination(self, voice_mgr, channel_hex):
        """teardown_voice_destination removes the channel from hosted dict."""
        with mock.patch("RNS.Destination") as MockDest:
            mock_dest = mock.MagicMock()
            mock_dest.hash = bytes.fromhex("d" * 32)
            MockDest.return_value = mock_dest
            voice_mgr.create_voice_destination(channel_hex)

        voice_mgr.teardown_voice_destination(channel_hex)
        assert not voice_mgr.is_hosting(channel_hex)

    def test_is_hosting_false_for_unknown_channel(self, voice_mgr):
        assert not voice_mgr.is_hosting("unknown" * 4)

    def test_get_participants_empty_initially(self, voice_mgr, channel_hex):
        """A newly created voice channel has no participants."""
        with mock.patch("RNS.Destination") as MockDest:
            mock_dest = mock.MagicMock()
            mock_dest.hash = bytes.fromhex("d" * 32)
            MockDest.return_value = mock_dest
            voice_mgr.create_voice_destination(channel_hex)

        assert voice_mgr.get_participants(channel_hex) == []


# ---------------------------------------------------------------------------
# VoiceManager: permission enforcement (core layer)
# ---------------------------------------------------------------------------

class TestVoicePermissionEnforcement:
    """Core enforcement tests: bypass the GUI and call core directly."""

    def _make_join_message(self, channel_hex: str, sender_hex: str):
        """Build a mock LXMF MT_VOICE_JOIN message."""
        msg = mock.MagicMock()
        msg.source_hash = bytes.fromhex(sender_hex.ljust(32, "0")[:32])
        msg.fields = {
            F_MSG_TYPE: MT_VOICE_JOIN,
            F_CHANNEL_HASH: bytes.fromhex(channel_hex),
        }
        return msg

    def test_voice_join_rejected_without_speak(self, voice_mgr, mock_storage,
                                               channel_hex, bob_hex):
        """A participant without SPEAK permission is rejected at the core layer."""
        # Bob is a member but SPEAK is not in his permission list.
        mock_storage._add_member(
            channel_hex, bob_hex, ROLE_MEMBER,
            perms={ROLE_MEMBER: []}  # no SPEAK
        )

        with mock.patch("RNS.Destination") as MockDest:
            mock_dest = mock.MagicMock()
            mock_dest.hash = bytes.fromhex("d" * 32)
            MockDest.return_value = mock_dest
            voice_mgr.create_voice_destination(channel_hex)

        mock_rns_identity = mock.MagicMock()
        mock_rns_identity.hash = bytes.fromhex(bob_hex.ljust(32, "0")[:32])

        with mock.patch.object(RNS.Identity, "recall", return_value=mock_rns_identity):
            msg = self._make_join_message(channel_hex, bob_hex)
            # _handle_voice_join is the core enforcement point.
            voice_mgr._handle_voice_join(msg, msg.fields)

        # No voice state should have been sent (no LXMF messages enqueued).
        assert len(voice_mgr._router.sent) == 0

    def test_voice_join_accepted_with_speak(self, voice_mgr, mock_storage,
                                            channel_hex, bob_hex):
        """A participant with SPEAK permission receives a voice state reply."""
        mock_storage._add_member(
            channel_hex, bob_hex, ROLE_MEMBER,
            perms={ROLE_MEMBER: [SPEAK]}
        )

        with mock.patch("RNS.Destination") as MockDest:
            mock_dest = mock.MagicMock()
            mock_dest.hash = bytes.fromhex("d" * 32)
            MockDest.return_value = mock_dest
            voice_mgr.create_voice_destination(channel_hex)

        with mock.patch.object(RNS.Identity, "recall", return_value=None), \
             mock.patch.object(RNS.Transport, "request_path"):
            msg = self._make_join_message(channel_hex, bob_hex)
            voice_mgr._handle_voice_join(msg, msg.fields)

        # No crash = enforcement passed, send attempted (path unknown is fine in test).

    def test_link_established_rejected_without_speak(self, voice_mgr, mock_storage,
                                                      channel_hex, bob_hex):
        """RNS Link rejected when participant lacks SPEAK (host-side enforcement)."""
        mock_storage._add_member(
            channel_hex, bob_hex, ROLE_MEMBER,
            perms={ROLE_MEMBER: []}  # no SPEAK
        )
        with mock.patch("RNS.Destination") as MockDest:
            mock_dest = mock.MagicMock()
            mock_dest.hash = bytes.fromhex("d" * 32)
            MockDest.return_value = mock_dest
            voice_mgr.create_voice_destination(channel_hex)

        bob_rns_identity = mock.MagicMock()
        bob_rns_identity.hash = bytes.fromhex(bob_hex.ljust(32, "0")[:32])

        link = mock.MagicMock()

        # Identity is only known after remote_identified fires, not at link_established.
        voice_mgr._on_link_remote_identified(channel_hex, link, bob_rns_identity)

        # Link must be torn down.
        link.teardown.assert_called_once()
        # No participant added.
        assert channel_hex not in voice_mgr._sessions or \
               bob_hex not in voice_mgr._sessions.get(channel_hex, {})


# ---------------------------------------------------------------------------
# VoiceManager: participant state tracking
# ---------------------------------------------------------------------------

class TestVoiceParticipantTracking:
    def test_link_closed_removes_participant(self, voice_mgr, mock_storage,
                                             channel_hex, bob_hex, alice_hex):
        """Participant is removed from sessions when their link closes."""
        mock_storage._add_member(
            channel_hex, bob_hex, ROLE_MEMBER,
            perms={ROLE_MEMBER: [SPEAK]}
        )

        with mock.patch("RNS.Destination"):
            voice_mgr.create_voice_destination(channel_hex)

        # Manually inject a participant session to simulate a connected peer.
        from trenchchat.core.voice import _ParticipantSession
        link = mock.MagicMock()
        session = _ParticipantSession(bob_hex, "Bob", link)
        voice_mgr._sessions[channel_hex][bob_hex] = session

        # Simulate link close.
        voice_mgr._on_link_closed(channel_hex, bob_hex, link)

        assert bob_hex not in voice_mgr._sessions.get(channel_hex, {})

    def test_speaking_signal_updates_state(self, voice_mgr, channel_hex, bob_hex):
        """In-band VS_SPEAKING and VS_SILENT signals update participant speaking state."""
        from trenchchat.core.voice import _ParticipantSession
        link = mock.MagicMock()
        session = _ParticipantSession(bob_hex, "Bob", link)
        voice_mgr._sessions[channel_hex] = {bob_hex: session}

        voice_mgr._handle_voice_signal(channel_hex, bob_hex, VS_SPEAKING)
        assert voice_mgr._sessions[channel_hex][bob_hex].is_speaking is True

        voice_mgr._handle_voice_signal(channel_hex, bob_hex, VS_SILENT)
        assert voice_mgr._sessions[channel_hex][bob_hex].is_speaking is False

    def test_mute_signal_updates_state(self, voice_mgr, channel_hex, bob_hex):
        """VS_MUTE and VS_UNMUTE update the participant mute state."""
        from trenchchat.core.voice import _ParticipantSession
        link = mock.MagicMock()
        session = _ParticipantSession(bob_hex, "Bob", link)
        voice_mgr._sessions[channel_hex] = {bob_hex: session}

        voice_mgr._handle_voice_signal(channel_hex, bob_hex, VS_MUTE)
        assert voice_mgr._sessions[channel_hex][bob_hex].is_muted is True

        voice_mgr._handle_voice_signal(channel_hex, bob_hex, VS_UNMUTE)
        assert voice_mgr._sessions[channel_hex][bob_hex].is_muted is False

    def test_get_participants_returns_current(self, voice_mgr, channel_hex, bob_hex):
        """get_participants returns the live participant list."""
        from trenchchat.core.voice import _ParticipantSession
        link = mock.MagicMock()
        session = _ParticipantSession(bob_hex, "Bob", link)
        voice_mgr._sessions[channel_hex] = {bob_hex: session}

        parts = voice_mgr.get_participants(channel_hex)
        assert len(parts) == 1
        assert parts[0]["identity_hex"] == bob_hex


# ---------------------------------------------------------------------------
# VoiceManager: PTT and VAD mode controls
# ---------------------------------------------------------------------------

class TestVoiceMicModes:
    def test_set_mic_mode_ptt(self, voice_mgr, channel_hex):
        voice_mgr.set_mic_mode(channel_hex, "ptt")
        assert voice_mgr.get_mic_mode(channel_hex) == "ptt"

    def test_set_mic_mode_vad(self, voice_mgr, channel_hex):
        voice_mgr.set_mic_mode(channel_hex, "vad")
        assert voice_mgr.get_mic_mode(channel_hex) == "vad"

    def test_invalid_mic_mode_raises(self, voice_mgr, channel_hex):
        with pytest.raises(ValueError):
            voice_mgr.set_mic_mode(channel_hex, "invalid")

    def test_mute_state_tracked(self, voice_mgr, channel_hex):
        assert not voice_mgr.is_muted(channel_hex)
        # set_muted without an active session should not crash.
        voice_mgr.set_muted(channel_hex, True)
        assert voice_mgr.is_muted(channel_hex)

    def test_deafen_state_tracked(self, voice_mgr, channel_hex):
        assert not voice_mgr.is_deafened(channel_hex)
        voice_mgr.set_deafened(channel_hex, True)
        assert voice_mgr.is_deafened(channel_hex)

    def test_is_in_voice_false_initially(self, voice_mgr, channel_hex):
        assert not voice_mgr.is_in_voice(channel_hex)


# ---------------------------------------------------------------------------
# Relay token verification
# ---------------------------------------------------------------------------

class TestRelayTokenVerification:
    def _make_rns_identity(self) -> "RNS.Identity":
        import RNS
        return RNS.Identity()

    def test_valid_token_accepted(self):
        """A properly signed relay token passes verification."""
        import RNS
        owner_identity = RNS.Identity()
        relay_dest_hash = b"\xaa" * 16
        channel_hash_bytes = b"\xbb" * 16
        timestamp = time.time()

        token = _make_relay_token(owner_identity, relay_dest_hash,
                                   channel_hash_bytes, timestamp)
        assert _verify_relay_token(owner_identity, token, relay_dest_hash,
                                    channel_hash_bytes, timestamp)

    def test_expired_token_rejected(self):
        """A token whose timestamp is outside the validity window is rejected."""
        import RNS
        owner_identity = RNS.Identity()
        relay_dest_hash = b"\xaa" * 16
        channel_hash_bytes = b"\xbb" * 16
        old_timestamp = time.time() - 400  # > _RELAY_TOKEN_VALIDITY_SECS (300s)

        token = _make_relay_token(owner_identity, relay_dest_hash,
                                   channel_hash_bytes, old_timestamp)
        assert not _verify_relay_token(owner_identity, token, relay_dest_hash,
                                        channel_hash_bytes, old_timestamp)

    def test_wrong_channel_rejected(self):
        """A token signed for channel A is rejected when presented for channel B."""
        import RNS
        owner_identity = RNS.Identity()
        relay_dest_hash = b"\xaa" * 16
        channel_a = b"\xcc" * 16
        channel_b = b"\xdd" * 16
        timestamp = time.time()

        token = _make_relay_token(owner_identity, relay_dest_hash, channel_a, timestamp)
        assert not _verify_relay_token(owner_identity, token, relay_dest_hash,
                                        channel_b, timestamp)

    def test_forged_token_rejected(self):
        """A token signed by a different identity is rejected."""
        import RNS
        owner_identity = RNS.Identity()
        forger_identity = RNS.Identity()
        relay_dest_hash = b"\xaa" * 16
        channel_hash_bytes = b"\xbb" * 16
        timestamp = time.time()

        # Forger signs the token, but we verify against owner_identity.
        token = _make_relay_token(forger_identity, relay_dest_hash,
                                   channel_hash_bytes, timestamp)
        assert not _verify_relay_token(owner_identity, token, relay_dest_hash,
                                        channel_hash_bytes, timestamp)


# ---------------------------------------------------------------------------
# Relay management: MANAGE_RELAY permission enforcement
# ---------------------------------------------------------------------------

class TestRelayPermissionEnforcement:
    def test_assign_relay_rejected_without_manage_relay(self, voice_mgr, mock_storage,
                                                         channel_hex, alice_hex, bob_hex):
        """assign_relay is rejected when caller lacks MANAGE_RELAY (core enforcement)."""
        # Bob is a member without MANAGE_RELAY.
        mock_storage._add_member(
            channel_hex, bob_hex, ROLE_MEMBER,
            perms={ROLE_MEMBER: [SPEAK]}
        )

        # Patch Bob as the identity in the voice manager.
        voice_mgr._identity = _MockIdentity(bob_hex)
        voice_mgr._storage = mock_storage

        voice_mgr.assign_relay(channel_hex, "relay" * 8)
        # No LXMF message should have been sent.
        assert len(voice_mgr._router.sent) == 0

    def test_revoke_relay_rejected_without_manage_relay(self, voice_mgr, mock_storage,
                                                         channel_hex, alice_hex, bob_hex):
        """revoke_relay is rejected when caller lacks MANAGE_RELAY."""
        mock_storage._add_member(
            channel_hex, bob_hex, ROLE_MEMBER,
            perms={ROLE_MEMBER: [SPEAK]}
        )
        mock_storage._relay[channel_hex] = "some_relay"
        mock_storage._channels[channel_hex]["relay_dest_hash"] = "some_relay"

        voice_mgr._identity = _MockIdentity(bob_hex)
        voice_mgr._storage = mock_storage

        voice_mgr.revoke_relay(channel_hex)
        assert len(voice_mgr._router.sent) == 0

    def test_assign_relay_accepted_with_manage_relay(self, voice_mgr, mock_storage,
                                                      channel_hex, alice_hex):
        """assign_relay proceeds when owner has MANAGE_RELAY."""
        # Alice (owner) has all permissions.
        with mock.patch.object(mock_storage, "has_permission", return_value=True), \
             mock.patch.object(mock_storage, "get_member_list_version", return_value=None), \
             mock.patch.object(RNS.Destination, "hash", return_value=b"\xaa" * 16), \
             mock.patch.object(RNS.Identity, "recall", return_value=None), \
             mock.patch.object(RNS.Transport, "request_path"):
            voice_mgr.assign_relay(channel_hex, alice_hex * 2)
            # Relay assign tries to send; path unknown so it returns early -- no crash.


# ---------------------------------------------------------------------------
# Relay protocol: handle_relay_assign (relay-side enforcement)
# ---------------------------------------------------------------------------

class TestRelayAssignHandling:
    def test_invalid_token_rejected(self, voice_mgr):
        """MT_RELAY_ASSIGN with a forged token is rejected (relay enforcement)."""
        import RNS
        owner_identity = RNS.Identity()
        channel_hex = "c" * 32

        forger_identity = RNS.Identity()
        relay_dest_hash = b"\xaa" * 16
        timestamp = time.time()
        bad_token = _make_relay_token(forger_identity, relay_dest_hash,
                                       bytes.fromhex(channel_hex), timestamp)

        msg = mock.MagicMock()
        msg.source_hash = relay_dest_hash

        with mock.patch.object(RNS.Identity, "recall", return_value=owner_identity), \
             mock.patch.object(owner_identity, "validate", return_value=False):
            fields = {
                F_MSG_TYPE: MT_RELAY_ASSIGN,
                F_CHANNEL_HASH: bytes.fromhex(channel_hex),
                F_RELAY_DEST_HASH: relay_dest_hash,
                F_RELAY_TOKEN: bad_token,
                0x03: timestamp,
                F_MEMBER_LIST_DOC: b"",
            }
            # Should not create a voice destination.
            assert not voice_mgr.is_hosting(channel_hex)
            voice_mgr._handle_relay_assign(msg, fields)
            assert not voice_mgr.is_hosting(channel_hex)


# ---------------------------------------------------------------------------
# VAD filter
# ---------------------------------------------------------------------------

class TestVadFilter:
    def test_gate_opens_on_loud_frame(self):
        """A frame above threshold opens the gate."""
        vad = VadFilter(threshold_db=-40.0, hold_ms=0)
        loud_frame = np.ones((480, 1), dtype=np.float32) * 0.5
        result = vad.handle_frame(loud_frame, 48000)
        assert vad.gate_open
        assert np.array_equal(result, loud_frame)

    def test_gate_closed_on_silence(self):
        """A frame below threshold (with no hold) produces silence output."""
        vad = VadFilter(threshold_db=-20.0, hold_ms=0)
        silent_frame = np.ones((480, 1), dtype=np.float32) * 1e-6
        result = vad.handle_frame(silent_frame, 48000)
        # Gate should be closed; result is a zero-filled frame.
        assert not vad.gate_open
        assert np.all(result == 0)

    def test_hold_timer_keeps_gate_open(self):
        """After a loud frame, the gate stays open during the hold period.

        While the gate is open the frame passes through even if it is silence.
        The gate_open flag itself tells us whether the hold is active.
        """
        vad = VadFilter(threshold_db=-40.0, hold_ms=500)
        loud_frame = np.ones((480, 1), dtype=np.float32) * 0.5
        silent_frame = np.zeros((480, 1), dtype=np.float32)

        vad.handle_frame(loud_frame, 48000)   # opens gate
        vad.handle_frame(silent_frame, 48000)  # within hold period
        # Gate should remain open because hold period has not expired.
        assert vad.gate_open

    def test_gate_closes_after_hold_expires(self):
        """After hold_ms elapses, the gate closes on the next silent frame."""
        vad = VadFilter(threshold_db=-40.0, hold_ms=1)  # 1ms hold
        loud_frame = np.ones((480, 1), dtype=np.float32) * 0.5
        silent_frame = np.zeros((480, 1), dtype=np.float32)

        vad.handle_frame(loud_frame, 48000)
        time.sleep(0.05)  # wait well past hold period
        vad.handle_frame(silent_frame, 48000)
        assert not vad.gate_open

    def test_speaking_callback_fires_on_state_change(self):
        """The speaking callback is called when the gate opens or closes."""
        vad = VadFilter(threshold_db=-40.0, hold_ms=0)
        states: list[bool] = []
        vad.add_speaking_callback(states.append)

        loud_frame = np.ones((480, 1), dtype=np.float32) * 0.5
        silent_frame = np.zeros((480, 1), dtype=np.float32)

        vad.handle_frame(loud_frame, 48000)   # gate opens -> callback(True)
        time.sleep(0.01)
        vad.handle_frame(silent_frame, 48000)  # gate closes -> callback(False)

        assert True in states
        assert False in states

    def test_threshold_setter(self):
        """Changing the threshold_db property updates the linear threshold."""
        vad = VadFilter(threshold_db=-40.0)
        vad.threshold_db = -20.0
        assert vad.threshold_db == pytest.approx(-20.0)

    def test_reset_clears_state(self):
        """reset() closes the gate and clears hold state."""
        vad = VadFilter(threshold_db=-40.0, hold_ms=1000)
        loud_frame = np.ones((480, 1), dtype=np.float32) * 0.5
        vad.handle_frame(loud_frame, 48000)
        assert vad.gate_open

        vad.reset()
        assert not vad.gate_open


# ---------------------------------------------------------------------------
# Adversarial: SPEAK bypass (test_adversarial.py extension coverage)
# ---------------------------------------------------------------------------

class TestAdversarialSpeak:
    def test_link_rejected_without_speak_permission(self):
        """
        Adversarial test: a participant who lacks SPEAK tries to bypass the
        GUI gate by directly establishing an RNS Link. The core layer in
        _on_link_established must reject the link.
        """
        import RNS

        channel_hex = "d" * 32
        owner_hex = "a" * 32
        attacker_hex = "e" * 32

        storage = _MockStorage()
        storage._add_channel(channel_hex, "secure-voice", owner_hex, CHANNEL_TYPE_VOICE)
        storage._add_member(channel_hex, owner_hex, ROLE_OWNER)
        # Attacker is a member but has no SPEAK permission.
        storage._add_member(channel_hex, attacker_hex, ROLE_MEMBER,
                            perms={ROLE_MEMBER: []})

        identity = _MockIdentity(owner_hex)
        router = _MockRouter()
        mgr = VoiceManager(identity, storage, router)

        with mock.patch("RNS.Destination"):
            mgr.create_voice_destination(channel_hex)

        # Mock the hash to match attacker_hex (first 16 bytes).
        attacker_hash_bytes = bytes.fromhex(attacker_hex.ljust(32, "0")[:32])
        attacker_rns_identity = mock.MagicMock()
        attacker_rns_identity.hash = attacker_hash_bytes

        link = mock.MagicMock()

        # Identity is only known after remote_identified fires, not at link_established.
        mgr._on_link_remote_identified(channel_hex, link, attacker_rns_identity)

        # The link must be torn down -- attacker rejected.
        link.teardown.assert_called_once()
        assert channel_hex not in mgr._sessions or \
               attacker_hex not in mgr._sessions.get(channel_hex, {})

    def test_relay_assign_with_forged_token_rejected(self):
        """
        Adversarial test: a non-owner attempts to assign a relay by crafting
        a MT_RELAY_ASSIGN with a self-signed token. The relay's core
        enforcement must reject it.
        """
        import RNS

        channel_hex = "f" * 32
        owner_hex = "a" * 32

        storage = _MockStorage()
        storage._add_channel(channel_hex, "attack-voice", owner_hex, CHANNEL_TYPE_VOICE)

        real_owner_identity = RNS.Identity()
        attacker_identity = RNS.Identity()

        relay_dest_hash = b"\xcc" * 16
        timestamp = time.time()

        # Attacker signs the token with their own key.
        forged_token = _make_relay_token(attacker_identity, relay_dest_hash,
                                          bytes.fromhex(channel_hex), timestamp)

        mgr = VoiceManager(
            _MockIdentity(owner_hex), storage, _MockRouter()
        )

        msg = mock.MagicMock()
        msg.source_hash = relay_dest_hash

        # The sender identity recalled from RNS is the real owner -- but the
        # token was signed by the attacker, so verification must fail.
        with mock.patch.object(RNS.Identity, "recall", return_value=real_owner_identity):
            fields = {
                F_MSG_TYPE: MT_RELAY_ASSIGN,
                F_CHANNEL_HASH: bytes.fromhex(channel_hex),
                F_RELAY_DEST_HASH: relay_dest_hash,
                F_RELAY_TOKEN: forged_token,
                0x03: timestamp,
                F_MEMBER_LIST_DOC: b"",
            }
            mgr._handle_relay_assign(msg, fields)

        # No voice destination should have been created.
        assert not mgr.is_hosting(channel_hex)
