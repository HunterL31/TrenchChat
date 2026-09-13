"""
A monotonic tick schedule for a periodic thread.

Shared by the audio pipelines and the screen capture loop. Sleeps only what is
left of the interval after the cycle's work, and re-anchors on an overrun
rather than sleeping negative or bursting to catch up: sleeping the whole
interval and then working makes the real period interval plus work, which
drifts without bound, and a sender even a few percent slow drains every
listener's buffer.
"""

import time


class Cadence:
    """Call wait() once per cycle; it returns when the next tick is due."""

    def __init__(self, interval: float):
        self._interval = interval
        self._next_at = time.monotonic()

    def wait(self) -> None:
        self._next_at += self._interval
        delay = self._next_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        else:
            self._next_at = time.monotonic()
