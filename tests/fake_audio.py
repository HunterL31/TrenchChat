"""
A deviceless stand-in for sounddevice, so a real AudioPipeline can run in
a test: a scripted microphone and a recording speaker.

AudioPipeline imports sounddevice inside start(), so putting one of these
in sys.modules gives a pipeline hardware it can open. The capture stream
hands it 20 ms blocks of a supplied signal on a real clock once speak()
opens the microphone (silence before that, and once the signal runs out),
and the playback stream keeps every block the pipeline writes, which is
what a listener would have heard.

Both streams run on the same anchored cadence PortAudio would drive them
at, so what a test measures includes the pipeline's own timing: a stream
that starves, or a playout thread that falls behind, records as the
silence it would have been.
"""

import threading
import time

SAMPLE_RATE = 48000
BLOCK_SECS = 0.02
SAMPLE_BYTES = 2


class FakeAudioDevices:
    """Put an instance in sys.modules["sounddevice"] for the duration of a
    test; every stream a pipeline opens is recorded in `streams`."""

    def __init__(self, capture: bytes = b""):
        self.capture = capture
        self.streams: list[_FakeStream] = []

    def RawInputStream(self, device=None, blocksize=960, callback=None,
                       **kwargs) -> "_FakeStream":
        return self._open("input", device, blocksize, callback)

    def RawOutputStream(self, device=None, blocksize=960, callback=None,
                        **kwargs) -> "_FakeStream":
        return self._open("output", device, blocksize, callback)

    def query_devices(self) -> list[dict]:
        return [{"name": "Fake Microphone", "max_input_channels": 1,
                 "max_output_channels": 0},
                {"name": "Fake Speakers", "max_input_channels": 0,
                 "max_output_channels": 2}]

    def playback_of(self, pipeline) -> "_FakeStream":
        """The playback stream a given pipeline opened."""
        return self._stream_of(pipeline, "output")

    def capture_of(self, pipeline) -> "_FakeStream":
        """The capture stream a given pipeline opened."""
        return self._stream_of(pipeline, "input")

    def _open(self, kind: str, device, blocksize: int,
              callback) -> "_FakeStream":
        stream = _FakeStream(self, kind, device, blocksize, callback)
        self.streams.append(stream)
        return stream

    def _stream_of(self, pipeline, kind: str) -> "_FakeStream":
        for stream in self.streams:
            if stream.kind == kind and stream.owner is pipeline:
                return stream
        raise LookupError(f"no {kind} stream for {pipeline!r}")


class _FakeStream:
    """One direction of a fake device, clocked on its own thread."""

    def __init__(self, devices: FakeAudioDevices, kind: str, device,
                 blocksize: int, callback):
        self.kind = kind
        self.device = device
        self.active = False
        self.owner = getattr(callback, "__self__", None)
        self.recorded = bytearray()
        self._feeding = False
        self._devices = devices
        self._callback = callback
        self._block_bytes = blocksize * SAMPLE_BYTES
        self._offset = 0
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.active = True
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name=f"fake-audio-{self.kind}")
        self._thread.start()

    def stop(self) -> None:
        self.active = False
        self._stopped.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def close(self) -> None:
        self.stop()

    def played(self) -> bytes:
        """Everything written to this playback stream so far."""
        return bytes(self.recorded)

    def speak(self) -> None:
        """Open the microphone: feed the capture signal from here on.

        A session joins before anyone talks, and the capture signal is
        finite, so a stream that started feeding at open would be half
        spent by the time the links are up.
        """
        self._feeding = True

    @property
    def position_secs(self) -> float:
        """How far into the capture signal the microphone has read."""
        return self._offset / SAMPLE_BYTES / SAMPLE_RATE

    @property
    def exhausted(self) -> bool:
        """True once the whole capture signal has been handed over."""
        return self._feeding and self._offset >= len(self._devices.capture)

    def _loop(self) -> None:
        next_at = time.monotonic()
        while not self._stopped.is_set():
            try:
                self._tick()
            except Exception:
                self.active = False
                raise
            next_at += BLOCK_SECS
            delay = next_at - time.monotonic()
            if delay > 0:
                self._stopped.wait(delay)
            else:
                next_at = time.monotonic()

    def _tick(self) -> None:
        frames = self._block_bytes // SAMPLE_BYTES
        if self.kind == "input":
            block = b""
            if self._feeding:
                block = self._devices.capture[
                    self._offset:self._offset + self._block_bytes]
                self._offset += self._block_bytes
            self._callback(block.ljust(self._block_bytes, b"\x00"), frames,
                           None, None)
            return
        block = bytearray(self._block_bytes)
        self._callback(memoryview(block), frames, None, None)
        self.recorded += block
