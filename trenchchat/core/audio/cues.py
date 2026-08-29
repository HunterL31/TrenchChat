"""
Synthesized UI event sounds for voice sessions.

Two short two-tone blips — rising for a join, falling for a leave — built
in code so the repo carries no audio assets. Each cue is a list of
playout-sized PCM frames the pipeline mixes in with the live streams.
"""

import array
import math
from functools import lru_cache

from trenchchat.core.audio.engine import FRAME_PCM_BYTES

_SAMPLE_RATE = 48000
_NOTE_SECS = 0.07
_GAP_SECS = 0.02
_AMPLITUDE = 9000
_DECAY = 40.0

JOIN_NOTES = (660.0, 990.0)
LEAVE_NOTES = (990.0, 660.0)


def _tone(freq: float, secs: float) -> array.array:
    """One decaying sine note; the envelope gives it a percussive edge."""
    samples = array.array("h")
    step = 2.0 * math.pi * freq / _SAMPLE_RATE
    for i in range(int(secs * _SAMPLE_RATE)):
        envelope = math.exp(-_DECAY * i / _SAMPLE_RATE)
        samples.append(int(_AMPLITUDE * envelope * math.sin(step * i)))
    return samples


def _cue(notes: tuple[float, ...]) -> list[bytes]:
    samples = array.array("h")
    for i, freq in enumerate(notes):
        if i:
            samples.extend([0] * int(_GAP_SECS * _SAMPLE_RATE))
        samples.extend(_tone(freq, _NOTE_SECS))
    pcm = samples.tobytes()
    pcm += b"\x00" * (-len(pcm) % FRAME_PCM_BYTES)
    return [pcm[i:i + FRAME_PCM_BYTES]
            for i in range(0, len(pcm), FRAME_PCM_BYTES)]


@lru_cache(maxsize=None)
def join_cue() -> list[bytes]:
    """Rising blip played when someone enters the voice session."""
    return _cue(JOIN_NOTES)


@lru_cache(maxsize=None)
def leave_cue() -> list[bytes]:
    """Falling blip played when someone leaves it."""
    return _cue(LEAVE_NOTES)
