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


# ---------------------------------------------------------------------------
# One malformed frame must not silence the session
# ---------------------------------------------------------------------------

class TestPlayoutSurvivesABadFrame:
    """Opus returns as many samples as the packet held, so a peer encoding at
    10 ms yields half a frame. mix() sums int16 arrays and raises on a length
    mismatch, and the playout thread is the only thing driving playback."""

    def test_mix_skips_a_short_frame_instead_of_raising(self):
        from trenchchat.core.audio.mixer import mix

        full = b"\x01\x00" * 960
        short = b"\x02\x00" * 480

        out = mix([full, short, full])
        assert len(out) == len(full), "a short frame changed the block length"

    def test_mix_of_equal_frames_still_sums(self):
        from trenchchat.core.audio.mixer import mix
        import numpy as np

        a = np.full(960, 100, dtype=np.int16).tobytes()
        b = np.full(960, 200, dtype=np.int16).tobytes()

        summed = np.frombuffer(mix([a, b]), dtype=np.int16)
        assert summed[0] == 300

    def test_mix_saturates_rather_than_wrapping(self):
        from trenchchat.core.audio.mixer import mix
        import numpy as np

        loud = np.full(960, 30000, dtype=np.int16).tobytes()
        summed = np.frombuffer(mix([loud, loud]), dtype=np.int16)
        assert summed[0] == 32767


# ---------------------------------------------------------------------------
# Device enumeration and resolution
# ---------------------------------------------------------------------------

_DEVICE_TABLE = [
    {"name": "Built-in Mic", "max_input_channels": 2, "max_output_channels": 0},
    {"name": "Built-in Speakers", "max_input_channels": 0,
     "max_output_channels": 2},
    {"name": "USB Headset", "max_input_channels": 1, "max_output_channels": 2},
]


class _FakeSounddevice:
    def __init__(self, devices=_DEVICE_TABLE):
        self._devices = devices

    def query_devices(self):
        return self._devices


class TestDeviceListing:
    def test_groups_by_direction(self, monkeypatch):
        from trenchchat.core.audio import devices
        monkeypatch.setitem(sys.modules, "sounddevice", _FakeSounddevice())

        info = devices.list_devices()
        assert info["available"] is True
        assert info["input"] == ["Built-in Mic", "USB Headset"]
        assert info["output"] == ["Built-in Speakers", "USB Headset"]

    def test_reports_reason_when_stack_missing(self, monkeypatch):
        from trenchchat.core.audio import devices
        monkeypatch.setitem(sys.modules, "sounddevice", None)

        info = devices.list_devices()
        assert info["available"] is False
        assert info["reason"]
        assert info["input"] == [] and info["output"] == []


class TestDeviceResolution:
    def test_none_means_default(self, monkeypatch):
        from trenchchat.core.audio import devices
        monkeypatch.setitem(sys.modules, "sounddevice", _FakeSounddevice())
        assert devices.resolve_device(None, "input") is None

    def test_name_resolves_to_index_for_its_direction(self, monkeypatch):
        from trenchchat.core.audio import devices
        monkeypatch.setitem(sys.modules, "sounddevice", _FakeSounddevice())
        assert devices.resolve_device("USB Headset", "input") == 2
        assert devices.resolve_device("USB Headset", "output") == 2
        assert devices.resolve_device("Built-in Mic", "input") == 0

    def test_name_in_wrong_direction_falls_back(self, monkeypatch):
        from trenchchat.core.audio import devices
        monkeypatch.setitem(sys.modules, "sounddevice", _FakeSounddevice())
        assert devices.resolve_device("Built-in Mic", "output") is None

    def test_unplugged_name_falls_back(self, monkeypatch):
        from trenchchat.core.audio import devices
        monkeypatch.setitem(sys.modules, "sounddevice", _FakeSounddevice())
        assert devices.resolve_device("Gone Headset", "input") is None

    def test_valid_index_passes_through_and_stale_falls_back(self, monkeypatch):
        from trenchchat.core.audio import devices
        monkeypatch.setitem(sys.modules, "sounddevice", _FakeSounddevice())
        assert devices.resolve_device(0, "input") == 0
        assert devices.resolve_device(0, "output") is None
        assert devices.resolve_device(99, "input") is None

    def test_query_failure_falls_back(self, monkeypatch):
        from trenchchat.core.audio import devices
        monkeypatch.setitem(sys.modules, "sounddevice", None)
        assert devices.resolve_device("USB Headset", "input") is None


# ---------------------------------------------------------------------------
# Pipeline device open fallback and health probe
# ---------------------------------------------------------------------------

class _VoiceConfig:
    def __init__(self, input_device=None, output_device=None):
        self.voice_input_device = input_device
        self.voice_output_device = output_device


class _FakeStream:
    def __init__(self, device=None, **kwargs):
        self.device = device
        self.active = True

    def start(self):
        pass

    def stop(self):
        self.active = False

    def close(self):
        pass


class TestPipelineDeviceFallback:
    def _pipeline(self, config):
        from trenchchat.core.audio.engine import AudioPipeline
        return AudioPipeline(config, lambda s, f: None, lambda s: None,
                             codec_factory=lambda: None)

    def test_open_uses_the_resolved_device(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "sounddevice", _FakeSounddevice())
        pipeline = self._pipeline(_VoiceConfig(input_device="USB Headset"))

        stream = pipeline._open_stream(_FakeStream, "input", lambda *a: None)
        assert stream.device == 2

    def test_open_failure_on_a_chosen_device_falls_back(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "sounddevice", _FakeSounddevice())
        pipeline = self._pipeline(_VoiceConfig(input_device="USB Headset"))

        class _Flaky(_FakeStream):
            def __init__(self, device=None, **kwargs):
                if device is not None:
                    raise RuntimeError("device disappeared")
                super().__init__(device=device, **kwargs)

        stream = pipeline._open_stream(_Flaky, "input", lambda *a: None)
        assert stream.device is None

    def test_open_failure_on_the_default_raises(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "sounddevice", _FakeSounddevice())
        pipeline = self._pipeline(_VoiceConfig())

        class _Broken(_FakeStream):
            def __init__(self, device=None, **kwargs):
                raise RuntimeError("no devices at all")

        with pytest.raises(RuntimeError):
            pipeline._open_stream(_Broken, "input", lambda *a: None)

    def test_healthy_tracks_stream_liveness(self):
        pipeline = self._pipeline(_VoiceConfig())
        assert pipeline.healthy() is True  # not running yet

        pipeline._running = True
        pipeline._in_stream = _FakeStream()
        pipeline._out_stream = _FakeStream()
        assert pipeline.healthy() is True

        pipeline._out_stream.active = False
        assert pipeline.healthy() is False

        pipeline._out_stream = None
        assert pipeline.healthy() is False
