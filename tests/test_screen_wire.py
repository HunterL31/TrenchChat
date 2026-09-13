"""
The screen share wire format: what one update carries and what a reader refuses.

Every bound in network/screen_wire.py is exercised from the outside, the way
a hostile member would reach it: a blob that parses as an update and then
lies about its images is refused before any decoder sees it.
"""

import io
import struct

import pytest
from PIL import Image

from trenchchat.network.screen_wire import (
    CURSOR_UNKNOWN, KIND_FULL, KIND_TILES, MAX_SHARE_HEIGHT, MAX_SHARE_WIDTH,
    MAX_TILE_BYTES, MAX_TILE_SHIFT, MAX_UPDATE_BYTES, MIN_TILE_SHIFT,
    SCREEN_WIRE_VERSION, ScreenUpdate, TILE_SHIFT, check_update_images,
    grid_size, jpeg_dimensions, pack_update, tile_rect, unpack_update,
)


def jpeg(width: int, height: int, colour=(20, 120, 200)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(out, format="JPEG", quality=60)
    return out.getvalue()


def test_a_tile_update_round_trips():
    update = ScreenUpdate(seq=9, width=300, height=200, cursor=(12, 34),
                          entries=[(0, 0, jpeg(128, 128)), (2, 1, jpeg(44, 72))])
    back = unpack_update(pack_update(update))
    assert (back.seq, back.width, back.height) == (9, 300, 200)
    assert back.kind == KIND_TILES and back.tile_shift == TILE_SHIFT
    assert back.cursor == (12, 34)
    assert back.entries == update.entries
    check_update_images(back)


def test_a_full_update_round_trips_and_declares_the_share_size():
    update = ScreenUpdate(seq=1, width=200, height=100, kind=KIND_FULL,
                          entries=[(0, 0, jpeg(200, 100))])
    back = unpack_update(pack_update(update))
    assert back.kind == KIND_FULL and len(back.entries) == 1
    check_update_images(back)


def test_an_unknown_cursor_reads_as_unknown():
    update = ScreenUpdate(seq=1, width=64, height=64,
                          entries=[(0, 0, jpeg(64, 64))])
    assert unpack_update(pack_update(update)).cursor == CURSOR_UNKNOWN


def test_the_grid_and_edge_tiles():
    assert grid_size(300, 200, TILE_SHIFT) == (3, 2)
    assert tile_rect(300, 200, TILE_SHIFT, 2, 1) == (256, 128, 300, 200)
    assert tile_rect(300, 200, TILE_SHIFT, 0, 0) == (0, 0, 128, 128)


def test_the_sequence_wraps_at_32_bits():
    update = ScreenUpdate(seq=(1 << 32) + 5, width=64, height=64,
                          entries=[(0, 0, jpeg(64, 64))])
    assert unpack_update(pack_update(update)).seq == 5


class TestPackRefuses:
    def test_a_share_over_the_size_ceiling(self):
        with pytest.raises(ValueError):
            pack_update(ScreenUpdate(seq=1, width=MAX_SHARE_WIDTH + 1,
                                     height=100, entries=[]))
        with pytest.raises(ValueError):
            pack_update(ScreenUpdate(seq=1, width=100,
                                     height=MAX_SHARE_HEIGHT + 1, entries=[]))

    def test_a_tile_outside_the_grid(self):
        with pytest.raises(ValueError):
            pack_update(ScreenUpdate(seq=1, width=200, height=100,
                                     entries=[(2, 0, jpeg(8, 8))]))

    def test_a_tile_shift_outside_the_range(self):
        for shift in (MIN_TILE_SHIFT - 1, MAX_TILE_SHIFT + 1):
            with pytest.raises(ValueError):
                pack_update(ScreenUpdate(seq=1, width=64, height=64,
                                         tile_shift=shift, entries=[]))

    def test_an_image_over_the_tile_ceiling(self):
        with pytest.raises(ValueError):
            pack_update(ScreenUpdate(seq=1, width=64, height=64,
                                     entries=[(0, 0, b"x" * (MAX_TILE_BYTES + 1))]))

    def test_an_update_over_the_byte_ceiling(self):
        entries = [(tx, ty, b"x" * MAX_TILE_BYTES)
                   for ty in range(9) for tx in range(15)]
        assert len(entries) * MAX_TILE_BYTES > MAX_UPDATE_BYTES
        with pytest.raises(ValueError):
            pack_update(ScreenUpdate(seq=1, width=MAX_SHARE_WIDTH,
                                     height=MAX_SHARE_HEIGHT, entries=entries))

    def test_a_full_update_with_two_images(self):
        with pytest.raises(ValueError):
            pack_update(ScreenUpdate(seq=1, width=64, height=64, kind=KIND_FULL,
                                     entries=[(0, 0, jpeg(64, 64)),
                                              (0, 0, jpeg(64, 64))]))


class TestUnpackRefuses:
    def _header(self, **overrides) -> bytes:
        fields = {"version": SCREEN_WIRE_VERSION, "seq": 1, "width": 64,
                  "height": 64, "shift": TILE_SHIFT, "kind": KIND_TILES,
                  "cx": -1, "cy": -1, "count": 0}
        fields.update(overrides)
        return struct.pack("!BIHHBBhhH", *fields.values())

    def test_another_version(self):
        with pytest.raises(ValueError):
            unpack_update(self._header(version=SCREEN_WIRE_VERSION + 1))

    def test_a_truncated_header(self):
        with pytest.raises(ValueError):
            unpack_update(self._header()[:-1])

    def test_a_truncated_entry(self):
        blob = self._header(count=1) + struct.pack("!HHI", 0, 0, 100) + b"x" * 50
        with pytest.raises(ValueError):
            unpack_update(blob)

    def test_trailing_bytes(self):
        blob = pack_update(ScreenUpdate(seq=1, width=64, height=64,
                                        entries=[(0, 0, jpeg(64, 64))]))
        with pytest.raises(ValueError):
            unpack_update(blob + b"\x00")

    def test_a_blob_over_the_ceiling_is_refused_before_parsing(self):
        with pytest.raises(ValueError):
            unpack_update(b"\x00" * (MAX_UPDATE_BYTES + 1))

    def test_a_tile_named_twice(self):
        data = jpeg(64, 64)
        blob = self._header(count=2) + struct.pack("!HHI", 0, 0, len(data)) + \
            data + struct.pack("!HHI", 0, 0, len(data)) + data
        with pytest.raises(ValueError):
            unpack_update(blob)

    def test_an_empty_image(self):
        blob = self._header(count=1) + struct.pack("!HHI", 0, 0, 0)
        with pytest.raises(ValueError):
            unpack_update(blob)

    def test_a_tile_outside_the_grid(self):
        data = jpeg(64, 64)
        blob = self._header(count=1) + struct.pack("!HHI", 5, 0, len(data)) + data
        with pytest.raises(ValueError):
            unpack_update(blob)


class TestImageChecks:
    def test_a_tile_declaring_more_than_its_slot_is_refused(self):
        update = ScreenUpdate(seq=1, width=200, height=100,
                              entries=[(1, 0, jpeg(128, 100))])
        with pytest.raises(ValueError):
            check_update_images(update)

    def test_a_tile_declaring_less_than_its_slot_is_refused(self):
        update = ScreenUpdate(seq=1, width=200, height=100,
                              entries=[(0, 0, jpeg(64, 64))])
        with pytest.raises(ValueError):
            check_update_images(update)

    def test_a_full_frame_declaring_the_wrong_size_is_refused(self):
        update = ScreenUpdate(seq=1, width=200, height=100, kind=KIND_FULL,
                              entries=[(0, 0, jpeg(199, 100))])
        with pytest.raises(ValueError):
            check_update_images(update)

    def test_bytes_that_are_not_a_jpeg_are_refused(self):
        update = ScreenUpdate(seq=1, width=64, height=64,
                              entries=[(0, 0, b"\x89PNG" + b"\x00" * 40)])
        with pytest.raises(ValueError):
            check_update_images(update)

    def test_jpeg_dimensions_reads_the_header_only(self):
        data = jpeg(37, 91)
        assert jpeg_dimensions(data) == (37, 91)
        # The scan is after the frame header; cutting it off changes nothing.
        assert jpeg_dimensions(data[:len(data) // 2]) == (37, 91)
        assert jpeg_dimensions(b"") is None
        assert jpeg_dimensions(b"\xff\xd8\xff") is None
        # A progressive JPEG carries SOF2 and reads the same.
        out = io.BytesIO()
        Image.new("RGB", (37, 91)).save(out, format="JPEG", progressive=True)
        assert jpeg_dimensions(out.getvalue()) == (37, 91)
