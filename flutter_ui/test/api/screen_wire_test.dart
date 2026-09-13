import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/screen_wire.dart';

/// Builds one update the way trenchchat/network/screen_wire.py packs it.
Uint8List packUpdate({
  int version = screenWireVersion,
  int seq = 1,
  int width = 300,
  int height = 200,
  int tileShift = 7,
  int kind = kindTiles,
  int cursorX = -1,
  int cursorY = -1,
  List<(int, int, List<int>)> entries = const [],
}) {
  final out = BytesBuilder();
  final header = ByteData(17)
    ..setUint8(0, version)
    ..setUint32(1, seq)
    ..setUint16(5, width)
    ..setUint16(7, height)
    ..setUint8(9, tileShift)
    ..setUint8(10, kind)
    ..setInt16(11, cursorX)
    ..setInt16(13, cursorY)
    ..setUint16(15, entries.length);
  out.add(header.buffer.asUint8List());
  for (final (tx, ty, bytes) in entries) {
    final entry = ByteData(8)
      ..setUint16(0, tx)
      ..setUint16(2, ty)
      ..setUint32(4, bytes.length);
    out.add(entry.buffer.asUint8List());
    out.add(bytes);
  }
  return out.toBytes();
}

void main() {
  test('a tile update round-trips with its grid and cursor', () {
    final update = parseScreenUpdate(packUpdate(
      seq: 9,
      cursorX: 12,
      cursorY: 34,
      entries: [(0, 0, [1, 2, 3]), (2, 1, [4, 5])],
    ));
    expect(update.seq, 9);
    expect((update.width, update.height), (300, 200));
    expect((update.cols, update.rows), (3, 2));
    expect((update.cursorX, update.cursorY), (12, 34));
    expect(update.isFull, isFalse);
    expect(update.entries.map((e) => (e.tx, e.ty)), [(0, 0), (2, 1)]);
    expect(update.entries.last.bytes, [4, 5]);
    expect(update.tileRect(2, 1), (left: 256, top: 128, right: 300, bottom: 200));
  });

  test('a full update carries one image at the origin', () {
    final update = parseScreenUpdate(packUpdate(kind: kindFull, entries: [(0, 0, [7])]));
    expect(update.isFull, isTrue);
    expect(update.entries.single.bytes, [7]);
  });

  test('an unknown cursor reads as unknown', () {
    final update = parseScreenUpdate(packUpdate(entries: [(0, 0, [1])]));
    expect((update.cursorX, update.cursorY), (-1, -1));
  });

  group('refuses', () {
    void refuses(Uint8List bytes) =>
        expect(() => parseScreenUpdate(bytes), throwsFormatException);

    test('another version', () => refuses(packUpdate(version: 2)));
    test('a truncated header', () => refuses(Uint8List(10)));
    test('a share over the ceiling', () => refuses(packUpdate(width: 4000)));
    test('a tile shift out of range', () => refuses(packUpdate(tileShift: 3)));
    test('an unknown kind', () => refuses(packUpdate(kind: 5)));
    test('a tile outside the grid',
        () => refuses(packUpdate(entries: [(5, 0, [1])])));
    test('a tile named twice',
        () => refuses(packUpdate(entries: [(0, 0, [1]), (0, 0, [2])])));
    test('an empty image', () => refuses(packUpdate(entries: [(0, 0, [])])));
    test('a full update with two images',
        () => refuses(packUpdate(kind: kindFull, entries: [(0, 0, [1]), (0, 0, [2])])));
    test('trailing bytes', () {
      final bytes = packUpdate(entries: [(0, 0, [1])]);
      refuses(Uint8List.fromList([...bytes, 0]));
    });
    test('a truncated image', () {
      final bytes = packUpdate(entries: [(0, 0, [1, 2, 3, 4])]);
      refuses(Uint8List.sublistView(bytes, 0, bytes.length - 2));
    });
    test('a blob over the byte ceiling', () => refuses(Uint8List(maxUpdateBytes + 1)));
  });
}
