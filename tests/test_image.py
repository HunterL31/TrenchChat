"""
Unit tests for trenchchat.core.image: compress_image, is_gif, prepare_image.
"""

import io

import pytest
from PIL import Image

from trenchchat.core.image import (
    compress_image,
    compress_gif,
    is_gif,
    prepare_image,
    MAX_IMAGE_BYTES,
    MAX_GIF_BYTES,
    MAX_IMAGE_DIMENSION,
)


def _make_jpeg(width: int, height: int, color=(100, 150, 200)) -> bytes:
    """Return a minimal JPEG as bytes."""
    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _make_png(width: int, height: int) -> bytes:
    """Return a minimal PNG as bytes."""
    img = Image.new("RGB", (width, height), (200, 100, 50))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class TestCompressImage:
    def test_output_is_valid_jpeg(self):
        """compress_image always returns a valid JPEG."""
        data = _make_jpeg(100, 100)
        result = compress_image(data)
        img = Image.open(io.BytesIO(result))
        assert img.format == "JPEG"

    def test_output_within_size_limit(self):
        """Compressed output does not exceed MAX_IMAGE_BYTES."""
        data = _make_jpeg(800, 600)
        result = compress_image(data)
        assert len(result) <= MAX_IMAGE_BYTES

    def test_small_image_unchanged_dimensions(self):
        """Small images (< MAX_IMAGE_DIMENSION) keep their original dimensions."""
        data = _make_jpeg(200, 150)
        result = compress_image(data)
        img = Image.open(io.BytesIO(result))
        assert img.width == 200
        assert img.height == 150

    def test_oversized_image_resized(self):
        """Images larger than MAX_IMAGE_DIMENSION are resized."""
        large_w, large_h = 2000, 1500
        data = _make_jpeg(large_w, large_h)
        result = compress_image(data)
        img = Image.open(io.BytesIO(result))
        assert img.width <= MAX_IMAGE_DIMENSION
        assert img.height <= MAX_IMAGE_DIMENSION

    def test_aspect_ratio_preserved(self):
        """Resizing preserves the aspect ratio (within 1px rounding)."""
        data = _make_jpeg(2400, 1200)
        result = compress_image(data)
        img = Image.open(io.BytesIO(result))
        expected_ratio = 2.0
        actual_ratio = img.width / img.height
        assert abs(actual_ratio - expected_ratio) < 0.02

    def test_tall_image_resized(self):
        """Portrait images exceeding the limit are resized on the tall axis."""
        data = _make_jpeg(400, 1600)
        result = compress_image(data)
        img = Image.open(io.BytesIO(result))
        assert img.height <= MAX_IMAGE_DIMENSION

    def test_wide_image_resized(self):
        """Landscape images exceeding the limit are resized on the wide axis."""
        data = _make_jpeg(1600, 400)
        result = compress_image(data)
        img = Image.open(io.BytesIO(result))
        assert img.width <= MAX_IMAGE_DIMENSION

    def test_png_input_accepted(self):
        """PNG input is accepted and converted to JPEG output."""
        data = _make_png(300, 200)
        result = compress_image(data)
        img = Image.open(io.BytesIO(result))
        assert img.format == "JPEG"

    def test_square_image_at_limit(self):
        """An image exactly at MAX_IMAGE_DIMENSION passes through unchanged."""
        data = _make_jpeg(MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION)
        result = compress_image(data)
        img = Image.open(io.BytesIO(result))
        assert img.width == MAX_IMAGE_DIMENSION
        assert img.height == MAX_IMAGE_DIMENSION


# ---------------------------------------------------------------------------
# GIF detection
# ---------------------------------------------------------------------------

_GIF89_HEADER = b"GIF89a" + b"\x01\x00\x01\x00\x00\x00\x00" + b"\xff\xff\xff" + b"\x00" * 10 + b";"


def _make_gif(width: int = 1, height: int = 1, frames: int = 1) -> bytes:
    """Return a minimal GIF89a with the given dimensions and frame count."""
    first = Image.new("P", (width, height), 0)
    rest = [Image.new("P", (width, height), 0) for _ in range(frames - 1)]
    buf = io.BytesIO()
    first.save(buf, format="GIF", save_all=True, append_images=rest, loop=0, duration=100)
    return buf.getvalue()


def _make_large_gif(target_bytes: int = MAX_GIF_BYTES + 1) -> bytes:
    """Return a real multi-frame animated GIF that exceeds target_bytes.

    Uses noise-filled frames so data doesn't compress away, and pre-computes a
    per-frame target so we can stop as soon as we exceed the threshold without
    encoding the whole GIF on every iteration.
    """
    import random

    frame_size = (200, 200)
    n_pixels = frame_size[0] * frame_size[1]

    frames: list[Image.Image] = []
    while True:
        # Random noise: each pixel gets a random palette index, resisting LZW compression
        noise = bytes(random.randint(0, 255) for _ in range(n_pixels))
        frame = Image.frombytes("P", frame_size, noise)
        frames.append(frame)

        buf = io.BytesIO()
        frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:],
                       loop=0, duration=100)
        if len(buf.getvalue()) >= target_bytes:
            return buf.getvalue()


