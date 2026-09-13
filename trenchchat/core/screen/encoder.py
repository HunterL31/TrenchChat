"""
Frames into updates: scale, split into tiles, keep what changed, JPEG it.

TileEncoder turns each captured frame into at most one ScreenUpdate: nothing
when the picture is the same as last time, the changed tiles when a few moved,
one full-frame image when most of them did, since a whole frame as tiles
costs about twice what it costs as one picture. There is one encoder for
every viewer, so a share runs at one size and one quality for all of them.

TileStore and Consumer are the coalescing that keeps every hop bounded. A
store holds the latest bytes per tile (or the latest full frame, which
supersedes them); a consumer remembers what one receiver has not seen yet.
next_update builds the one update that brings a consumer to the store's
current state, however many updates it missed: the sharer keeps a consumer
per viewer, and the viewer's backend keeps one per client socket.
"""

import io
import time
from dataclasses import dataclass, field

from PIL import Image

from trenchchat.network.screen_wire import (
    CURSOR_UNKNOWN, KIND_FULL, KIND_TILES, MAX_SHARE_HEIGHT, MAX_SHARE_WIDTH,
    ScreenUpdate, TILE_SHIFT, grid_size, tile_rect,
)

JPEG_QUALITY = 75

# Over this share of the grid changed in one tick, the tick is sent as one
# full-frame image rather than as tiles.
FULL_FRAME_THRESHOLD = 0.6

# The presets the picker offers: the share's largest size and its frame rate.
PRESET_CLEARER = "clearer"
PRESET_SMOOTHER = "smoother"
PRESETS = {
    PRESET_CLEARER: {"max_width": MAX_SHARE_WIDTH, "max_height": MAX_SHARE_HEIGHT,
                     "fps": 15},
    PRESET_SMOOTHER: {"max_width": 1280, "max_height": 720, "fps": 30},
}
DEFAULT_PRESET = PRESET_CLEARER

MIN_FPS = 1
MAX_FPS = 30


def share_size(source_width: int, source_height: int, max_width: int,
               max_height: int) -> tuple[int, int]:
    """The size a source is sent at: scaled down to fit, never up."""
    scale = min(1.0, max_width / source_width, max_height / source_height)
    return (max(1, round(source_width * scale)),
            max(1, round(source_height * scale)))


class TileEncoder:
    """Frames in, updates out, one share size and one quality for everyone."""

    def __init__(self, source_width: int, source_height: int, *,
                 max_width: int = MAX_SHARE_WIDTH,
                 max_height: int = MAX_SHARE_HEIGHT,
                 tile_shift: int = TILE_SHIFT, quality: int = JPEG_QUALITY,
                 full_frame_threshold: float = FULL_FRAME_THRESHOLD):
        self.source_size = (source_width, source_height)
        self.width, self.height = share_size(source_width, source_height,
                                             max_width, max_height)
        self.tile_shift = tile_shift
        self.quality = quality
        self.full_frame_threshold = full_frame_threshold
        self.cols, self.rows = grid_size(self.width, self.height, tile_shift)
        self.seq = 0
        self._last = None
        self.encode_secs = 0.0
        self.frames_in = 0
        self.updates_out = 0

    @property
    def size(self) -> tuple[int, int]:
        return self.width, self.height

    def encode(self, frame: Image.Image) -> ScreenUpdate | None:
        """The update this frame calls for, or None when nothing changed."""
        import numpy as np

        started = time.monotonic()
        self.frames_in += 1
        if frame.size != (self.width, self.height):
            frame = frame.resize((self.width, self.height), Image.BILINEAR)
        if frame.mode != "RGB":
            frame = frame.convert("RGB")
        pixels = self._padded(np.asarray(frame))
        edge = 1 << self.tile_shift
        blocks = pixels.reshape(self.rows, edge, self.cols, edge, 3)
        if self._last is None:
            changed = np.ones((self.rows, self.cols), dtype=bool)
        else:
            changed = np.any(blocks != self._last, axis=(1, 3, 4))
        self._last = blocks
        count = int(changed.sum())
        if count == 0:
            self.encode_secs += time.monotonic() - started
            return None
        self.seq += 1
        self.updates_out += 1
        if count >= self.full_frame_threshold * self.rows * self.cols:
            update = ScreenUpdate(seq=self.seq, width=self.width,
                                  height=self.height, kind=KIND_FULL,
                                  tile_shift=self.tile_shift,
                                  entries=[(0, 0, self._jpeg(frame))])
        else:
            entries = []
            for ty, tx in zip(*np.nonzero(changed)):
                box = tile_rect(self.width, self.height, self.tile_shift,
                                int(tx), int(ty))
                entries.append((int(tx), int(ty), self._jpeg(frame.crop(box))))
            update = ScreenUpdate(seq=self.seq, width=self.width,
                                  height=self.height, kind=KIND_TILES,
                                  tile_shift=self.tile_shift, entries=entries)
        self.encode_secs += time.monotonic() - started
        return update

    def _padded(self, pixels):
        """The frame padded out to whole tiles, so every block is one shape."""
        import numpy as np

        edge = 1 << self.tile_shift
        pad_h = self.rows * edge - self.height
        pad_w = self.cols * edge - self.width
        if pad_h == 0 and pad_w == 0:
            return pixels
        return np.pad(pixels, ((0, pad_h), (0, pad_w), (0, 0)))

    def _jpeg(self, image: Image.Image) -> bytes:
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=self.quality)
        return out.getvalue()

    def stats(self) -> dict:
        return {"frames_in": self.frames_in, "updates_out": self.updates_out,
                "encode_secs": round(self.encode_secs, 3),
                "width": self.width, "height": self.height}


