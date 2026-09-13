"""
Phase 0 spike: what a 1080p tile encode costs in Python, per workload.

Three synthetic workloads stand in for the recorded ones the plan named: a
desktop with typing (one small region changes per tick), a scrolling document
(the whole picture shifts by a few rows), and full-motion video in a window
(a large region of noise). For each, at two tile sizes, the script reports
milliseconds per tick for the diff and encode and bytes on the wire per second
at 15 frames a second. Throwaway: nothing under trenchchat/ imports it.

    .venv/bin/python devtools/spikes/screen/encode_cost.py
"""

import time

import numpy as np
from PIL import Image, ImageDraw

from trenchchat.core.screen.encoder import TileEncoder

WIDTH, HEIGHT = 1920, 1080
TICKS = 30
FPS = 15


def desktop() -> Image.Image:
    rng = np.random.default_rng(1)
    base = Image.new("RGB", (WIDTH, HEIGHT), (36, 38, 46))
    draw = ImageDraw.Draw(base)
    for y in range(40, HEIGHT, 22):
        draw.text((60, y), "def encode(self, frame): " * 6, fill=(200, 200, 210))
    noise = rng.integers(0, 40, (200, 300, 3), dtype=np.uint8)
    base.paste(Image.fromarray(noise), (1500, 800))
    return base


def typing(frame: Image.Image, tick: int) -> Image.Image:
    edited = frame.copy()
    ImageDraw.Draw(edited).text((60 + tick * 9, 500), "x", fill=(255, 255, 255))
    return edited


def scrolling(frame: Image.Image, tick: int) -> Image.Image:
    pixels = np.asarray(frame)
    return Image.fromarray(np.roll(pixels, -3 * (tick + 1), axis=0))


def video(frame: Image.Image, tick: int) -> Image.Image:
    rng = np.random.default_rng(tick)
    edited = frame.copy()
    noise = rng.integers(0, 255, (720, 1280, 3), dtype=np.uint8)
    edited.paste(Image.fromarray(noise), (320, 180))
    return edited


def measure(name, mutate, tile_shift: int) -> None:
    base = desktop()
    encoder = TileEncoder(WIDTH, HEIGHT, tile_shift=tile_shift)
    encoder.encode(base)
    total_bytes = 0
    started = time.monotonic()
    for tick in range(TICKS):
        update = encoder.encode(mutate(base, tick))
        if update is not None:
            total_bytes += update.payload_bytes
    elapsed = time.monotonic() - started
    per_tick_ms = elapsed / TICKS * 1000
    print(f"{name:10s} tile {1 << tile_shift:3d}: {per_tick_ms:6.1f} ms/tick, "
          f"{total_bytes / TICKS / 1024:7.1f} KB/update, "
          f"{total_bytes / TICKS * FPS / 1024 / 1024 * 8:6.2f} Mbit/s at {FPS} fps")


if __name__ == "__main__":
    for shift in (6, 7):
        for name, mutate in (("typing", typing), ("scrolling", scrolling),
                             ("video", video)):
            measure(name, mutate, shift)
