"""
Wire format for screen share updates, carried by direct sessions only.

The screen plane's counterpart of voice_wire.py: one fixed binary layout,
deliberately dependency-free, used unchanged on both hops (sharer to viewer
over the session, viewer's backend to its client over a WebSocket) so the
backend forwards bytes rather than re-encoding them.

Layout:
    SC_UPDATE : u8 version | u32 seq | u16 width | u16 height | u8 tile_shift |
                u8 kind | i16 cursor_x | i16 cursor_y | u16 count |
                count x (u16 tx | u16 ty | u32 len | len bytes of JPEG)

kind is KIND_TILES (each entry is one tile of the grid) or KIND_FULL (one entry
at (0, 0) covering the whole share). tile_shift names the tile edge as a power
of two. The grid is ceil(width / edge) by ceil(height / edge); a tile on the
right or bottom edge is whatever is left, so a tile's declared JPEG size is
fixed by its coordinates and checked against them before anything decodes it.
cursor is (-1, -1) when unknown.

Every reader states its limits: the peer on the other end is a member, and a
member is still assumed hostile.
"""

import struct
from dataclasses import dataclass, field

SCREEN_WIRE_VERSION = 1

KIND_TILES = 0
KIND_FULL = 1

# The tile edge as a power of two: 128 px by default, 32 to 256 accepted.
TILE_SHIFT = 7
MIN_TILE_SHIFT = 5
MAX_TILE_SHIFT = 8

# The largest share a node sends or accepts. A 4K screen is downscaled to fit.
MAX_SHARE_WIDTH = 1920
MAX_SHARE_HEIGHT = 1080

# One update, and one image within it. Under frames.MAX_FRAME_BYTES with room
# for the request around it; a 1080p full frame at the encoder's quality is a
# fraction of the update ceiling.
MAX_UPDATE_BYTES = 4 * 1024 * 1024
MAX_TILE_BYTES = 256 * 1024

CURSOR_UNKNOWN = (-1, -1)

_HEADER = "!BIHHBBhhH"
_HEADER_BYTES = struct.calcsize(_HEADER)
_ENTRY = "!HHI"
_ENTRY_BYTES = struct.calcsize(_ENTRY)

SEQ_MODULUS = 1 << 32

_JPEG_SOI = b"\xff\xd8"
# Start-of-frame markers, every JPEG process; each carries the dimensions.
_JPEG_SOF_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF})
_JPEG_STANDALONE = frozenset({0xD8, 0x01} | set(range(0xD0, 0xD8)))


@dataclass
class ScreenUpdate:
    """One update: what changed since the receiver last looked."""

    seq: int
    width: int
    height: int
    kind: int = KIND_TILES
    tile_shift: int = TILE_SHIFT
    cursor: tuple[int, int] = CURSOR_UNKNOWN
    entries: list[tuple[int, int, bytes]] = field(default_factory=list)

    @property
    def tile_edge(self) -> int:
        return 1 << self.tile_shift

    @property
    def payload_bytes(self) -> int:
        """The image bytes this update carries, without its framing."""
        return sum(len(data) for _tx, _ty, data in self.entries)


def grid_size(width: int, height: int, tile_shift: int) -> tuple[int, int]:
    """Columns and rows of the tile grid over a share of this size."""
    edge = 1 << tile_shift
    return (width + edge - 1) // edge, (height + edge - 1) // edge


def tile_rect(width: int, height: int, tile_shift: int,
              tx: int, ty: int) -> tuple[int, int, int, int]:
    """The pixel box (left, top, right, bottom) one tile covers."""
    edge = 1 << tile_shift
    left, top = tx * edge, ty * edge
    return left, top, min(left + edge, width), min(top + edge, height)


def pack_update(update: ScreenUpdate) -> bytes:
    """One update as bytes. Raises ValueError for anything over a limit."""
    _check_dimensions(update.width, update.height, update.tile_shift)
    if update.kind not in (KIND_TILES, KIND_FULL):
        raise ValueError("unknown update kind")
    cols, rows = grid_size(update.width, update.height, update.tile_shift)
    if update.kind == KIND_FULL and len(update.entries) != 1:
        raise ValueError("a full update carries exactly one image")
    if len(update.entries) > cols * rows:
        raise ValueError("more entries than the grid has tiles")
    cursor_x, cursor_y = update.cursor
    parts = [struct.pack(_HEADER, SCREEN_WIRE_VERSION, update.seq % SEQ_MODULUS,
                         update.width, update.height, update.tile_shift,
                         update.kind, cursor_x, cursor_y, len(update.entries))]
    size = _HEADER_BYTES
    for tx, ty, data in update.entries:
        if not data or len(data) > MAX_TILE_BYTES:
            raise ValueError("image size out of range")
        if update.kind == KIND_FULL:
            if (tx, ty) != (0, 0):
                raise ValueError("a full update sits at the origin")
        elif not (0 <= tx < cols and 0 <= ty < rows):
            raise ValueError("tile outside the grid")
        size += _ENTRY_BYTES + len(data)
        if size > MAX_UPDATE_BYTES:
            raise ValueError("update over the byte ceiling")
        parts.append(struct.pack(_ENTRY, tx, ty, len(data)))
        parts.append(data)
    return b"".join(parts)


