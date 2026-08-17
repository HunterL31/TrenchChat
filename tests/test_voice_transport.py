"""
Voice frame-plane tests: wire format parsing, and streaming behavior via
the in-process FakeVoiceTransport (dial authorization, dedup, redial,
speaking indicators). Real RNS Link behavior is exercised against a live
two-process network by devtools/testenv/smoke_test.py.
"""

import time

import pytest

from tests.helpers import wait_for, wait_for_roster, wait_for_rx_frames
from tests.test_voice import _setup_invite_channel
from trenchchat.network import voice_wire
from trenchchat.network.voice_wire import (
    VOICE_MAX_FRAME_BYTES, VOICE_WIRE_VERSION, VP_AUDIO,
    pack_accept, pack_audio, pack_hello,
    unpack_accept, unpack_audio, unpack_hello,
)


# ---------------------------------------------------------------------------
# Wire format
# ---------------------------------------------------------------------------

class TestVoiceWire:
    def test_audio_roundtrip(self):
        frames = [b"\x01" * 40, b"\x02" * 61]
        payload = pack_audio(1234, frames)
        seq, out = unpack_audio(payload)
        assert seq == 1234
        assert out == frames

    def test_audio_seq_wraps_modulus(self):
        payload = pack_audio(70000, [b"x"])
        seq, _ = unpack_audio(payload)
        assert seq == 70000 % (1 << 16)

    def test_audio_rejects_wrong_type(self):
        with pytest.raises(ValueError):
            unpack_audio(pack_accept())

    def test_audio_rejects_truncated(self):
        payload = pack_audio(1, [b"\x01" * 40])
        with pytest.raises(ValueError):
            unpack_audio(payload[:-5])

    def test_audio_rejects_trailing_bytes(self):
        payload = pack_audio(1, [b"\x01" * 40]) + b"junk"
        with pytest.raises(ValueError):
            unpack_audio(payload)

    def test_audio_rejects_oversized_frame(self):
        with pytest.raises(ValueError):
            pack_audio(1, [b"x" * (VOICE_MAX_FRAME_BYTES + 1)])
        with pytest.raises(ValueError):
            pack_audio(1, [])

    def test_audio_payload_budget_fits_link_mdu(self):
        import RNS
        assert voice_wire.VOICE_MAX_PACKET_PAYLOAD <= RNS.Link.MDU

    def test_audio_rejects_over_budget_bundle(self):
        frames = [b"\xff" * VOICE_MAX_FRAME_BYTES
                  for _ in range(voice_wire.VOICE_FRAMES_PER_PACKET)]
        with pytest.raises(ValueError):
            pack_audio(0, frames)

    def test_bundle_frames_respects_budget(self):
        import RNS
        frames = [b"\xff" * VOICE_MAX_FRAME_BYTES for _ in range(5)] + \
                 [b"\x01" * 40 for _ in range(5)]
        bundles = voice_wire.bundle_frames(frames)
        assert [f for bundle in bundles for f in bundle] == frames
        assert all(len(pack_audio(0, bundle)) <= RNS.Link.MDU
                   for bundle in bundles)

    def test_hello_roundtrip(self):
        channel_hash = bytes(range(16))
        version, codec_id, out = unpack_hello(pack_hello(channel_hash))
        assert version == VOICE_WIRE_VERSION
        assert codec_id == voice_wire.CODEC_OPUS
        assert out == channel_hash

    def test_hello_rejects_bad_length(self):
        with pytest.raises(ValueError):
            unpack_hello(pack_hello(bytes(16))[:-1])
        with pytest.raises(ValueError):
            pack_hello(b"short")

    def test_accept_roundtrip(self):
        assert unpack_accept(pack_accept()) == VOICE_WIRE_VERSION

    def test_packet_type_empty(self):
        with pytest.raises(ValueError):
            voice_wire.packet_type(b"")


# ---------------------------------------------------------------------------
# Streaming via FakeVoiceTransport
# ---------------------------------------------------------------------------

