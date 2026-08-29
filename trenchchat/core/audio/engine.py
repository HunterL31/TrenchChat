"""
Audio pipelines: capture → encode → transmit and receive → mix → playback.

AudioPipeline drives real devices via sounddevice/PortAudio. Heavy
dependencies (sounddevice, the Opus codec, the numpy mixer) are imported
lazily inside methods so this module itself imports anywhere — headless
testenv workers use TonePipeline, which feeds the same encode/transmit
path with a generated signal and needs no devices at all.

Threading: the PortAudio callbacks only move PCM blocks between bounded
queues; encoding runs on a dedicated thread (so socket writes never happen
on the audio callback), and playout runs on its own 20 ms-cadence thread
that pops each sender's jitter buffer, decodes with packet-loss
concealment, and mixes.
"""

import array
import math
import queue
import struct
import threading
import time

import RNS

from trenchchat.core.audio.jitter import JitterBuffer
from trenchchat.network.voice_wire import (
    VOICE_FRAME_MS, VOICE_FRAMES_PER_PACKET, VOICE_MAX_PACKET_PAYLOAD,
)

FRAME_SAMPLES = 48000 * VOICE_FRAME_MS // 1000
FRAME_PCM_BYTES = FRAME_SAMPLES * 2
_PCM_QUEUE_BLOCKS = 8
_OUT_QUEUE_BLOCKS = 3
_SPEAKING_HANGOVER_SECS = 0.3
_DEFAULT_VAD_THRESHOLD_DB = -45.0
_SEQ_MODULUS = 1 << 16


def _rms_db(pcm: bytes) -> float:
    samples = array.array("h", pcm)
    if not samples:
        return -120.0
    acc = 0
    for sample in samples:
        acc += sample * sample
    rms = math.sqrt(acc / len(samples)) / 32768.0
    if rms <= 0.0:
        return -120.0
    return 20.0 * math.log10(rms)