def unpack_update(payload: bytes,
                  max_bytes: int = MAX_UPDATE_BYTES) -> ScreenUpdate:
    """One update off the wire, every field bounded. Raises ValueError.

    Checks the framing and the grid; check_update_images checks that each
    image declares the size its slot allows, which is the step before any
    decode.
    """
    if len(payload) > max_bytes:
        raise ValueError("update over the byte ceiling")
    if len(payload) < _HEADER_BYTES:
        raise ValueError("truncated update header")
    (version, seq, width, height, tile_shift, kind, cursor_x, cursor_y,
     count) = struct.unpack(_HEADER, payload[:_HEADER_BYTES])
    if version != SCREEN_WIRE_VERSION:
        raise ValueError(f"screen wire version {version} is unsupported")
    _check_dimensions(width, height, tile_shift)
    if kind not in (KIND_TILES, KIND_FULL):
        raise ValueError("unknown update kind")
    cols, rows = grid_size(width, height, tile_shift)
    if kind == KIND_FULL and count != 1:
        raise ValueError("a full update carries exactly one image")
    if count > cols * rows:
        raise ValueError("more entries than the grid has tiles")
    entries: list[tuple[int, int, bytes]] = []
    seen: set[tuple[int, int]] = set()
    offset = _HEADER_BYTES
    for _ in range(count):
        if offset + _ENTRY_BYTES > len(payload):
            raise ValueError("truncated update entry")
        tx, ty, size = struct.unpack(_ENTRY, payload[offset:offset + _ENTRY_BYTES])
        offset += _ENTRY_BYTES
        if size == 0 or size > MAX_TILE_BYTES:
            raise ValueError("image size out of range")
        if kind == KIND_FULL:
            if (tx, ty) != (0, 0):
                raise ValueError("a full update sits at the origin")
        elif not (tx < cols and ty < rows):
            raise ValueError("tile outside the grid")
        if (tx, ty) in seen:
            raise ValueError("a tile named twice")
        seen.add((tx, ty))
        data = payload[offset:offset + size]
        if len(data) != size:
            raise ValueError("truncated update image")
        offset += size
        entries.append((tx, ty, data))
    if offset != len(payload):
        raise ValueError("trailing bytes in update")
    cursor = (cursor_x, cursor_y)
    if cursor_x < 0 or cursor_y < 0:
        cursor = CURSOR_UNKNOWN
    return ScreenUpdate(seq=seq, width=width, height=height, kind=kind,
                        tile_shift=tile_shift, cursor=cursor, entries=entries)


def check_update_images(update: ScreenUpdate) -> None:
    """Refuse an update whose images do not declare the size their slot has.

    A JPEG that declares more than its slot would decode to more than the
    receiver reserved for it; one that declares less would leave a hole. Both
    are refused here, from the header alone, before any decoder runs.
    """
    for tx, ty, data in update.entries:
        declared = jpeg_dimensions(data)
        if declared is None:
            raise ValueError("an image that is not a JPEG")
        if update.kind == KIND_FULL:
            expected = (update.width, update.height)
        else:
            left, top, right, bottom = tile_rect(
                update.width, update.height, update.tile_shift, tx, ty)
            expected = (right - left, bottom - top)
        if declared != expected:
            raise ValueError(f"image at ({tx}, {ty}) declares {declared}, "
                             f"its slot is {expected}")


def jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """The width and height a JPEG declares, or None if it is not one.

    Walks the marker segments to the first start-of-frame; nothing is decoded.
    """
    if len(data) < 4 or data[:2] != _JPEG_SOI:
        return None
    offset = 2
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            return None
        marker = data[offset + 1]
        if marker == 0xFF:
            offset += 1
            continue
        if marker in _JPEG_STANDALONE:
            offset += 2
            continue
        segment = struct.unpack("!H", data[offset + 2:offset + 4])[0]
        if segment < 2:
            return None
        if marker in _JPEG_SOF_MARKERS:
            if offset + 9 > len(data):
                return None
            height, width = struct.unpack("!HH", data[offset + 5:offset + 9])
            if width == 0 or height == 0:
                return None
            return width, height
        if marker == 0xDA:
            return None
        offset += 2 + segment
    return None


def _check_dimensions(width: int, height: int, tile_shift: int) -> None:
    if not (0 < width <= MAX_SHARE_WIDTH and 0 < height <= MAX_SHARE_HEIGHT):
        raise ValueError("share size out of range")
    if not MIN_TILE_SHIFT <= tile_shift <= MAX_TILE_SHIFT:
        raise ValueError("tile size out of range")
