"""
Jitter buffer for one remote voice stream.

Pure Python, no third-party dependencies: reorders frames by 16-bit
sequence number, absorbs network jitter behind a small target depth, and
reports gaps as None so the caller can invoke codec packet-loss
concealment. After starvation the buffer refills to the target depth
before resuming, trading a little latency for a stretch of smooth audio.
"""

import threading

from trenchchat.network.voice_wire import SEQ_MODULUS as _SEQ_MODULUS
from trenchchat.network.voice_wire import seq_distance as _seq_distance

JITTER_TARGET_FRAMES = 4
JITTER_MAX_FRAMES = 25


class JitterBuffer:
    def __init__(self, target_frames: int = JITTER_TARGET_FRAMES,
                 max_frames: int = JITTER_MAX_FRAMES):
        self._target = target_frames
        self._max = max_frames
        self._lock = threading.Lock()
        self._frames: dict[int, bytes] = {}
        self._next_seq: int | None = None
        self._filling = True
        self._started = False

    def push(self, seq: int, frame: bytes) -> None:
        seq %= _SEQ_MODULUS
        with self._lock:
            if self._next_seq is not None:
                dist = _seq_distance(seq, self._next_seq)
                if dist < 0:
                    if self._started:
                        return  # older than the play cursor
                    # Nothing played yet: the first-seen frame wasn't the
                    # earliest. Rewind so playback starts from this one.
                    self._next_seq = seq
                if dist >= self._max:
                    # Far ahead of the cursor: the stream skipped (or we
                    # stalled). Restart around the new position.
                    self._frames.clear()
                    self._next_seq = seq
                    self._filling = True
            if seq in self._frames:
                return
            self._frames[seq] = frame
            if self._next_seq is None:
                self._next_seq = seq
            while len(self._frames) > self._max:
                oldest = min(self._frames,
                             key=lambda s: _seq_distance(s, self._next_seq))
                self._frames.pop(oldest)
                if oldest == self._next_seq:
                    self._next_seq = (self._next_seq + 1) % _SEQ_MODULUS

    def pop(self) -> bytes | None:
        """Next frame in sequence, or None for a gap (or while refilling)."""
        with self._lock:
            if self._next_seq is None:
                return None
            if self._filling:
                if len(self._frames) < self._target:
                    return None
                self._filling = False
            frame = self._frames.pop(self._next_seq, None)
            if frame is None and not self._frames:
                # Starved dry: hold the cursor and refill before resuming.
                self._filling = True
                return None
            self._next_seq = (self._next_seq + 1) % _SEQ_MODULUS
            if frame is not None:
                self._started = True
            return frame

    def reset(self) -> None:
        with self._lock:
            self._frames.clear()
            self._next_seq = None
            self._filling = True
            self._started = False

    def depth(self) -> int:
        with self._lock:
            return len(self._frames)
