"""
Where the pixels come from: a monitor through mss, or a script in tests.

mss talks to the display server directly (GDI, CoreGraphics, X11) with no
compiled extension. It captures monitors and regions rather than windows, and
not the cursor. On an X11 display its handle belongs to the thread that made
it, so a source opens itself on the capture thread and never before.

Wayland has no X11 screen to read; mss fails to connect and the probe names
the session type, so a user reads "unavailable on Wayland" rather than a
black share.
"""

import io
import os
import threading
from abc import ABC, abstractmethod

import RNS
from PIL import Image

# The picker's thumbnail: one grab per monitor, scaled to this longest edge.
THUMBNAIL_EDGE = 320
THUMBNAIL_QUALITY = 60

# mss's monitor 0 is the union of every display; real monitors start at 1.
_FIRST_MONITOR = 1

_probe_lock = threading.Lock()
_probe_result: tuple[bool, str] | None = None


class ScreenSource(ABC):
    """One thing to capture, opened on the thread that will grab from it."""

    @abstractmethod
    def open(self) -> tuple[int, int]:
        """Prepare the source and return its size."""

    @abstractmethod
    def grab(self) -> Image.Image:
        """The current picture, as an RGB image at the source's size."""

    @abstractmethod
    def close(self) -> None:
        """Release the source."""


class MonitorSource(ScreenSource):
    """One physical monitor, read through mss."""

    def __init__(self, monitor: int):
        self.monitor = int(monitor)
        self._mss = None
        self._geometry: dict | None = None

    def open(self) -> tuple[int, int]:
        import mss

        self._mss = mss.MSS()
        monitors = self._mss.monitors
        if not _FIRST_MONITOR <= self.monitor < len(monitors):
            self.close()
            raise ValueError(f"no monitor {self.monitor}")
        self._geometry = monitors[self.monitor]
        return self._geometry["width"], self._geometry["height"]

    def grab(self) -> Image.Image:
        if self._mss is None or self._geometry is None:
            raise RuntimeError("source is not open")
        shot = self._mss.grab(self._geometry)
        return Image.frombuffer("RGBA", shot.size, shot.bgra, "raw", "BGRA",
                                0, 1).convert("RGB")

    def close(self) -> None:
        if self._mss is not None:
            try:
                self._mss.close()
            except Exception as e:
                RNS.log(f"TrenchChat [screen]: closing the capture: {e}",
                        RNS.LOG_DEBUG)
        self._mss = None
        self._geometry = None


class ScriptedSource(ScreenSource):
    """A source that plays back given frames, for tests and headless testers.

    frames is a list of RGB images all of one size, or a callable returning
    the next one. A list's last frame repeats once it is exhausted, which is
    what a static screen looks like.
    """

    def __init__(self, frames):
        self._next = frames if callable(frames) else None
        self._frames = [] if callable(frames) else list(frames)
        self._index = 0
        self._pending: Image.Image | None = None
        self.grabs = 0

    def open(self) -> tuple[int, int]:
        if self._next is not None:
            self._pending = self._next()
            return self._pending.size
        return self._frames[0].size

    def grab(self) -> Image.Image:
        self.grabs += 1
        if self._next is not None:
            frame, self._pending = self._pending, None
            return frame if frame is not None else self._next()
        frame = self._frames[min(self._index, len(self._frames) - 1)]
        self._index += 1
        return frame

    def close(self) -> None:
        pass


class MovingBoxSource(ScriptedSource):
    """A headless tester's screen: a small picture with a box that moves.

    Every grab differs from the last in one tile, so a share always has
    something to send and a viewer can tell frames apart, and the picture is
    small enough that a tester encodes it in a millisecond.
    """

    WIDTH = 320
    HEIGHT = 200

    def __init__(self, monitor: int = 1):
        self._tick = 0
        self._tint = int(monitor) % 200
        super().__init__(self._next_frame)

    def _next_frame(self) -> Image.Image:
        from PIL import ImageDraw

        frame = Image.new("RGB", (self.WIDTH, self.HEIGHT), (20 + self._tint, 24, 30))
        offset = (self._tick * 7) % (self.WIDTH - 20)
        ImageDraw.Draw(frame).rectangle((offset, 40, offset + 12, 52),
                                        fill=(220, 60, 60))
        self._tick += 1
        return frame


def probe_capture() -> tuple[bool, str]:
    """Whether a screen can be captured here. Probed once, on demand."""
    global _probe_result
    with _probe_lock:
        if _probe_result is None:
            _probe_result = _probe()
        return _probe_result


def _probe() -> tuple[bool, str]:
    try:
        import mss
    except Exception as e:
        return False, f"mss unavailable: {e}"
    try:
        with mss.MSS() as grabber:
            if len(grabber.monitors) <= _FIRST_MONITOR:
                return False, "no monitor to capture"
    except Exception as e:
        if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
            return False, "screen capture is unavailable on Wayland"
        return False, f"screen capture unavailable: {e}"
    return True, ""


def list_monitors() -> dict:
    """The monitors a user may share, with the probe's answer when there are none."""
    available, reason = probe_capture()
    if not available:
        return {"available": False, "reason": reason, "monitors": []}
    import mss

    try:
        with mss.MSS() as grabber:
            monitors = grabber.monitors[_FIRST_MONITOR:]
    except Exception as e:
        return {"available": False, "reason": str(e), "monitors": []}
    return {"available": True, "reason": "", "monitors": [
        {"index": index, "width": m["width"], "height": m["height"],
         "left": m["left"], "top": m["top"]}
        for index, m in enumerate(monitors, start=_FIRST_MONITOR)]}


def monitor_thumbnail(monitor: int) -> bytes | None:
    """One small JPEG of a monitor for the picker, grabbed now and kept nowhere."""
    source = MonitorSource(monitor)
    try:
        source.open()
        image = source.grab()
    except Exception as e:
        RNS.log(f"TrenchChat [screen]: thumbnail of monitor {monitor} failed: {e}",
                RNS.LOG_DEBUG)
        return None
    finally:
        source.close()
    image.thumbnail((THUMBNAIL_EDGE, THUMBNAIL_EDGE))
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=THUMBNAIL_QUALITY)
    return out.getvalue()