class TestIsGif:
    def test_gif89a_detected(self):
        """GIF89a magic bytes are recognised."""
        assert is_gif(b"GIF89a" + b"\x00" * 20)

    def test_gif87a_detected(self):
        """GIF87a magic bytes are recognised."""
        assert is_gif(b"GIF87a" + b"\x00" * 20)

    def test_jpeg_not_gif(self):
        """JPEG is not detected as GIF."""
        assert not is_gif(b"\xff\xd8\xff\xe0" + b"\x00" * 20)

    def test_png_not_gif(self):
        """PNG is not detected as GIF."""
        assert not is_gif(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)

    def test_empty_not_gif(self):
        """Empty bytes are not detected as GIF."""
        assert not is_gif(b"")

    def test_real_gif_detected(self):
        """A real GIF generated by PIL is detected correctly."""
        assert is_gif(_make_gif())


# ---------------------------------------------------------------------------
# prepare_image
# ---------------------------------------------------------------------------

class TestPrepareImage:
    def test_jpeg_is_compressed_not_gif(self):
        """prepare_image compresses JPEGs and returns gif=False."""
        data = _make_jpeg(100, 100)
        result, gif_flag = prepare_image(data)
        assert not gif_flag
        img = Image.open(io.BytesIO(result))
        assert img.format == "JPEG"

    def test_png_is_compressed_not_gif(self):
        """prepare_image compresses PNGs (converting to JPEG) and returns gif=False."""
        data = _make_png(100, 100)
        result, gif_flag = prepare_image(data)
        assert not gif_flag
        img = Image.open(io.BytesIO(result))
        assert img.format == "JPEG"

    def test_gif_passes_through_unchanged_when_small(self):
        """A small GIF is returned unchanged by prepare_image."""
        data = _make_gif()
        assert len(data) < MAX_GIF_BYTES
        result, gif_flag = prepare_image(data)
        assert gif_flag
        assert result == data

    def test_gif_flag_is_true(self):
        """prepare_image sets gif=True for GIF input."""
        data = _make_gif()
        _, gif_flag = prepare_image(data)
        assert gif_flag

    def test_oversized_gif_is_compressed_not_raised(self):
        """A GIF exceeding MAX_GIF_BYTES is scaled down, not rejected."""
        data = _make_large_gif()
        assert len(data) > MAX_GIF_BYTES
        result, gif_flag = prepare_image(data)
        assert gif_flag
        assert len(result) <= MAX_GIF_BYTES

    def test_compressed_gif_is_still_valid_gif(self):
        """The result of compressing an oversized GIF can be read back by PIL."""
        data = _make_large_gif()
        result, _ = prepare_image(data)
        img = Image.open(io.BytesIO(result))
        assert img.format == "GIF"

    def test_jpeg_result_within_size_limit(self):
        """Compressed JPEG from prepare_image stays within MAX_IMAGE_BYTES."""
        data = _make_jpeg(800, 600)
        result, _ = prepare_image(data)
        assert len(result) <= MAX_IMAGE_BYTES


# ---------------------------------------------------------------------------
# compress_gif
# ---------------------------------------------------------------------------

class TestCompressGif:
    def test_small_gif_returned_unchanged(self):
        """compress_gif fast-paths GIFs already within MAX_GIF_BYTES."""
        data = _make_gif(10, 10, frames=3)
        result = compress_gif(data)
        assert result == data

    def test_oversized_gif_fits_after_compression(self):
        """compress_gif reduces an oversized GIF to fit within MAX_GIF_BYTES."""
        data = _make_large_gif()
        result = compress_gif(data)
        assert len(result) <= MAX_GIF_BYTES

    def test_result_is_valid_gif(self):
        """The compressed output is a valid GIF PIL can open."""
        data = _make_large_gif()
        result = compress_gif(data)
        img = Image.open(io.BytesIO(result))
        assert img.format == "GIF"

    def test_all_frames_preserved(self):
        """Frame count is preserved after compression (using a real multi-frame GIF)."""
        # Build a real animated GIF using distinct solid colours so frames
        # survive the RGBA round-trip inside compress_gif without collapsing.
        colours = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (0, 255, 255)]
        pil_frames = [
            Image.new("RGB", (400, 400), c).convert("P", palette=Image.ADAPTIVE)
            for c in colours * 20  # 100 frames — enough to push past MAX_GIF_BYTES
        ]
        buf = io.BytesIO()
        pil_frames[0].save(buf, format="GIF", save_all=True, append_images=pil_frames[1:],
                           loop=0, duration=100)
        data = buf.getvalue()

        n_original = Image.open(io.BytesIO(data)).n_frames
        result = compress_gif(data)
        n_compressed = Image.open(io.BytesIO(result)).n_frames
        assert n_compressed == n_original
