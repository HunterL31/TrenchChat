"""
Voice signalling and roster tests.

These ride the LXMF plane through the standard TestTransport shim; the
frame plane (links, streaming) is covered in test_voice_transport.py.
"""

import time
from unittest.mock import MagicMock, patch

import LXMF
import RNS
import pytest

from tests.helpers import (
    wait_for, wait_for_roster, wait_for_subscriber,
)
from trenchchat.core import actions
from trenchchat.core.voice import MAX_VOICE_PARTICIPANTS, VOICE_SIGNAL_MAX_AGE_SECS
from trenchchat.network.voice_transport import PEER_STREAMING
from trenchchat.core.permissions import (
    PRESET_OPEN, PRESET_PRIVATE, ROLE_MEMBER, ROLE_OWNER, SEND_MESSAGE,
)
from trenchchat.core.protocol import (
    F_CHANNEL_HASH, F_MSG_TYPE, F_TIMESTAMP,
    F_VOICE_JOINED_AT, F_VOICE_MUTED,
    MT_VOICE_JOIN,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup_invite_channel(peer_factory, *, member_perms=None):
    """Alice (owner) and Bob (member) on a shared invite-only channel."""
    alice = peer_factory("alice")
    bob = peer_factory("bob")

    perms = dict(PRESET_PRIVATE)
    if member_perms is not None:
        perms[ROLE_MEMBER] = list(member_perms)

    ch_hash = alice.channel_mgr.create_channel("voice-ch", "", permissions=perms)
    alice.storage.upsert_member(ch_hash, bob.identity.hash_hex, "Bob",
                                role=ROLE_MEMBER)

    _mirror_membership(bob, alice, ch_hash, perms)
    return alice, bob, ch_hash


def _mirror_membership(peer, owner, ch_hash, perms):
    """Give a peer the local channel/member rows its receiver checks against."""
    peer.storage.upsert_channel(ch_hash, "voice-ch", "",
                                owner.identity.hash_hex, perms, time.time())
    peer.storage.subscribe(ch_hash)
    peer.storage.upsert_member(ch_hash, peer.identity.hash_hex,
                               peer.name.capitalize(), role=ROLE_MEMBER)
    peer.storage.upsert_member(ch_hash, owner.identity.hash_hex, "Alice",
                               role=ROLE_OWNER)
    peer.storage.set_channel_permissions(ch_hash, perms)


def _setup_open_channel(peer_factory, names=("alice", "bob")):
    """An open-join channel every named peer is subscribed to."""
    peers = [peer_factory(name) for name in names]
    owner = peers[0]
    perms = dict(PRESET_OPEN)
    ch_hash = owner.channel_mgr.create_channel("open-voice", "",
                                               permissions=perms)
    for peer in peers[1:]:
        peer.storage.upsert_channel(ch_hash, "open-voice", "",
                                    owner.identity.hash_hex, perms, time.time())
        peer.subscription_mgr.subscribe(ch_hash, owner.identity.hash_hex)
    owner.storage.subscribe(ch_hash)
    for peer in peers[1:]:
        assert wait_for_subscriber(peer, ch_hash, owner.identity.hash_hex)
    return peers, ch_hash


def _craft_voice_message(sender, recipient, fields):
    """Build and send a voice control message exactly as a client would."""
    delivery_hash = RNS.Destination.hash(
        bytes.fromhex(recipient.identity.hash_hex), "lxmf", "delivery")
    dest_identity = RNS.Identity.recall(delivery_hash)
    dest = RNS.Destination(
        dest_identity, RNS.Destination.OUT, RNS.Destination.SINGLE,
        "lxmf", "delivery",
    )
    lxm = LXMF.LXMessage(dest, sender.router.delivery_destination, "",
                         desired_method=LXMF.LXMessage.DIRECT)
    lxm.fields = fields
    sender.router.send(lxm)
    return lxm


# ---------------------------------------------------------------------------
# Signalling / roster
# ---------------------------------------------------------------------------

class TestVoiceSignalling:
    def test_join_broadcasts_to_channel_recipients(self, peer_factory):
        alice, bob, ch_hash = _setup_invite_channel(peer_factory)
        assert bob.voice_mgr.join_voice(ch_hash) is True
        assert wait_for_roster(alice, ch_hash, bob.identity.hash_hex)
        entry = next(e for e in alice.voice_mgr.get_roster(ch_hash)
                     if e["identity_hash"] == bob.identity.hash_hex)
        assert entry["muted"] is False

    def test_join_reply_reveals_current_occupant(self, peer_factory):
        """A joiner learns existing occupants from their state replies, not
        from having witnessed the original join."""
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        perms = dict(PRESET_PRIVATE)
        ch_hash = alice.channel_mgr.create_channel("voice-ch", "",
                                                   permissions=perms)
        alice.storage.upsert_member(ch_hash, bob.identity.hash_hex, "Bob",
                                    role=ROLE_MEMBER)

        # Alice joins while Bob has no local record of the channel at all,
        # so her join broadcast is dropped on his side.
        assert alice.voice_mgr.join_voice(ch_hash) is True
        time.sleep(0.3)
        assert bob.voice_mgr.get_roster(ch_hash) == []

        _mirror_membership(bob, alice, ch_hash, perms)
        assert bob.voice_mgr.join_voice(ch_hash) is True
        assert wait_for_roster(bob, ch_hash, alice.identity.hash_hex)

    def test_leave_removes_from_roster(self, peer_factory):
        alice, bob, ch_hash = _setup_invite_channel(peer_factory)
        bob.voice_mgr.join_voice(ch_hash)
        assert wait_for_roster(alice, ch_hash, bob.identity.hash_hex)

        bob.voice_mgr.leave_voice()
        assert wait_for(
            lambda: all(e["identity_hash"] != bob.identity.hash_hex
                        for e in alice.voice_mgr.get_roster(ch_hash)),
            msg="bob removed from alice's roster",
        )
        assert bob.voice_mgr.current_channel is None

    def test_mute_state_propagates(self, peer_factory):
        alice, bob, ch_hash = _setup_invite_channel(peer_factory)
        bob.voice_mgr.join_voice(ch_hash)
        assert wait_for_roster(alice, ch_hash, bob.identity.hash_hex)

        bob.voice_mgr.set_muted(True)
        assert bob.voice_mgr.is_muted is True
        assert wait_for(
            lambda: any(e["identity_hash"] == bob.identity.hash_hex
                        and e["muted"]
                        for e in alice.voice_mgr.get_roster(ch_hash)),
            msg="bob's mute state on alice's roster",
        )

    def test_roster_entry_expires_without_refresh(self, peer_factory):
        """A one-shot join with no follow-up state refresh ages out."""
        alice, bob, ch_hash = _setup_invite_channel(peer_factory)
        now = time.time()
        _craft_voice_message(bob, alice, {
            F_MSG_TYPE: MT_VOICE_JOIN,
            F_CHANNEL_HASH: bytes.fromhex(ch_hash),
            F_TIMESTAMP: now,
            F_VOICE_MUTED: False,
            F_VOICE_JOINED_AT: now,
        })
        assert wait_for_roster(alice, ch_hash, bob.identity.hash_hex)
        # Test peers use roster_ttl_secs=2.0; no state refresh arrives
        # because Bob never actually joined a session.
        assert wait_for(
            lambda: alice.voice_mgr.get_roster(ch_hash) == [],
            timeout=10.0,
            msg="stale roster entry pruned",
        )

    def test_stale_voice_signalling_dropped(self, peer_factory):
        alice, bob, ch_hash = _setup_invite_channel(peer_factory)
        now = time.time()
        _craft_voice_message(bob, alice, {
            F_MSG_TYPE: MT_VOICE_JOIN,
            F_CHANNEL_HASH: bytes.fromhex(ch_hash),
            F_TIMESTAMP: now - 3600,
            F_VOICE_MUTED: False,
            F_VOICE_JOINED_AT: now - 3600,
        })
        time.sleep(0.3)
        assert alice.voice_mgr.get_roster(ch_hash) == []


class TestStaleSignalDiagnostics:
    """Bug 79: an age-dropped voice signal must be diagnosable -- a clock-drift
    drop looks identical to packet loss otherwise."""

    def _deliver(self, mgr, sender_hex, fields):
        lxm = MagicMock()
        lxm.fields = fields
        lxm.source_hash = bytes.fromhex(sender_hex)
        ident = MagicMock()
        ident.hash = bytes.fromhex(sender_hex)
        with patch("trenchchat.core.voice.RNS.Identity.recall", return_value=ident), \
                patch("trenchchat.core.voice.RNS.log") as log:
            mgr._on_lxmf_message(lxm)
        return log

    def test_past_skew_logs_clock_skew_warning(self, peer_factory):
        alice, bob, ch_hash = _setup_invite_channel(peer_factory)
        now = time.time()
        log = self._deliver(alice.voice_mgr, bob.identity.hash_hex, {
            F_MSG_TYPE: MT_VOICE_JOIN,
            F_CHANNEL_HASH: bytes.fromhex(ch_hash),
            F_TIMESTAMP: now - VOICE_SIGNAL_MAX_AGE_SECS - 60,
            F_VOICE_MUTED: False,
        })
        messages = " ".join(str(c.args[0]) for c in log.call_args_list)
        assert "clock skew" in messages and "past" in messages

    def test_future_skew_logs_direction(self, peer_factory):
        alice, bob, ch_hash = _setup_invite_channel(peer_factory)
        now = time.time()
        log = self._deliver(alice.voice_mgr, bob.identity.hash_hex, {
            F_MSG_TYPE: MT_VOICE_JOIN,
            F_CHANNEL_HASH: bytes.fromhex(ch_hash),
            F_TIMESTAMP: now + VOICE_SIGNAL_MAX_AGE_SECS + 60,
            F_VOICE_MUTED: False,
        })
        messages = " ".join(str(c.args[0]) for c in log.call_args_list)
        assert "clock skew" in messages and "future" in messages

    def test_missing_timestamp_logged_distinctly(self, peer_factory):
        alice, bob, ch_hash = _setup_invite_channel(peer_factory)
        log = self._deliver(alice.voice_mgr, bob.identity.hash_hex, {
            F_MSG_TYPE: MT_VOICE_JOIN,
            F_CHANNEL_HASH: bytes.fromhex(ch_hash),
            F_VOICE_MUTED: False,
        })
        messages = " ".join(str(c.args[0]) for c in log.call_args_list)
        assert "missing or invalid timestamp" in messages


class TestVoiceJoinGuards:
    def test_join_denied_without_permission(self, peer_factory):
        alice, bob, ch_hash = _setup_invite_channel(
            peer_factory, member_perms=[SEND_MESSAGE])  # no voice_chat

        assert actions.join_voice_channel(
            bob.storage, bob.voice_mgr, ch_hash, bob.identity.hash_hex,
        ) is False
        assert bob.voice_mgr.join_voice(ch_hash) is False
        time.sleep(0.3)
        assert alice.voice_mgr.get_roster(ch_hash) == []

    def test_join_unknown_channel_denied(self, peer_factory):
        bob = peer_factory("bob")
        assert bob.voice_mgr.join_voice("ab" * 16) is False

    def test_join_open_channel_allowed_without_member_row(self, peer_factory):
        (alice, bob), ch_hash = _setup_open_channel(peer_factory)
        assert bob.voice_mgr.join_voice(ch_hash) is True
        assert wait_for_roster(alice, ch_hash, bob.identity.hash_hex)

    def test_second_join_while_active_returns_false(self, peer_factory):
        alice, bob, ch_hash = _setup_invite_channel(peer_factory)
        other = alice.channel_mgr.create_channel(
            "other", "", permissions=dict(PRESET_OPEN))
        alice.storage.subscribe(other)

        assert alice.voice_mgr.join_voice(ch_hash) is True
        assert alice.voice_mgr.join_voice(other) is False
        assert alice.voice_mgr.current_channel == ch_hash

    def test_participant_cap_enforced_at_the_link_layer(self, peer_factory, monkeypatch):
        """Occupancy is counted where it is real -- the inbound link handshake.

        A local join can't observe remote links, so it admits optimistically;
        the over-cap peer is turned away when it tries to establish a link into
        the full session. (A local cap keyed on the signalled roster instead
        would be spoofable -- see the flood test in test_adversarial.py.)
        """
        monkeypatch.setattr("trenchchat.core.voice.MAX_VOICE_PARTICIPANTS", 2)
        (alice, bob, carol), ch_hash = _setup_open_channel(
            peer_factory, names=("alice", "bob", "carol"))

        assert alice.voice_mgr.join_voice(ch_hash) is True
        assert wait_for_roster(bob, ch_hash, alice.identity.hash_hex)
        assert bob.voice_mgr.join_voice(ch_hash) is True
        assert wait_for_roster(alice, ch_hash, bob.identity.hash_hex)

        # The session is genuinely full (two real, signalling members).
        assert alice.voice_mgr._authorize_link(
            carol.identity.hash_hex, ch_hash) is False, \
            "a third participant was admitted past the cap at the link layer"

    def test_leave_when_not_in_session_is_noop(self, peer_factory):
        bob = peer_factory("bob")
        assert actions.leave_voice_channel(bob.voice_mgr) is False


class TestVoiceCallbacks:
    def test_roster_callback_fired_on_change(self, peer_factory):
        alice, bob, ch_hash = _setup_invite_channel(peer_factory)
        seen: list[str] = []
        alice.voice_mgr.add_roster_callback(seen.append)

        bob.voice_mgr.join_voice(ch_hash)
        assert wait_for(lambda: ch_hash in seen,
                        msg="roster callback on alice")

    def test_session_callback_on_join_and_leave(self, peer_factory):
        alice, bob, ch_hash = _setup_invite_channel(peer_factory)
        states: list[str] = []
        bob.voice_mgr.add_session_callback(states.append)

        bob.voice_mgr.join_voice(ch_hash)
        bob.voice_mgr.leave_voice()
        assert "joined" in states
        assert "left" in states


# ---------------------------------------------------------------------------
# The links are the ground truth for who is audible
# ---------------------------------------------------------------------------

class TestLinkOnlyParticipants:
    """The roster is built only from LXMF signalling, but audio fan-out is
    driven by established links -- so a peer that dials in without ever
    sending voice_join was both uncounted by the participant cap and
    invisible in the participant list every client shows.
    """

    def test_a_link_only_peer_counts_towards_the_cap(self, peer_factory):
        alice, bob, ch_hash = _setup_invite_channel(peer_factory)
        alice.voice_mgr.join_voice(ch_hash)

        # Streaming links with nobody in the signalled roster. Bob is a real
        # member, so only the cap can refuse him -- otherwise this would pass
        # on the permission check instead.
        stream_hexes = [f"{i:032x}" for i in range(MAX_VOICE_PARTICIPANTS)]
        alice.voice_transport._streams.update(stream_hexes)
        assert alice.voice_mgr._peer_may_voice(ch_hash, bob.identity.hash_hex)

        assert not alice.voice_mgr._authorize_link(bob.identity.hash_hex, ch_hash), \
            "the cap counted the signalled roster while links drive fan-out"

    def test_a_link_only_peer_appears_in_the_roster(self, peer_factory):
        alice, bob, ch_hash = _setup_invite_channel(peer_factory)
        alice.voice_mgr.join_voice(ch_hash)
        alice.voice_transport._streams.add("ab" * 16)

        listeners = [r["identity_hash"] for r in alice.voice_mgr.get_roster(ch_hash)]
        assert "ab" * 16 in listeners, \
            "a peer we are streaming to was absent from the participant list"

    def test_an_ordinary_join_is_still_authorised(self, peer_factory):
        alice, bob, ch_hash = _setup_invite_channel(peer_factory)
        alice.voice_mgr.join_voice(ch_hash)

        assert alice.voice_mgr._authorize_link(bob.identity.hash_hex, ch_hash), \
            "a legitimate member was refused a voice link"


class TestVoiceResourceRelease:
    """A peer's jitter buffer and native decoder were released only on a
    polite voice_leave, so a dropped link or a revoked permission left both
    behind for the rest of the session -- and nothing ever removed the
    connection, whose every re-dial is another mesh-wide path request.
    """

    def test_a_dropped_link_releases_the_peers_audio_state(self, peer_factory):
        alice, bob, ch_hash = _setup_invite_channel(peer_factory)
        alice.voice_mgr.join_voice(ch_hash)
        bob_hex = bob.identity.hash_hex

        class _Pipeline:
            def __init__(self):
                self.dropped = []

            def drop_peer(self, peer_hex):
                self.dropped.append(peer_hex)

        pipeline = _Pipeline()
        alice.voice_mgr._audio_pipeline = pipeline
        with alice.voice_mgr._lock:
            alice.voice_mgr._rx_frames[bob_hex] = 1
            alice.voice_mgr._speaking[bob_hex] = True

        alice.voice_mgr._on_peer_link_state(bob_hex, "idle")

        assert pipeline.dropped == [bob_hex], \
            "a peer whose link dropped kept its decoder and jitter buffer"
        assert bob_hex not in alice.voice_mgr._rx_frames
        assert bob_hex not in alice.voice_mgr._speaking

    def test_a_streaming_peer_keeps_its_audio_state(self, peer_factory):
        alice, bob, ch_hash = _setup_invite_channel(peer_factory)
        alice.voice_mgr.join_voice(ch_hash)

        class _Pipeline:
            def __init__(self):
                self.dropped = []

            def drop_peer(self, peer_hex):
                self.dropped.append(peer_hex)

        pipeline = _Pipeline()
        alice.voice_mgr._audio_pipeline = pipeline

        alice.voice_mgr._on_peer_link_state(bob.identity.hash_hex, PEER_STREAMING)

        assert pipeline.dropped == [], "a live peer's decoder was released"

    def test_leaving_clears_the_session_before_stopping_the_transport(
            self, peer_factory):
        """A VP_HELLO landing in between would otherwise be authorised against
        the session being left, repopulating the connection table."""
        alice, bob, ch_hash = _setup_invite_channel(peer_factory)
        alice.voice_mgr.join_voice(ch_hash)

        seen = {}
        real_stop = alice.voice_transport.stop

        def _stop():
            seen["session_at_stop"] = alice.voice_mgr._session_channel
            real_stop()

        alice.voice_transport.stop = _stop
        alice.voice_mgr.leave_voice()

        assert seen["session_at_stop"] is None, \
            "the transport stopped while the session was still authorising links"


# ---------------------------------------------------------------------------
# Audio pipeline rebuild: device changes and unplug recovery
# ---------------------------------------------------------------------------

class _CountingPipeline:
    def __init__(self):
        self.started = False
        self.stopped = False
        self.muted = None
        self.alive = True

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def set_muted(self, muted):
        self.muted = muted

    def healthy(self):
        return self.alive


class TestAudioRestart:
    def _join_with_counting_audio(self, peer_factory):
        peers, ch_hash = _setup_open_channel(peer_factory, names=("alice",))
        alice = peers[0]
        built = []

        def factory(config, on_encoded, on_speaking_self):
            pipeline = _CountingPipeline()
            built.append(pipeline)
            return pipeline

        alice.voice_mgr._audio_factory = factory
        assert alice.voice_mgr.join_voice(ch_hash)
        return alice, built

    def test_restart_audio_rebuilds_the_pipeline(self, peer_factory):
        alice, built = self._join_with_counting_audio(peer_factory)
        assert len(built) == 1 and built[0].started

        alice.voice_mgr.restart_audio()

        assert built[0].stopped, "the old pipeline was left running"
        assert len(built) == 2 and built[1].started
        assert alice.voice_mgr.audio_status()["available"] is True

    def test_restart_audio_is_a_noop_outside_a_session(self, peer_factory):
        alice = peer_factory("alice")
        built = []
        alice.voice_mgr._audio_factory = lambda *a: built.append(a)

        alice.voice_mgr.restart_audio()
        assert built == []

    def test_tick_rebuilds_a_dead_pipeline(self, peer_factory):
        alice, built = self._join_with_counting_audio(peer_factory)
        built[0].alive = False
        alice.voice_mgr._last_audio_restart = 0.0  # cooldown long expired

        alice.voice_mgr.tick()

        assert built[0].stopped
        assert len(built) == 2 and built[1].started

    def test_tick_respects_the_restart_cooldown(self, peer_factory):
        alice, built = self._join_with_counting_audio(peer_factory)
        built[0].alive = False
        alice.voice_mgr._last_audio_restart = time.time()

        alice.voice_mgr.tick()

        assert len(built) == 1, "a restart thrashed inside the cooldown"


class TestDegradedAudioStatus:
    """A pipeline can run with one direction down; the status must say so
    and the session callback must surface it."""

    class _HalfDeafPipeline(_CountingPipeline):
        def device_status(self):
            return {"input_ok": False, "output_ok": True,
                    "input_error": "no capture device", "output_error": ""}

    def test_direction_state_reaches_audio_status(self, peer_factory):
        peers, ch_hash = _setup_open_channel(peer_factory, names=("alice",))
        alice = peers[0]
        alice.voice_mgr._audio_factory = \
            lambda *a: self._HalfDeafPipeline()
        states = []
        alice.voice_mgr.add_session_callback(states.append)

        assert alice.voice_mgr.join_voice(ch_hash)

        status = alice.voice_mgr.audio_status()
        assert status["available"] is True
        assert status["input_ok"] is False
        assert status["output_ok"] is True
        assert status["input_error"] == "no capture device"
        assert "audio_error" in states, \
            "a degraded start surfaced nothing to the client"

    def test_pipeline_without_device_status_is_fully_ok(self, peer_factory):
        peers, ch_hash = _setup_open_channel(peer_factory, names=("alice",))
        alice = peers[0]
        alice.voice_mgr._audio_factory = lambda *a: _CountingPipeline()
        states = []
        alice.voice_mgr.add_session_callback(states.append)

        assert alice.voice_mgr.join_voice(ch_hash)

        status = alice.voice_mgr.audio_status()
        assert status["available"] is True
        assert status["input_ok"] is True and status["output_ok"] is True
        assert "audio_error" not in states


class TestUnavailableAudioReason:
    def test_missing_audio_stack_reason_reaches_audio_status(
            self, peer_factory, monkeypatch):
        """create_pipeline logs why the stack is missing and returns None;
        the specific reason (not a generic line) must reach the client."""
        from trenchchat.core import audio

        peers, ch_hash = _setup_open_channel(peer_factory, names=("alice",))
        alice = peers[0]
        assert alice.voice_mgr._audio_factory is None  # default factory path
        monkeypatch.setattr(audio, "create_pipeline", lambda *a: None)
        monkeypatch.setattr(
            audio, "audio_available",
            lambda: (False, "opus codec unavailable: libopus not found"))

        assert alice.voice_mgr.join_voice(ch_hash)

        status = alice.voice_mgr.audio_status()
        assert status["available"] is False
        assert status["reason"] == "opus codec unavailable: libopus not found"
