"""
Voice quality tests: receive-quality metrics, and a comparison of the
voice path's parameters against Discord's standard voice settings.

Discord's standard voice profile is Opus at 48 kHz, mono, 20 ms frames,
64 kbps default bitrate, with client-side jitter buffering keeping the
processing latency budget under ~150 ms. These tests pin TrenchChat's
voice path to that class: same codec profile, the Discord default bitrate
must work within our wire format, decoded fidelity at that bitrate must
be high, and the algorithmic latency budget must stay in range. Network
loss/jitter against the same thresholds is measured over a real network
by devtools/testenv/smoke_test.py.
"""

import array
import math
import time

import pytest

from tests.helpers import wait_for, wait_for_rx_frames
from tests.test_voice import _setup_invite_channel
from tests.test_voice_transport import _join_both
from trenchchat.core.voice import NOMINAL_FRAME_RATE_FPS
from trenchchat.network.voice_wire import (
    VOICE_FRAME_MS, VOICE_FRAMES_PER_PACKET, VOICE_MAX_FRAME_BYTES,
    pack_audio,
)

# Discord standard voice profile, for comparison.
DISCORD_SAMPLE_RATE = 48000
DISCORD_FRAME_MS = 20
DISCORD_CHANNELS = 1
DISCORD_DEFAULT_BITRATE = 64000
DISCORD_LATENCY_BUDGET_MS = 150


def _speech_like_pcm(frames: int) -> bytes:
    """A few harmonics in the speech band, continuous across frames."""
    samples = array.array("h")
    total = frames * 960
    for i in range(total):
        t = i / 48000.0
        value = (
            0.5 * math.sin(2 * math.pi * 220 * t)
            + 0.3 * math.sin(2 * math.pi * 440 * t)
            + 0.2 * math.sin(2 * math.pi * 880 * t)
        )
        samples.append(int(12000 * value))
    return samples.tobytes()


# ---------------------------------------------------------------------------
# Codec profile vs Discord
# ---------------------------------------------------------------------------

