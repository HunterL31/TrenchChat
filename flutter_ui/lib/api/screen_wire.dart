// The screen share update as it arrives on the watch socket: the same bytes
// the direct session carried (trenchchat/network/screen_wire.py), forwarded
// by the backend without re-encoding. One fixed binary layout, parsed here
// with every bound stated, so the client needs no msgpack and never decodes
// an image it has not measured first.
//
//   u8 version | u32 seq | u16 width | u16 height | u8 tile_shift | u8 kind |
//   i16 cursor_x | i16 cursor_y | u16 count |
//   count x (u16 tx | u16 ty | u32 len | len bytes of JPEG)
import 'dart:typed_data';

const int screenWireVersion = 1;
const int kindTiles = 0;
const int kindFull = 1;

const int maxShareWidth = 1920;
const int maxShareHeight = 1080;
const int minTileShift = 5;
const int maxTileShift = 8;
const int maxUpdateBytes = 4 * 1024 * 1024;
const int maxTileBytes = 256 * 1024;

const int _headerBytes = 17;
const int _entryBytes = 8;

/// One image inside an update: a tile at a grid slot, or the whole frame at
/// the origin.
class ScreenEntry {
  const ScreenEntry(this.tx, this.ty, this.bytes);
  final int tx;
  final int ty;
  final Uint8List bytes;
}

/// One update off the wire.
class ScreenUpdate {
  const ScreenUpdate({
    required this.seq,
    required this.width,
    required this.height,
    required this.tileShift,
    required this.kind,
    required this.cursorX,
    required this.cursorY,
    required this.entries,
  });

  final int seq;
  final int width;
  final int height;
  final int tileShift;
  final int kind;

  /// -1 when unknown.
  final int cursorX;
  final int cursorY;
  final List<ScreenEntry> entries;

  int get tileEdge => 1 << tileShift;
  bool get isFull => kind == kindFull;
  int get cols => (width + tileEdge - 1) ~/ tileEdge;
  int get rows => (height + tileEdge - 1) ~/ tileEdge;

  /// The pixel box one tile covers, clipped at the right and bottom edges.
  ({int left, int top, int right, int bottom}) tileRect(int tx, int ty) {
    final left = tx * tileEdge;
    final top = ty * tileEdge;
    return (
      left: left,
      top: top,
      right: left + tileEdge < width ? left + tileEdge : width,
      bottom: top + tileEdge < height ? top + tileEdge : height,
    );
  }
}

/// Parses one update, or throws [FormatException] for anything over a bound.
ScreenUpdate parseScreenUpdate(Uint8List payload) {
  if (payload.length > maxUpdateBytes) {
    throw const FormatException('update over the byte ceiling');
  }
  if (payload.length < _headerBytes) {
    throw const FormatException('truncated update header');
  }
  final data = ByteData.sublistView(payload);
  final version = data.getUint8(0);
  if (version != screenWireVersion) {
    throw FormatException('screen wire version $version is unsupported');
  }
  final seq = data.getUint32(1);
  final width = data.getUint16(5);
  final height = data.getUint16(7);
  final tileShift = data.getUint8(9);
  final kind = data.getUint8(10);
  final cursorX = data.getInt16(11);
  final cursorY = data.getInt16(13);
  final count = data.getUint16(15);
  if (width == 0 || height == 0 || width > maxShareWidth || height > maxShareHeight) {
    throw const FormatException('share size out of range');
  }
  if (tileShift < minTileShift || tileShift > maxTileShift) {
    throw const FormatException('tile size out of range');
  }
  if (kind != kindTiles && kind != kindFull) {
    throw const FormatException('unknown update kind');
  }
  final edge = 1 << tileShift;
  final cols = (width + edge - 1) ~/ edge;
  final rows = (height + edge - 1) ~/ edge;
  if (kind == kindFull && count != 1) {
    throw const FormatException('a full update carries exactly one image');
  }
  if (count > cols * rows) {
    throw const FormatException('more entries than the grid has tiles');
  }
  final entries = <ScreenEntry>[];
  final seen = <int>{};
  var offset = _headerBytes;
  for (var i = 0; i < count; i++) {
    if (offset + _entryBytes > payload.length) {
      throw const FormatException('truncated update entry');
    }
    final tx = data.getUint16(offset);
    final ty = data.getUint16(offset + 2);
    final size = data.getUint32(offset + 4);
    offset += _entryBytes;
    if (size == 0 || size > maxTileBytes) {
      throw const FormatException('image size out of range');
    }
    if (kind == kindFull) {
      if (tx != 0 || ty != 0) {
        throw const FormatException('a full update sits at the origin');
      }
    } else if (tx >= cols || ty >= rows) {
      throw const FormatException('tile outside the grid');
    }
    final slot = ty * cols + tx;
    if (!seen.add(slot)) throw const FormatException('a tile named twice');
    if (offset + size > payload.length) {
      throw const FormatException('truncated update image');
    }
    entries.add(ScreenEntry(
        tx, ty, Uint8List.sublistView(payload, offset, offset + size)));
    offset += size;
  }
  if (offset != payload.length) {
    throw const FormatException('trailing bytes in update');
  }
  return ScreenUpdate(
    seq: seq,
    width: width,
    height: height,
    tileShift: tileShift,
    kind: kind,
    cursorX: cursorX < 0 || cursorY < 0 ? -1 : cursorX,
    cursorY: cursorX < 0 || cursorY < 0 ? -1 : cursorY,
    entries: entries,
  );
}
