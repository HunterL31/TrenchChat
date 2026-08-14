// Goldens for the three custom-drawn primitives ported from primitives.jsx:
// SignalMeter (all five levels), StatusDot (each state), Avatar (with and
// without an image).
import 'dart:typed_data';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/models/link_quality.dart';
import 'package:flutter_ui/theme/app_theme.dart';
import 'package:flutter_ui/theme/tokens.dart';
import 'package:flutter_ui/widgets/avatar.dart';
import 'package:flutter_ui/widgets/signal_meter.dart';
import 'package:flutter_ui/widgets/status_dot.dart';

import 'test_fonts.dart';

Widget _harness(Widget child) => MaterialApp(
      theme: buildAppTheme(),
      debugShowCheckedModeBanner: false,
      home: Scaffold(body: Center(child: child)),
    );

Widget _labeled(String label, Widget child) => Padding(
      padding: const EdgeInsets.all(12),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          child,
          const SizedBox(height: 6),
          Text(label, style: TextStyle(fontSize: 10, color: TCColors.textSecondary)),
        ],
      ),
    );

Future<Uint8List> _swatchPng({int size = 32, Color color = const Color(0xFFE07A3F)}) async {
  final recorder = ui.PictureRecorder();
  final canvas = Canvas(recorder);
  canvas.drawRect(Rect.fromLTWH(0, 0, size.toDouble(), size.toDouble()), Paint()..color = color);
  final picture = recorder.endRecording();
  final image = await picture.toImage(size, size);
  final data = await image.toByteData(format: ui.ImageByteFormat.png);
  return data!.buffer.asUint8List();
}

void main() {
  setUpAll(loadTestFonts);

  testWidgets('signal_meter all levels', (tester) async {
    await tester.pumpWidget(_harness(Row(
      key: const Key('golden-row'),
      mainAxisSize: MainAxisSize.min,
      children: [
        LinkQualityLevel.excellent,
        LinkQualityLevel.good,
        LinkQualityLevel.fair,
        LinkQualityLevel.poor,
        LinkQualityLevel.unknown,
      ]
          .map((l) => _labeled(l.name, SignalMeter(level: l, size: 18)))
          .toList(),
    )));
    await tester.pump();
    await expectLater(
      find.byKey(const Key('golden-row')),
      matchesGoldenFile('goldens/signal_meter_levels.png'),
    );
  });

  testWidgets('status_dot all states', (tester) async {
    await tester.pumpWidget(_harness(Row(
      key: const Key('golden-row'),
      mainAxisSize: MainAxisSize.min,
      children: [
        _labeled('online', const StatusDot(status: PresenceStatus.online, size: 14)),
        _labeled('offline', const StatusDot(status: PresenceStatus.offline, size: 14)),
        _labeled('away', const StatusDot(status: PresenceStatus.away, size: 14)),
      ],
    )));
    await tester.pump();
    await expectLater(
      find.byKey(const Key('golden-row')),
      matchesGoldenFile('goldens/status_dot_states.png'),
    );
  });

  testWidgets('avatar with and without image', (tester) async {
    // Encoding and decoding an image both need real async; inside the fake-async
    // test zone the codec never completes and the test hangs until timeout.
    late Uint8List imageBytes;
    await tester.runAsync(() async => imageBytes = await _swatchPng());

    await tester.pumpWidget(_harness(Row(
      key: const Key('golden-row'),
      mainAxisSize: MainAxisSize.min,
      children: [
        _labeled('initial', const Avatar(name: 'f3a1…9c2e', size: 40, status: PresenceStatus.online)),
        _labeled('image', Avatar(name: 'f3a1…9c2e', imageBytes: imageBytes, size: 40)),
      ],
    )));
    await tester.runAsync(() async {
      await precacheImage(
        MemoryImage(imageBytes),
        tester.element(find.byKey(const Key('golden-row'))),
      );
    });
    await tester.pump();
    await expectLater(
      find.byKey(const Key('golden-row')),
      matchesGoldenFile('goldens/avatar_variants.png'),
    );
  });
}
