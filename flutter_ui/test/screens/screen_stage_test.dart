import 'dart:typed_data';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/screen_watch.dart';
import 'package:flutter_ui/api/screen_wire.dart';
import 'package:flutter_ui/screens/main_window/screen_stage.dart';
import 'package:flutter_ui/widgets/tc_icon.dart';

import '../api/screen_wire_test.dart';

const int _edge = 32;
const int _shift = 5;

Future<Uint8List> _png(int width, int height, Color colour) async {
  final recorder = ui.PictureRecorder();
  Canvas(recorder).drawRect(
      Rect.fromLTWH(0, 0, width.toDouble(), height.toDouble()), Paint()..color = colour);
  final image = await recorder.endRecording().toImage(width, height);
  final bytes = await image.toByteData(format: ui.ImageByteFormat.png);
  image.dispose();
  return bytes!.buffer.asUint8List();
}

Future<Color> _pixel(ui.Image image, int x, int y) async {
  final data = await image.toByteData(format: ui.ImageByteFormat.rawRgba);
  final offset = (y * image.width + x) * 4;
  return Color.fromARGB(data!.getUint8(offset + 3), data.getUint8(offset),
      data.getUint8(offset + 1), data.getUint8(offset + 2));
}

/// Applies a hand-built update to the buffer the way the watch does.
Future<void> _apply(ScreenFrameBuffer buffer, Uint8List packed) async {
  final update = parseScreenUpdate(packed);
  buffer.apply(update, await decodeScreenUpdate(update));
}

void main() {
  test('a full frame then two tiles compose into one picture', () async {
    final buffer = ScreenFrameBuffer();
    final red = await _png(96, 64, const Color(0xFFFF0000));
    await _apply(buffer, packUpdate(
        width: 96, height: 64, tileShift: _shift, kind: kindFull,
        entries: [(0, 0, red)]));
    expect(buffer.full, isNotNull);
    expect((buffer.width, buffer.height), (96, 64));

    final blue = await _png(_edge, _edge, const Color(0xFF0000FF));
    final green = await _png(_edge, _edge, const Color(0xFF00FF00));
    await _apply(buffer, packUpdate(
        seq: 2, width: 96, height: 64, tileShift: _shift,
        entries: [(1, 0, blue), (2, 1, green)]));
    expect(buffer.tiles, hasLength(2));
    expect(buffer.updates, 2);

    final composed = await composeForTest(buffer);
    expect(await _pixel(composed, 4, 4), const Color(0xFFFF0000));
    expect(await _pixel(composed, _edge + 4, 4), const Color(0xFF0000FF));
    expect(await _pixel(composed, 2 * _edge + 4, _edge + 4), const Color(0xFF00FF00));
    expect(await _pixel(composed, 4, _edge + 4), const Color(0xFFFF0000));
    composed.dispose();
    buffer.dispose();
  });

  test('a later tile replaces its slot and a new full frame clears the tiles',
      () async {
    final buffer = ScreenFrameBuffer();
    final red = await _png(64, 32, const Color(0xFFFF0000));
    await _apply(buffer, packUpdate(
        width: 64, height: 32, tileShift: _shift, kind: kindFull, entries: [(0, 0, red)]));
    final blue = await _png(_edge, _edge, const Color(0xFF0000FF));
    await _apply(buffer, packUpdate(
        seq: 2, width: 64, height: 32, tileShift: _shift, entries: [(0, 0, blue)]));
    final green = await _png(_edge, _edge, const Color(0xFF00FF00));
    await _apply(buffer, packUpdate(
        seq: 3, width: 64, height: 32, tileShift: _shift, entries: [(0, 0, green)]));
    expect(buffer.tiles, hasLength(1));
    var composed = await composeForTest(buffer);
    expect(await _pixel(composed, 2, 2), const Color(0xFF00FF00));
    composed.dispose();

    final white = await _png(64, 32, const Color(0xFFFFFFFF));
    await _apply(buffer, packUpdate(
        seq: 4, width: 64, height: 32, tileShift: _shift, kind: kindFull,
        entries: [(0, 0, white)]));
    expect(buffer.tiles, isEmpty);
    composed = await composeForTest(buffer);
    expect(await _pixel(composed, 2, 2), const Color(0xFFFFFFFF));
    composed.dispose();
    buffer.dispose();
  });

  test('the painter keeps the share aspect inside the stage', () {
    final buffer = ScreenFrameBuffer()
      ..width = 160
      ..height = 90;
    final painter = ScreenStagePainter(buffer, background: const Color(0xFF000000));
    final rect = painter.pictureRect(const Size(400, 400));
    expect(rect.width, 400);
    expect(rect.height, 225);
    expect(rect.top, 87.5);
  });

  testWidgets('the stage names the sharer and offers expand and stop',
      (tester) async {
    final buffer = ScreenFrameBuffer();
    var expanded = false;
    var stopped = false;
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: Column(children: [
          ScreenStage(
            buffer: buffer,
            sharerName: 'Alice',
            expanded: false,
            onToggleExpanded: () => expanded = true,
            onStop: () => stopped = true,
          ),
        ]),
      ),
    ));
    expect(find.text('WATCHING · Alice'), findsOneWidget);
    expect(find.byWidgetPredicate((w) => w is TcIcon && w.icon == TcIcons.screen),
        findsOneWidget);
    await tester.tap(find.byTooltip('Expand'));
    expect(expanded, isTrue);
    await tester.tap(find.byTooltip('Stop watching'));
    expect(stopped, isTrue);
    buffer.dispose();
  });

  testWidgets('an ended share keeps the stage up with the reason over it',
      (tester) async {
    final buffer = ScreenFrameBuffer();
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: Column(children: [
          ScreenStage(
            buffer: buffer,
            sharerName: 'Alice',
            expanded: false,
            endedReason: 'The direct connection was lost.',
            onToggleExpanded: () {},
            onStop: () {},
          ),
        ]),
      ),
    ));
    expect(find.text('SHARE ENDED · Alice'), findsOneWidget);
    expect(find.text('The direct connection was lost.'), findsOneWidget);
    buffer.dispose();
  });
}
