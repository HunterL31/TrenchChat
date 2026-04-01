"""
Voice Activity Detection (VAD) filter for LXST audio pipelines.

Acts as an LXST-compatible filter that gates audio frames based on RMS energy.
When the signal energy is below the threshold, silence frames (zeros) are returned
so the Packetizer can skip sending them, saving bandwidth.

Usage with an LXST LineSource:
    vad = VadFilter(threshold_db=-40.0, hold_ms=300)
    source = LineSource(codec=codec, sink=packetizer, filters=[vad])
"""

import time
import threading

import numpy as np
import RNS


# Default threshold: -40 dBFS is a reasonable floor for voice in a quiet room.
VAD_DEFAULT_THRESHOLD_DB = -40.0
# Hold-open time in seconds: continue transmitting for this long after speech
# drops below threshold, to avoid clipping the end of words.
VAD_DEFAULT_HOLD_SECS = 0.30
# Sensitivity: minimum number of consecutive "active" samples before opening gate.
VAD_ATTACK_FRAMES = 1


class VadFilter:
    """Energy-threshold voice activity gate for use as an LXST filter.

    When instantiated and passed as a filter to an LXST LineSource, frames
    are evaluated per-frame. Frames above the threshold open the gate; frames
    below it close the gate after the hold period expires.

    A closed gate returns a zero-valued frame of the same shape, allowing the
    codec + Packetizer chain to remain intact while suppressing silent audio.
    """

    def __init__(self, threshold_db: float = VAD_DEFAULT_THRESHOLD_DB,
                 hold_ms: float = VAD_DEFAULT_HOLD_SECS * 1000):
        self._threshold_db = threshold_db
        self._threshold_linear = self._db_to_linear(threshold_db)
        self._hold_secs = hold_ms / 1000.0
        self._lock = threading.Lock()
        self._gate_open = False
        self._last_active_at: float | None = None
        self._speaking_callback: list = []

    @property
    def threshold_db(self) -> float:
        """Current energy threshold in dBFS."""
        return self._threshold_db

    @threshold_db.setter
    def threshold_db(self, value: float) -> None:
        with self._lock:
            self._threshold_db = value
            self._threshold_linear = self._db_to_linear(value)

    @property
    def gate_open(self) -> bool:
        """True if the VAD gate is currently open (speech detected)."""
        with self._lock:
            return self._gate_open

    def add_speaking_callback(self, callback) -> None:
        """Register a callback invoked with (speaking: bool) when gate state changes."""
        if callback not in self._speaking_callback:
            self._speaking_callback.append(callback)

    def remove_speaking_callback(self, callback) -> None:
        if callback in self._speaking_callback:
            self._speaking_callback.remove(callback)

    def handle_frame(self, frame: np.ndarray, samplerate: int) -> np.ndarray:
        """Evaluate a frame and return it unmodified if active, or silence if not.

        This is the LXST filter interface: called by LineSource for each captured
        audio frame before it reaches the codec.
        """
        rms = float(np.sqrt(np.mean(frame ** 2)))
        is_active = rms >= self._threshold_linear

        with self._lock:
            was_open = self._gate_open
            if is_active:
                self._last_active_at = time.monotonic()
                new_open = True
            elif self._last_active_at is not None:
                elapsed = time.monotonic() - self._last_active_at
                new_open = elapsed < self._hold_secs
            else:
                new_open = False

            self._gate_open = new_open
            state_changed = new_open != was_open

        if state_changed:
            self._fire_speaking_callbacks(new_open)

        if new_open:
            return frame
        # Return silence with the same shape so the codec sees consistent frames.
        return np.zeros_like(frame)

    def reset(self) -> None:
        """Reset VAD state. Call when stopping a voice session."""
        with self._lock:
            self._gate_open = False
            self._last_active_at = None

    def _fire_speaking_callbacks(self, speaking: bool) -> None:
        for cb in self._speaking_callback:
            try:
                cb(speaking)
            except Exception as exc:
                RNS.log(f"TrenchChat [vad]: speaking callback error: {exc}", RNS.LOG_ERROR)

    @staticmethod
    def _db_to_linear(db: float) -> float:
        """Convert a dBFS value to a linear amplitude ratio."""
        return 10 ** (db / 20.0)
