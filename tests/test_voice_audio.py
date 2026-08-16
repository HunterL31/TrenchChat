"""
Audio primitive tests: jitter buffer (pure Python, always runs), mixer
(needs numpy), Opus codec (needs opuslib + libopus), and the availability
probe. Codec and mixer cases skip cleanly on machines without the system
libraries.
"""

import sys

import pytest

from trenchchat.core.audio.jitter import JitterBuffer


class TestJitterBuffer:
    def _drain_ready(self, buf: JitterBuffer, n: int) -> list:
        return [buf.pop() for _ in range(n)]

    def test_reorders_frames(self):
        buf = JitterBuffer(target_frames=2)
        buf.push(1, b"b")
        buf.push(0, b"a")
        buf.push(2, b"c")
        assert self._drain_ready(buf, 3) == [b"a", b"b", b"c"]

    def test_gap_returns_none(self):
        buf = JitterBuffer(target_frames=2)
        buf.push(0, b"a")
        buf.push(2, b"c")
        buf.push(3, b"d")
        assert buf.pop() == b"a"
        assert buf.pop() is None  # seq 1 lost — caller runs PLC
        assert buf.pop() == b"c"

    def test_seq_wraparound(self):
        buf = JitterBuffer(target_frames=2)
        buf.push(65535, b"a")
        buf.push(0, b"b")
        buf.push(1, b"c")
        assert self._drain_ready(buf, 3) == [b"a", b"b", b"c"]

    def test_drops_stale_and_duplicate(self):
        buf = JitterBuffer(target_frames=1)
        buf.push(5, b"a")
        assert buf.pop() == b"a"
        buf.push(5, b"late")     # already played
        buf.push(6, b"b")
        buf.push(6, b"dup")      # duplicate of a queued frame
        assert buf.pop() == b"b"

    def test_caps_depth(self):
        buf = JitterBuffer(target_frames=1, max_frames=5)
        for seq in range(10):
            buf.push(seq, bytes([seq]))
        assert buf.depth() <= 5

    def test_refills_after_starvation(self):
        buf = JitterBuffer(target_frames=3)
        buf.push(0, b"a")
        assert buf.pop() is None  # still filling
        buf.push(1, b"b")
        buf.push(2, b"c")
        assert self._drain_ready(buf, 3) == [b"a", b"b", b"c"]
        # Starved: goes back to filling until target depth again.
        assert buf.pop() is None
        buf.push(3, b"d")
        assert buf.pop() is None
        buf.push(4, b"e")
        buf.push(5, b"f")
        assert buf.pop() == b"d"

    def test_far_ahead_restarts_stream(self):
        buf = JitterBuffer(target_frames=1, max_frames=5)
        buf.push(0, b"a")
        assert buf.pop() == b"a"
        buf.push(1000, b"z")  # sender skipped far ahead
        assert buf.pop() == b"z"

    def test_reset(self):
        buf = JitterBuffer(target_frames=1)
        buf.push(0, b"a")
        buf.reset()
        assert buf.depth() == 0
        assert buf.pop() is None


class TestMixer:
    def test_sums_and_clips(self):
        pytest.importorskip("numpy")
        import numpy as np
        from trenchchat.core.audio.mixer import mix

        a = np.array([1000, -1000, 30000], dtype=np.int16).tobytes()
        b = np.array([500, -500, 30000], dtype=np.int16).tobytes()
        out = np.frombuffer(mix([a, b]), dtype=np.int16)
        assert list(out) == [1500, -1500, 32767]  # last clips, no wrap

    def test_single_frame_passthrough(self):
        pytest.importorskip("numpy")
        from trenchchat.core.audio.mixer import mix
        assert mix([b"\x01\x02"]) == b"\x01\x02"
        assert mix([]) == b""


class TestOpusCodec:
    def _codec(self):
        pytest.importorskip("opuslib")
        from trenchchat.core.audio.codec import OpusCodec
        return OpusCodec()

    def test_roundtrip_sine(self):
        import array
        import math
        codec = self._codec()
        pcm = array.array("h", (
            int(10000 * math.sin(2 * math.pi * 440 * i / 48000))
            for i in range(960)
        )).tobytes()
        encoded = codec.encode(pcm)
        assert 0 < len(encoded) <= 200
        decoded = codec.decode(encoded)
        assert len(decoded) == len(pcm)

    def test_plc_produces_audio(self):
        codec = self._codec()
        codec.encode(b"\x00" * 1920)
        plc = codec.decode(None)
        assert len(plc) == 1920


class TestAudioAvailability:
    def test_reports_reason_when_sounddevice_missing(self, monkeypatch):
        from trenchchat.core import audio
        monkeypatch.setitem(sys.modules, "sounddevice", None)
        available, reason = audio.audio_available()
        assert available is False
        assert "sounddevice" in reason

    def test_create_pipeline_returns_none_when_unavailable(self, monkeypatch):
        from trenchchat.core import audio
        monkeypatch.setitem(sys.modules, "sounddevice", None)
        assert audio.create_pipeline(None, lambda s, f: None,
                                     lambda s: None) is None


class TestTonePipeline:
    def test_emits_bundles_when_enabled(self):
        from trenchchat.core.audio.engine import make_tone_pipeline
        from trenchchat.network.voice_wire import VOICE_FRAMES_PER_PACKET

        emitted: list[tuple[int, list[bytes]]] = []
        pipeline = make_tone_pipeline(
            None, lambda seq, frames: emitted.append((seq, frames)),
            lambda speaking: None)
        pipeline.start()
        try:
            pipeline.set_tone_enabled(True)
            deadline = 5.0
            import time
            start = time.time()
            while not emitted and time.time() - start < deadline:
                time.sleep(0.05)
        finally:
            pipeline.stop()
        assert emitted, "tone pipeline emitted nothing"
        seq, frames = emitted[0]
        assert len(frames) == VOICE_FRAMES_PER_PACKET
        assert all(0 < len(f) <= 200 for f in frames)

    def test_silent_until_enabled_and_when_muted(self):
        import time
        from trenchchat.core.audio.engine import make_tone_pipeline

        emitted: list = []
        pipeline = make_tone_pipeline(
            None, lambda seq, frames: emitted.append(seq), lambda s: None)
        pipeline.start()
        try:
            time.sleep(0.2)
            assert emitted == []
            pipeline.set_tone_enabled(True)
            pipeline.set_muted(True)
            time.sleep(0.2)
            assert emitted == []
        finally:
            pipeline.stop()
