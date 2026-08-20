"""Mix decoded 16-bit mono PCM frames from multiple senders into one."""

import numpy as np


def mix(frames: list[bytes]) -> bytes:
    """Sum int16 PCM frames with saturation.

    Frames of a length other than the first are skipped rather than summed:
    numpy raises on a mismatch, and a peer choosing a shorter Opus frame
    duration is enough to produce one.
    """
    if not frames:
        return b""
    if len(frames) == 1:
        return frames[0]
    width = len(frames[0])
    acc = np.zeros(width // 2, dtype=np.int32)
    for frame in frames:
        if len(frame) != width:
            continue
        acc += np.frombuffer(frame, dtype=np.int16).astype(np.int32)
    return np.clip(acc, -32768, 32767).astype(np.int16).tobytes()