class TestDiscordComparableProfile:
    def test_codec_matches_discord_voice_profile(self):
        pytest.importorskip("opuslib")
        from trenchchat.core.audio import codec

        assert codec.OPUS_SAMPLE_RATE == DISCORD_SAMPLE_RATE
        assert codec.OPUS_FRAME_MS == DISCORD_FRAME_MS
        assert codec.OPUS_CHANNELS == DISCORD_CHANNELS
        assert VOICE_FRAME_MS == DISCORD_FRAME_MS

    def test_discord_default_bitrate_fits_wire_format(self):
        """64 kbps — Discord's default voice bitrate — must be usable:
        every encoded frame within the per-frame cap, and size-aware
        bundling keeping every packet within one link MDU even across
        VBR peaks."""
        pytest.importorskip("opuslib")
        import RNS
        from trenchchat.core.audio.codec import OpusCodec
        from trenchchat.network.voice_wire import bundle_frames

        codec = OpusCodec(bitrate=DISCORD_DEFAULT_BITRATE)
        pcm = _speech_like_pcm(50)
        encoded = [codec.encode(pcm[i * 1920:(i + 1) * 1920])
                   for i in range(50)]
        assert all(0 < len(f) <= VOICE_MAX_FRAME_BYTES for f in encoded)

        for bundle in bundle_frames(encoded):
            assert len(pack_audio(0, bundle)) <= RNS.Link.MDU

    def test_fidelity_at_discord_bitrate(self):
        """Decoded audio at 64 kbps must preserve the input tones cleanly,
        and the default 16 kbps mesh profile must stay intelligible-grade.

        Fidelity is measured as tone-to-noise ratio in the frequency
        domain (energy at the input tones vs everything else), which is
        insensitive to the codec's fractional-sample delay. Reference
        points on this signal: the raw input measures ~69 dB, Opus 64 kbps
        ~37 dB, Opus 16 kbps ~30 dB."""
        pytest.importorskip("opuslib")
        numpy = pytest.importorskip("numpy")
        from trenchchat.core.audio.codec import OpusCodec

        def tone_to_noise_db(bitrate: int) -> float:
            codec = OpusCodec(bitrate=bitrate)
            frames = 60
            pcm = _speech_like_pcm(frames)
            decoded = b"".join(
                codec.decode(codec.encode(pcm[i * 1920:(i + 1) * 1920]))
                for i in range(frames)
            )
            x = numpy.frombuffer(decoded, dtype=numpy.int16).astype(float)
            x = x[4800:4800 + 32768]  # skip encoder warm-up
            spectrum = numpy.abs(
                numpy.fft.rfft(x * numpy.hanning(len(x)))) ** 2
            freqs = numpy.fft.rfftfreq(len(x), 1 / 48000)
            tone_mask = numpy.zeros_like(spectrum, dtype=bool)
            for f in (220, 440, 880):
                tone_mask |= numpy.abs(freqs - f) < 15
            tone = spectrum[tone_mask].sum()
            noise = spectrum[~tone_mask].sum()
            return 10.0 * math.log10(tone / max(noise, 1e-9))

        discord_quality = tone_to_noise_db(DISCORD_DEFAULT_BITRATE)
        mesh_quality = tone_to_noise_db(16000)
        assert discord_quality >= 32.0, \
            "64 kbps fidelity below Discord-comparable quality"
        assert mesh_quality >= 24.0, \
            "default mesh bitrate fell below intelligible quality"
        assert discord_quality > mesh_quality, \
            "raising the bitrate to Discord's default did not improve quality"

    def test_latency_budget_within_discord_range(self):
        """The algorithmic (non-network) latency budget: one capture frame,
        bundling delay, jitter-buffer target depth, and one playout tick.
        Discord's client budget for the same stages is ~150 ms; ours must
        not exceed it, so total mouth-to-ear stays comparable on a fast
        path."""
        from trenchchat.core.audio.jitter import JITTER_TARGET_FRAMES

        budget_ms = (
            VOICE_FRAME_MS                                  # capture
            + (VOICE_FRAMES_PER_PACKET - 1) * VOICE_FRAME_MS  # bundling
            + JITTER_TARGET_FRAMES * VOICE_FRAME_MS           # jitter buffer
            + VOICE_FRAME_MS                                  # playout tick
        )
        assert budget_ms <= DISCORD_LATENCY_BUDGET_MS

    def test_bandwidth_at_or_below_discord_default(self):
        """Per-stream bandwidth at the default mesh bitrate (payload plus
        per-packet framing) must not exceed Discord's 64 kbps default, so a
        full 8-way call costs less upstream than 7 Discord streams."""
        pytest.importorskip("opuslib")
        from trenchchat.core.audio.codec import OpusCodec

        codec = OpusCodec(bitrate=16000)
        frames = 50  # one second
        pcm = _speech_like_pcm(frames)
        encoded = [codec.encode(pcm[i * 1920:(i + 1) * 1920])
                   for i in range(frames)]
        packets = [
            pack_audio(i, encoded[i:i + VOICE_FRAMES_PER_PACKET])
            for i in range(0, frames, VOICE_FRAMES_PER_PACKET)
        ]
        bits_per_second = sum(len(p) for p in packets) * 8
        assert bits_per_second <= DISCORD_DEFAULT_BITRATE


# ---------------------------------------------------------------------------
# Receive-quality metrics
# ---------------------------------------------------------------------------

class _StubPipeline:
    """Audio pipeline double exposing only what VoiceManager calls."""

    def __init__(self, playout: dict):
        self._playout = playout

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def set_muted(self, muted: bool) -> None:
        pass

    def playout_stats(self) -> dict:
        return self._playout


