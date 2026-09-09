"""
A shaped path for the in-process fake transports.

The fake transports hand every packet over after a fixed delay, which is a
link no real user has. A ShapedPath puts the dev environment's own timing
model in front of that delivery instead: the same LinkProfile table the
scenario suite shapes real sockets with (devtools/testenv/link_profiles.py)
and the same schedule() that turns a profile into a delivery time, so a
number tuned here means the same thing there.

Two things a profile cannot say are added on top, because both are what
an ordinary internet path actually does to voice:

- Packets are timed independently, so a path whose jitter exceeds the
  40 ms spacing between voice packets delivers some of them out of order.
  The dev environment's shaper is a single queue and cannot reorder.
- stall() holds everything for a while and then releases it in one burst,
  which is a Wi-Fi retransmit storm, a mobile handover, or any hop whose
  queue briefly stops draining.
"""

import random
import sys
import threading
import time
from pathlib import Path

_TESTENV_DIR = Path(__file__).resolve().parents[1] / "devtools" / "testenv"
if str(_TESTENV_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTENV_DIR))

from link_profiles import LinkProfile, resolve  # noqa: E402
from link_shaper import schedule  # noqa: E402

# Consumer links, by the names link_profiles.py gives them.
HOME_FIBRE = "home_fibre"
HOME_WIFI = "home_wifi"
MOBILE_LTE = "mobile_lte"


class ShapedPath:
    """One direction of a shaped link, shared by every packet sent on it.

    Not thread-safe by accident: send_at() is called from whichever thread
    is transmitting, and the channel's busy-until time is shared state.
    """

    def __init__(self, profile: str | LinkProfile, seed: int = 0):
        self.profile = (profile if isinstance(profile, LinkProfile)
                        else resolve(profile))
        self.dropped = 0
        self.delivered = 0
        self._random = random.Random(f"trenchchat-path-{self.profile.name}-{seed}")
        self._lock = threading.Lock()
        self._free_at = 0.0
        self._held_until = 0.0

    def send_at(self, size_bytes: int) -> float | None:
        """When a packet of this size arrives, or None if the path ate it."""
        with self._lock:
            if self.profile.loss_pct > 0 and \
                    self._random.random() * 100.0 < self.profile.loss_pct:
                self.dropped += 1
                return None
            self.delivered += 1
            now = time.monotonic()
            jitter = (self._random.uniform(-1.0, 1.0)
                      if self.profile.jitter_ms > 0 else 0.0)
            deliver_at, self._free_at = schedule(
                now, self._free_at, size_bytes, self.profile, jitter)
            return max(deliver_at, self._held_until)

    def stall(self, secs: float) -> None:
        """Stop the path draining for a while; whatever was in flight, and
        whatever is sent meanwhile, arrives together when it clears."""
        with self._lock:
            self._held_until = max(self._held_until, time.monotonic() + secs)