class TileStore:
    """The latest bytes per tile, or the latest full frame that supersedes them.

    Bounded by construction: at most one image per grid slot plus one full
    frame, however long a share runs.
    """

    def __init__(self):
        self.width = 0
        self.height = 0
        self.tile_shift = TILE_SHIFT
        self.seq = 0
        self.cursor = CURSOR_UNKNOWN
        self.full: bytes | None = None
        self.tiles: dict[tuple[int, int], bytes] = {}
        self.bytes_applied = 0

    def apply(self, update: ScreenUpdate) -> None:
        """Take one update as the new current state."""
        if (update.width, update.height, update.tile_shift) != \
                (self.width, self.height, self.tile_shift):
            self.clear()
            self.width, self.height = update.width, update.height
            self.tile_shift = update.tile_shift
        self.seq = update.seq
        self.cursor = update.cursor
        self.bytes_applied += update.payload_bytes
        if update.kind == KIND_FULL:
            self.full = update.entries[0][2]
            self.tiles.clear()
            return
        for tx, ty, data in update.entries:
            self.tiles[(tx, ty)] = data

    def clear(self) -> None:
        self.full = None
        self.tiles.clear()
        self.seq = 0
        self.cursor = CURSOR_UNKNOWN

    @property
    def empty(self) -> bool:
        return self.full is None and not self.tiles


@dataclass
class Consumer:
    """What one receiver has not seen: a full frame first, then tiles."""

    needs_full: bool = True
    dirty: set[tuple[int, int]] = field(default_factory=set)

    def note(self, update: ScreenUpdate) -> None:
        """Record that this update happened without sending it."""
        if update.kind == KIND_FULL:
            self.needs_full = True
            self.dirty.clear()
            return
        for tx, ty, _data in update.entries:
            self.dirty.add((tx, ty))

    @property
    def behind(self) -> bool:
        return self.needs_full or bool(self.dirty)


def next_update(store: TileStore, consumer: Consumer) -> ScreenUpdate | None:
    """The one update that moves a consumer towards the store's state.

    A full frame comes first when one is owed; the tiles that changed after it
    follow in the next call. Marks the consumer as sent: the caller must put
    the update on the wire or hand the marks back with Consumer.note.
    """
    if consumer.needs_full:
        if store.full is None:
            consumer.needs_full = False
            consumer.dirty.update(store.tiles)
        else:
            consumer.needs_full = False
            return ScreenUpdate(seq=store.seq, width=store.width,
                                height=store.height, kind=KIND_FULL,
                                tile_shift=store.tile_shift, cursor=store.cursor,
                                entries=[(0, 0, store.full)])
    entries = [(tx, ty, store.tiles[(tx, ty)])
               for tx, ty in sorted(consumer.dirty) if (tx, ty) in store.tiles]
    consumer.dirty.clear()
    if not entries:
        return None
    return ScreenUpdate(seq=store.seq, width=store.width, height=store.height,
                        kind=KIND_TILES, tile_shift=store.tile_shift,
                        cursor=store.cursor, entries=entries)


def preset_settings(preset: str, fps: int | None = None) -> dict:
    """The size and rate a preset names, the rate overridable within bounds."""
    settings = dict(PRESETS.get(preset, PRESETS[DEFAULT_PRESET]))
    if fps is not None:
        settings["fps"] = max(MIN_FPS, min(MAX_FPS, int(fps)))
    return settings