def _join_both(alice, bob, ch_hash):
    assert alice.voice_mgr.join_voice(ch_hash) is True
    assert wait_for_roster(bob, ch_hash, alice.identity.hash_hex)
    assert bob.voice_mgr.join_voice(ch_hash) is True
    assert wait_for(
        lambda: bob.identity.hash_hex in
        alice.voice_transport.connected_peers(),
        msg="alice streaming to bob",
    )


class TestVoiceStreaming:
    def test_frames_flow_between_two_participants(self, peer_factory):
        alice, bob, ch_hash = _setup_invite_channel(peer_factory)
        _join_both(alice, bob, ch_hash)

        bob.voice_transport.send_frames(0, [b"\x01" * 40, b"\x02" * 40])
        alice.voice_transport.send_frames(0, [b"\x03" * 40])
        assert wait_for_rx_frames(alice, bob.identity.hash_hex, 2)
        assert wait_for_rx_frames(bob, alice.identity.hash_hex, 1)

    def test_speaking_flag_follows_frames(self, peer_factory):
        alice, bob, ch_hash = _setup_invite_channel(peer_factory)
        _join_both(alice, bob, ch_hash)

        def bob_entry():
            return next((e for e in alice.voice_mgr.get_roster(ch_hash)
                         if e["identity_hash"] == bob.identity.hash_hex), None)

        bob.voice_transport.send_frames(0, [b"\x01" * 40])
        assert wait_for(lambda: bob_entry() and bob_entry()["speaking"],
                        msg="bob speaking on alice's roster")
        # No further frames: the flag decays after the hold interval.
        assert wait_for(lambda: bob_entry() and not bob_entry()["speaking"],
                        msg="bob's speaking flag decays")

    def test_deterministic_initiator_dials_once(self, peer_factory):
        alice, bob, ch_hash = _setup_invite_channel(peer_factory)
        _join_both(alice, bob, ch_hash)

        smaller = min(alice.identity.hash_hex, bob.identity.hash_hex)
        pair = {alice.identity.hash_hex, bob.identity.hash_hex}
        dials = [d for d in alice.voice_transport.registry.dial_log
                 if {d[0], d[1]} == pair]
        assert dials, "no dial was recorded"
        assert all(dialer == smaller for dialer, _ in dials)

    def test_redial_after_simulated_drop(self, peer_factory):
        alice, bob, ch_hash = _setup_invite_channel(peer_factory)
        _join_both(alice, bob, ch_hash)

        bob.voice_transport.simulate_drop(alice.identity.hash_hex)
        assert wait_for(
            lambda: bob.identity.hash_hex in
            alice.voice_transport.connected_peers(),
            msg="stream re-established after drop",
        )

    def test_unreachable_peer_marked(self, peer_factory):
        alice, bob, ch_hash = _setup_invite_channel(peer_factory)
        alice.voice_transport.fail_connect_to.add(bob.identity.hash_hex)
        bob.voice_transport.fail_connect_to.add(alice.identity.hash_hex)

        assert alice.voice_mgr.join_voice(ch_hash) is True
        assert wait_for_roster(bob, ch_hash, alice.identity.hash_hex)
        assert bob.voice_mgr.join_voice(ch_hash) is True

        def bob_link_state():
            entry = next((e for e in alice.voice_mgr.get_roster(ch_hash)
                          if e["identity_hash"] == bob.identity.hash_hex),
                         None)
            return entry["link_state"] if entry else None

        assert wait_for(lambda: bob_link_state() == "unreachable",
                        timeout=15.0, msg="bob marked unreachable")

    def test_leave_stops_transport(self, peer_factory):
        alice, bob, ch_hash = _setup_invite_channel(peer_factory)
        _join_both(alice, bob, ch_hash)

        bob.voice_mgr.leave_voice()
        assert bob.voice_transport.connected_peers() == set()
        assert wait_for(
            lambda: alice.voice_transport.connected_peers() == set(),
            msg="alice's stream to bob torn down",
        )

        before = alice.voice_mgr.frame_stats()["rx_frames"].get(
            bob.identity.hash_hex, 0)
        bob.voice_transport.send_frames(0, [b"\x01" * 40])
        time.sleep(0.3)
        after = alice.voice_mgr.frame_stats()["rx_frames"].get(
            bob.identity.hash_hex, 0)
        assert after == before
