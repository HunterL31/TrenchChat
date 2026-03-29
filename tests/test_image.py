"""
Unit tests for trenchchat.core.image.compress_image().
"""

import io

import pytest
from PIL import Image

from trenchchat.core.image import (
    compress_image,
    MAX_IMAGE_BYTES,
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