class AudioPipeline:
    """Full-duplex device pipeline for one voice session."""

    def __init__(self, config, on_encoded, on_speaking_self,
                 codec_factory=None):
        self._config = config
        self._on_encoded = on_encoded
        self._on_speaking_self = on_speaking_self
        if codec_factory is None:
            from trenchchat.core.audio.codec import OpusCodec
            bitrate = _voice_setting(config, "bitrate", 16000)
            codec_factory = lambda: OpusCodec(bitrate=bitrate)
        self._codec_factory = codec_factory
        self._encoder = codec_factory()

        self._muted = False
        self._running = False
        self._seq = 0
        self._pcm_in: queue.Queue = queue.Queue(maxsize=_PCM_QUEUE_BLOCKS)
        self._pcm_out: queue.Queue = queue.Queue(maxsize=_OUT_QUEUE_BLOCKS)
        self._jitter: dict[str, JitterBuffer] = {}
        self._decoders: dict[str, object] = {}
        self._peer_lock = threading.Lock()
        self._in_stream = None
        self._out_stream = None
        self._encode_thread = None
        self._playout_thread = None
        self._speaking = False
        self._last_voiced_at = 0.0

    def start(self) -> None:
        import sounddevice as sd

        self._running = True
        try:
            self._in_stream = self._open_stream(
                sd.RawInputStream, "input", self._on_input_block)
            self._out_stream = self._open_stream(
                sd.RawOutputStream, "output", self._on_output_block)
        except Exception:
            self._close_streams()
            self._running = False
            raise
        self._encode_thread = threading.Thread(
            target=self._encode_loop, daemon=True, name="voice-encode")
        self._playout_thread = threading.Thread(
            target=self._playout_loop, daemon=True, name="voice-playout")
        self._in_stream.start()
        self._out_stream.start()
        self._encode_thread.start()
        self._playout_thread.start()

    def _open_stream(self, stream_cls, kind: str, callback):
        """Open a stream on the configured device, falling back to the
        system default when that device is missing or fails to open."""
        from trenchchat.core.audio.devices import resolve_device

        configured = _voice_setting(self._config, f"{kind}_device", None)
        device = resolve_device(configured, kind)
        kwargs = dict(samplerate=48000, channels=1, dtype="int16",
                      blocksize=FRAME_SAMPLES, callback=callback)
        try:
            return stream_cls(device=device, **kwargs)
        except Exception as e:
            if device is None:
                raise
            RNS.log(f"TrenchChat [voice]: {kind} device {configured!r} "
                    f"failed to open ({e}); falling back to default",
                    RNS.LOG_WARNING)
            return stream_cls(device=None, **kwargs)

    def stop(self) -> None:
        self._running = False
        for thread in (self._encode_thread, self._playout_thread):
            if thread is not None:
                thread.join(timeout=2.0)
        self._close_streams()

    def _close_streams(self) -> None:
        for stream in (self._in_stream, self._out_stream):
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception as e:
                    RNS.log(f"TrenchChat [voice]: stream close error: {e}",
                            RNS.LOG_DEBUG)
        self._in_stream = None
        self._out_stream = None

    def healthy(self) -> bool:
        """False once either PortAudio stream has died (device unplugged);
        the owner rebuilds the pipeline, re-resolving devices."""
        if not self._running:
            return True
        for stream in (self._in_stream, self._out_stream):
            if stream is None:
                return False
            try:
                if not stream.active:
                    return False
            except Exception:
                return False
        return True

    def set_muted(self, muted: bool) -> None:
        self._muted = muted

    def play(self, peer_hex: str, seq: int, frames: list[bytes]) -> None:
        with self._peer_lock:
            buffer = self._jitter.get(peer_hex)
            if buffer is None:
                buffer = JitterBuffer()
                self._jitter[peer_hex] = buffer
                self._decoders[peer_hex] = self._codec_factory()
        for i, frame in enumerate(frames):
            buffer.push(seq + i, frame)

    def drop_peer(self, peer_hex: str) -> None:
        with self._peer_lock:
            self._jitter.pop(peer_hex, None)
            self._decoders.pop(peer_hex, None)

    # --- device callbacks (never block, never do real work) ---

    def _on_input_block(self, indata, frames, time_info, status) -> None:
        try:
            self._pcm_in.put_nowait(bytes(indata))
        except queue.Full:
            pass

    def _on_output_block(self, outdata, frames, time_info, status) -> None:
        try:
            block = self._pcm_out.get_nowait()
        except queue.Empty:
            block = b"\x00" * len(outdata)
        outdata[:] = block[:len(outdata)].ljust(len(outdata), b"\x00")

    # --- worker threads ---

    def _encode_loop(self) -> None:
        bundle: list[bytes] = []
        bundle_seq = 0
        bundle_size = 4
        while self._running:
            try:
                pcm = self._pcm_in.get(timeout=0.2)
            except queue.Empty:
                continue
            if len(pcm) != FRAME_PCM_BYTES:
                continue
            if not self._gate_open(pcm):
                bundle.clear()
                bundle_size = 4
                continue
            try:
                encoded = self._encoder.encode(pcm)
            except Exception as e:
                RNS.log(f"TrenchChat [voice]: encode error: {e}",
                        RNS.LOG_ERROR)
                continue
            # High-bitrate VBR peaks can overflow the packet payload budget;
            # flush the pending bundle early rather than exceed it.
            if bundle and bundle_size + 1 + len(encoded) > \
                    VOICE_MAX_PACKET_PAYLOAD:
                self._on_encoded(bundle_seq, list(bundle))
                bundle.clear()
                bundle_size = 4
            if not bundle:
                bundle_seq = self._seq
            bundle.append(encoded)
            bundle_size += 1 + len(encoded)
            self._seq = (self._seq + 1) % _SEQ_MODULUS
            if len(bundle) >= VOICE_FRAMES_PER_PACKET:
                self._on_encoded(bundle_seq, list(bundle))
                bundle.clear()
                bundle_size = 4

    def _gate_open(self, pcm: bytes) -> bool:
        """Mute switch plus voice-activity gate.

        In "ptt" mode an unmuted pipeline always transmits — the frontend
        maps the push-to-talk key to set_muted(False) while held.
        """
        if self._muted:
            self._set_speaking(False)
            return False
        if _voice_setting(self._config, "mode", "vad") != "vad":
            self._set_speaking(True)
            return True
        threshold = _voice_setting(
            self._config, "vad_threshold_db", _DEFAULT_VAD_THRESHOLD_DB)
        now = time.time()
        if _rms_db(pcm) >= threshold:
            self._last_voiced_at = now
            self._set_speaking(True)
            return True
        if now - self._last_voiced_at <= _SPEAKING_HANGOVER_SECS:
            return True
        self._set_speaking(False)
        return False

    def _set_speaking(self, speaking: bool) -> None:
        if speaking == self._speaking:
            return
        self._speaking = speaking
        try:
            self._on_speaking_self(speaking)
        except Exception as e:
            RNS.log(f"TrenchChat [voice]: speaking callback error: {e}",
                    RNS.LOG_ERROR)

    def _playout_loop(self) -> None:
        """Mix one frame per peer per tick.

        Wrapped whole: this thread is the only thing driving playback, and
        nothing restarts it, so an exception escaping here silences the
        session while audio_status() still reports it available.
        """
        from trenchchat.core.audio.mixer import mix

        interval = VOICE_FRAME_MS / 1000.0
        next_at = time.monotonic()
        while self._running:
            try:
                next_at = self._playout_tick(mix, next_at, interval)
            except Exception as e:
                RNS.log(f"TrenchChat [voice]: playout error: {e}", RNS.LOG_ERROR)
                next_at = time.monotonic() + interval
                time.sleep(interval)

    def _playout_tick(self, mix, next_at: float, interval: float) -> float:
        next_at += interval
        decoded: list[bytes] = []
        with self._peer_lock:
            peers = list(self._jitter.items())
        for peer_hex, buffer in peers:
            frame = buffer.pop()
            decoder = self._decoders.get(peer_hex)
            if decoder is None:
                continue
            if frame is None and buffer.depth() == 0:
                continue  # silent, not a mid-stream gap: skip PLC
            try:
                pcm = decoder.decode(frame)
            except Exception as e:
                RNS.log(f"TrenchChat [voice]: decode error: {e}",
                        RNS.LOG_DEBUG)
                continue
            # Opus returns as many samples as the packet actually held, so
            # a peer encoding at 10 ms yields half a frame. mix() sums
            # int16 arrays and raises on a length mismatch, and this loop
            # is the only thing driving playback.
            if len(pcm) != FRAME_PCM_BYTES:
                RNS.log(
                    f"TrenchChat [voice]: dropping a {len(pcm)}-byte frame "
                    f"from {peer_hex[:12]}… (expected {FRAME_PCM_BYTES})",
                    RNS.LOG_DEBUG,
                )
                continue
            decoded.append(pcm)
        if decoded:
            try:
                self._pcm_out.put_nowait(mix(decoded))
            except queue.Full:
                pass
            except Exception as e:
                RNS.log(f"TrenchChat [voice]: mix error: {e}", RNS.LOG_WARNING)
        delay = next_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        else:
            next_at = time.monotonic()
        return next_at


