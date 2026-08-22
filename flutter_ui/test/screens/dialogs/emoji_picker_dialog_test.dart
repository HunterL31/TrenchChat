import 'dart:convert';
import 'dart:typed_data';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/app_state.dart';
import 'package:flutter_ui/screens/dialogs/emoji_picker_dialog.dart';

import '../../fake_backend.dart';

const _hash =
    'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb';

Future<String> _pngB64() async {
  final recorder = ui.PictureRecorder();
  Canvas(recorder).drawRect(
      const Rect.fromLTWH(0, 0, 8, 8), Paint()..color = const Color(0xFF00FF00));
  final image = await recorder.endRecording().toImage(8, 8);
  final data = await image.toByteData(format: ui.ImageByteFormat.png);
  return base64Encode(Uint8List.view(data!.buffer));
}

Widget _harness(AppState state, void Function(EmojiSelection?) onPicked,
    {String? title}) {
  return MaterialApp(
    home: Scaffold(
      body: Builder(
        builder: (context) => ElevatedButton(
          onPressed: () async => onPicked(title == null
              ? await showEmojiPickerDialog(context, state)
              : await showEmojiPickerDialog(context, state, title: title)),
          child: const Text('open'),
        ),
      ),
    ),
  );
}

void main() {
  late FakeBackend backend;
  late AppState state;

  setUp(() {
    backend = FakeBackend();
    state = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
  });

  tearDown(() {
    state.dispose();
  });

  testWidgets('a built-in pick returns the unicode char for both keys', (tester) async {
    backend.routes['GET /emoji'] = [];
    EmojiSelection? picked;
    await tester.pumpWidget(_harness(state, (s) => picked = s));
    await tester.tap(find.text('open'));
    await tester.pump();
    await settle(tester);

    await tester.tap(find.text('👍'));
    await tester.pumpAndSettle();
    expect(picked, isNotNull);
    expect(picked!.reactionKey, '👍');
    expect(picked!.composeToken, '👍');
  });

  testWidgets('defaults to the React title but honors a custom one', (tester) async {
    backend.routes['GET /emoji'] = [];
    await tester.pumpWidget(_harness(state, (_) {}));
    await tester.tap(find.text('open'));
    await tester.pump();
    await settle(tester);
    expect(find.text('React'), findsOneWidget);

    await tester.tap(find.text('👍'));
    await tester.pumpAndSettle();

    await tester.pumpWidget(_harness(state, (_) {}, title: 'Add emoji'));
    await tester.tap(find.text('open'));
    await tester.pump();
    await settle(tester);
    expect(find.text('Add emoji'), findsOneWidget);
    expect(find.text('React'), findsNothing);
  });

  testWidgets('a custom pick returns the hash key and a :name@hash: token', (tester) async {
    late final String b64;
    await tester.runAsync(() async => b64 = await _pngB64());
    backend.routes['GET /emoji'] = [
      {'emoji_hash': _hash, 'name': 'salute', 'image_data_b64': b64},
    ];
    EmojiSelection? picked;
    await tester.pumpWidget(_harness(state, (s) => picked = s));
    await tester.tap(find.text('open'));
    await tester.pump();
    await settle(tester);

    await tester.tap(find.byTooltip(':salute:'));
    await tester.pumpAndSettle();
    expect(picked, isNotNull);
    expect(picked!.reactionKey, _hash);
    expect(picked!.composeToken, ':salute@$_hash:');
  });
}