class TestRxQualityMetrics:
    def test_clean_stream_reports_no_loss(self, peer_factory):
        alice, bob, ch_hash = _setup_invite_channel(peer_factory)
        _join_both(alice, bob, ch_hash)

        for i in range(10):
            bob.voice_transport.send_frames(i * 2, [b"\x01" * 40] * 2)
            time.sleep(0.04)
        assert wait_for_rx_frames(alice, bob.identity.hash_hex, 20)

        quality = alice.voice_mgr.frame_stats()["rx_quality"][
            bob.identity.hash_hex]
        assert quality["received"] == 20
        assert quality["lost"] == 0
        assert quality["loss_pct"] == 0.0

    def test_dropped_packets_counted_as_loss(self, peer_factory):
        alice, bob, ch_hash = _setup_invite_channel(peer_factory)
        _join_both(alice, bob, ch_hash)

        bob.voice_transport.drop_every_n = 2  # every 2nd packet vanishes
        for i in range(10):
            bob.voice_transport.send_frames(i * 2, [b"\x01" * 40] * 2)
            time.sleep(0.04)

        def lossy():
            stats = alice.voice_mgr.frame_stats()["rx_quality"]
            q = stats.get(bob.identity.hash_hex)
            return q and q["lost"] > 0 and q["loss_pct"] > 0
        assert wait_for(lossy, msg="loss recorded on alice")

    def test_irregular_arrival_registers_jitter(self, peer_factory):
        alice, bob, ch_hash = _setup_invite_channel(peer_factory)
        _join_both(alice, bob, ch_hash)

        # Seq spacing says 40 ms per packet; actual pacing alternates far
        # off that, so smoothed jitter must rise above zero.
        for i in range(10):
            bob.voice_transport.send_frames(i * 2, [b"\x01" * 40] * 2)
            time.sleep(0.005 if i % 2 else 0.1)
        assert wait_for_rx_frames(alice, bob.identity.hash_hex, 20)

        quality = alice.voice_mgr.frame_stats()["rx_quality"][
            bob.identity.hash_hex]
        assert quality["jitter_ms"] > 0.0

    def test_uniformly_slow_sender_is_invisible_to_loss_but_not_rate_fps(
            self, peer_factory):
        """The blind spot rate_fps exists for.

        loss and jitter are clocked by sequence number, so a sender emitting
        every frame but 25% slower than its own seq spacing claims scores 0%
        loss and near-zero jitter — while the listener's jitter buffer drains,
        starves and refills, which is audible as the stream cutting in and out.
        Wall-clock rate is the only metric here that sees it.
        """
        alice, bob, ch_hash = _setup_invite_channel(peer_factory)
        _join_both(alice, bob, ch_hash)

        packets = 30
        next_at = time.monotonic()
        for i in range(packets):
            bob.voice_transport.send_frames(i * 2, [b"\x01" * 40] * 2)
            next_at += 0.05  # seq spacing claims 40 ms
            delay = next_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        assert wait_for_rx_frames(alice, bob.identity.hash_hex, packets * 2)

        quality = alice.voice_mgr.frame_stats()["rx_quality"][
            bob.identity.hash_hex]
        assert quality["lost"] == 0
        assert quality["loss_pct"] == 0.0
        assert 34.0 <= quality["rate_fps"] <= 46.0, (
            f"expected ~40 fps against a nominal {NOMINAL_FRAME_RATE_FPS:.0f}, "
            f"got {quality['rate_fps']}"
        )

    def test_rate_is_none_until_the_span_is_long_enough(self, peer_factory):
        alice, bob, ch_hash = _setup_invite_channel(peer_factory)
        _join_both(alice, bob, ch_hash)
        bob.voice_transport.send_frames(0, [b"\x01" * 40] * 2)
        assert wait_for_rx_frames(alice, bob.identity.hash_hex, 2)

        quality = alice.voice_mgr.frame_stats()["rx_quality"][
            bob.identity.hash_hex]
        assert quality["rate_fps"] is None

    def test_playout_counters_reach_frame_stats(self, peer_factory):
        alice, _bob, ch_hash = _setup_invite_channel(peer_factory)
        counts = {"ab" * 16: {"decoded": 40, "plc": 1, "starved": 3}}
        alice.voice_mgr._audio_factory = lambda *a: _StubPipeline(counts)
        assert alice.voice_mgr.join_voice(ch_hash)

        assert alice.voice_mgr.frame_stats()["playout"] == counts

    def test_playout_is_empty_for_a_pipeline_without_counters(self,
                                                              peer_factory):
        alice, _bob, ch_hash = _setup_invite_channel(peer_factory)
        assert alice.voice_mgr.join_voice(ch_hash)

        assert alice.voice_mgr.frame_stats()["playout"] == {}

    def test_quality_counters_reset_on_leave(self, peer_factory):
        alice, bob, ch_hash = _setup_invite_channel(peer_factory)
        _join_both(alice, bob, ch_hash)
        bob.voice_transport.send_frames(0, [b"\x01" * 40])
        assert wait_for_rx_frames(alice, bob.identity.hash_hex, 1)

        alice.voice_mgr.leave_voice()
        assert alice.voice_mgr.frame_stats()["rx_quality"] == {}
