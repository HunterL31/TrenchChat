"""Mix decoded 16-bit mono PCM frames from multiple senders into one."""

import numpy as np


def mix(frames: list[bytes]) -> bytes:
    """Sum int16 PCM frames with saturation. Frames must be equal length."""
    if not frames:
        return b""
    if len(frames) == 1:
        return frames[0]
    acc = np.zeros(len(frames[0]) // 2, dtype=np.int32)
    for frame in frames:
        acc += np.frombuffer(frame, dtype=np.int16).astype(np.int32)
    return np.clip(acc, -32768, 32767).astype(np.int16).tobytes()