class TonePipeline:
    """Deviceless pipeline for headless workers and loopback tests.

    Transmits a 440 Hz tone through the real codec when one is available
    (falling back to synthetic byte frames), and counts received frames
    instead of playing them. The tone starts disabled; the testenv toggles
    it per worker.
    """

    def __init__(self, config, on_encoded, on_speaking_self):
        self._on_encoded = on_encoded
        self._on_speaking_self = on_speaking_self
        try:
            from trenchchat.core.audio.codec import OpusCodec
            self._encoder = OpusCodec()
        except Exception:
            self._encoder = None
        self._muted = False
        self._tone_enabled = False
        self._running = False
        self._thread = None
        self._seq = 0
        self._phase = 0.0
        self.rx_counts: dict[str, int] = {}

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="voice-tone")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def set_muted(self, muted: bool) -> None:
        self._muted = muted

    def set_tone_enabled(self, enabled: bool) -> None:
        self._tone_enabled = enabled
        if not enabled:
            try:
                self._on_speaking_self(False)
            except Exception:
                pass

    def play(self, peer_hex: str, seq: int, frames: list[bytes]) -> None:
        self.rx_counts[peer_hex] = self.rx_counts.get(peer_hex, 0) + len(frames)

    def drop_peer(self, peer_hex: str) -> None:
        self.rx_counts.pop(peer_hex, None)

    def _loop(self) -> None:
        interval = VOICE_FRAME_MS * VOICE_FRAMES_PER_PACKET / 1000.0
        while self._running:
            time.sleep(interval)
            if self._muted or not self._tone_enabled:
                continue
            frames = [self._next_frame()
                      for _ in range(VOICE_FRAMES_PER_PACKET)]
            seq = self._seq
            self._seq = (self._seq + len(frames)) % _SEQ_MODULUS
            try:
                self._on_speaking_self(True)
                self._on_encoded(seq, frames)
            except Exception as e:
                RNS.log(f"TrenchChat [voice]: tone emit error: {e}",
                        RNS.LOG_ERROR)

    def _next_frame(self) -> bytes:
        if self._encoder is None:
            return struct.pack("!H", self._seq % _SEQ_MODULUS) * 20
        step = 2.0 * math.pi * 440.0 / 48000.0
        samples = array.array("h")
        for _ in range(FRAME_SAMPLES):
            samples.append(int(12000 * math.sin(self._phase)))
            self._phase = (self._phase + step) % (2.0 * math.pi)
        return self._encoder.encode(samples.tobytes())


def make_tone_pipeline(config, on_encoded, on_speaking_self) -> TonePipeline:
    return TonePipeline(config, on_encoded, on_speaking_self)


def _voice_setting(config, key: str, default):
    if config is None:
        return default
    getter = getattr(config, f"voice_{key}", None)
    if getter is None:
        return default
    return getter
