// The FILE action stages a file for the next send: hidden outright where
// this reader may not share one, named and sized in the chip, and mutually
// exclusive with a staged image, since one message carries one attachment.
import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:flutter_ui/attachments.dart';
import 'package:flutter_ui/screens/main_window/compose_bar.dart';
import 'package:flutter_ui/widgets/tc_icon.dart';

final _bytes = Uint8List(2048);
final _png = Uint8List.fromList([1, 2, 3, 4]);

Widget _harness({
  required Future<bool> Function(String, PickedAttachment?) onSend,
  Future<PickedAttachment?> Function()? pickFile,
  Future<PickedAttachment?> Function()? pickAttachment,
}) =>
    MaterialApp(
      home: Scaffold(
        body: ComposeBar(
          channelName: 'general',
          channelHash: 'hash-a',
          enabled: true,
          onSend: onSend,
          pickFile: pickFile,
          pickAttachment: pickAttachment,
          compact: true,
        ),
      ),
    );

Future<PickedAttachment?> _pickFile() async => PickedAttachment(
    name: 'notes.bin', bytes: _bytes, kind: AttachmentKind.file);

Future<PickedAttachment?> _pickImage() async =>
    PickedAttachment(name: 'shot.png', bytes: _png);

Future<void> _tapFile(WidgetTester tester) async {
  await tester.tap(find.text('FILE'));
  await tester.pumpAndSettle();
}

Future<void> _tapPlus(WidgetTester tester) async {
  await tester.tap(find.ancestor(
    of: find.byWidgetPredicate((w) => w is TcIcon && w.icon == TcIcons.plus),
    matching: find.byType(GestureDetector),
  ));
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('no share permission means no file action at all', (tester) async {
    await tester.pumpWidget(_harness(onSend: (_, _) async => true));

    expect(find.text('FILE'), findsNothing);
  });

  testWidgets('the file action appears once sharing is allowed', (tester) async {
    await tester.pumpWidget(
        _harness(onSend: (_, _) async => true, pickFile: _pickFile));

    expect(find.text('FILE'), findsOneWidget);
  });

  testWidgets('a picked file is staged with its name and size', (tester) async {
    await tester.pumpWidget(
        _harness(onSend: (_, _) async => true, pickFile: _pickFile));

    await _tapFile(tester);

    expect(find.text('notes.bin'), findsOneWidget);
    expect(find.text('2.0 KB'), findsOneWidget);
  });

  testWidgets('the staged file is what the send carries', (tester) async {
    PickedAttachment? sent;
    await tester.pumpWidget(_harness(
      onSend: (_, attachment) async {
        sent = attachment;
        return true;
      },
      pickFile: _pickFile,
    ));

    await _tapFile(tester);
    await tester.tap(find.byTooltip('Send'));
    await tester.pumpAndSettle();

    expect(sent?.name, 'notes.bin');
    expect(sent?.kind, AttachmentKind.file);
    expect(sent?.bytes, _bytes);
  });

  testWidgets('a file replaces a staged image, and an image replaces a file',
      (tester) async {
    await tester.pumpWidget(_harness(
      onSend: (_, _) async => true,
      pickFile: _pickFile,
      pickAttachment: _pickImage,
    ));

    await _tapPlus(tester);
    expect(find.text('shot.png'), findsOneWidget);

    await _tapFile(tester);
    expect(find.text('shot.png'), findsNothing);
    expect(find.text('notes.bin'), findsOneWidget);

    await _tapPlus(tester);
    expect(find.text('notes.bin'), findsNothing);
    expect(find.text('shot.png'), findsOneWidget);
  });

  testWidgets('a refused send gives the file back', (tester) async {
    await tester.pumpWidget(
        _harness(onSend: (_, _) async => false, pickFile: _pickFile));

    await _tapFile(tester);
    await tester.tap(find.byTooltip('Send'));
    await tester.pumpAndSettle();

    expect(find.text('notes.bin'), findsOneWidget);
  });

  testWidgets('removing the chip unstages the file', (tester) async {
    PickedAttachment? sent;
    await tester.pumpWidget(_harness(
      onSend: (_, attachment) async {
        sent = attachment;
        return true;
      },
      pickFile: _pickFile,
    ));

    await _tapFile(tester);
    await tester.tap(find.text('REMOVE'));
    await tester.pumpAndSettle();

    await tester.enterText(find.byType(TextField), 'text only');
    await tester.tap(find.byTooltip('Send'));
    await tester.pumpAndSettle();

    expect(sent, isNull);
  });
}
