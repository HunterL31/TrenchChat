// The + button stages an image, and the staged image is what the send carries.
// A send with no text at all is still a send when an image is attached, and a
// refused send gives the image back rather than eating it.
import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:flutter_ui/attachments.dart';
import 'package:flutter_ui/screens/main_window/compose_bar.dart';
import 'package:flutter_ui/widgets/tc_icon.dart';

final _png = Uint8List.fromList([1, 2, 3, 4]);

const _attachment = 'shot.png';

/// Stands in for the platform paste route: [paste] fires whatever a real
/// paste event would have delivered, and [disposed] records that the compose
/// bar unsubscribed.
class _FakePasteSource {
  void Function(PickedAttachment)? _handler;
  var disposed = false;

  VoidCallback watch(void Function(PickedAttachment) onImage) {
    _handler = onImage;
    return () => disposed = true;
  }

  void paste(PickedAttachment image) => _handler!(image);
}

Widget _harness({
  required Future<bool> Function(String, PickedAttachment?) onSend,
  Future<PickedAttachment?> Function()? pickAttachment,
  _FakePasteSource? pasteSource,
  bool enabled = true,
  String? channelHash,
}) =>
    MaterialApp(
      home: Scaffold(
        body: ComposeBar(
          channelName: 'general',
          channelHash: channelHash,
          enabled: enabled,
          onSend: onSend,
          pickAttachment: pickAttachment,
          watchPastedImages: pasteSource?.watch,
          compact: true, // gives a tappable send button
        ),
      ),
    );

Future<PickedAttachment?> _pick() async =>
    PickedAttachment(name: _attachment, bytes: _png);

Future<void> _tapPlus(WidgetTester tester) async {
  await tester.tap(find.ancestor(
    of: find.byWidgetPredicate((w) => w is TcIcon && w.icon == TcIcons.plus),
    matching: find.byType(GestureDetector),
  ));
  await tester.pumpAndSettle();
}

Future<void> _send(WidgetTester tester) async {
  await tester.tap(find.byTooltip('Send'));
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('picking an image stages it as a named chip', (tester) async {
    await tester.pumpWidget(
        _harness(onSend: (_, _) async => true, pickAttachment: _pick));

    expect(find.text(_attachment), findsNothing);
    await _tapPlus(tester);
    expect(find.text(_attachment), findsOneWidget);
  });

  testWidgets('the staged image reaches onSend', (tester) async {
    PickedAttachment? sent;
    await tester.pumpWidget(_harness(
      onSend: (_, attachment) async {
        sent = attachment;
        return true;
      },
      pickAttachment: _pick,
    ));

    await _tapPlus(tester);
    await tester.enterText(find.byType(TextField), 'look at this');
    await _send(tester);

    expect(sent?.name, _attachment);
    expect(sent?.bytes, _png);
  });

  testWidgets('an image with no text still sends', (tester) async {
    var sends = 0;
    await tester.pumpWidget(_harness(
      onSend: (content, attachment) async {
        sends++;
        expect(content, '');
        expect(attachment, isNotNull);
        return true;
      },
      pickAttachment: _pick,
    ));

    await _tapPlus(tester);
    await _send(tester);

    expect(sends, 1);
  });

  testWidgets('an accepted send clears the chip', (tester) async {
    await tester.pumpWidget(
        _harness(onSend: (_, _) async => true, pickAttachment: _pick));

    await _tapPlus(tester);
    await _send(tester);

    expect(find.text(_attachment), findsNothing);
  });

  testWidgets('a refused send gives the image back', (tester) async {
    await tester.pumpWidget(
        _harness(onSend: (_, _) async => false, pickAttachment: _pick));

    await _tapPlus(tester);
    await tester.enterText(find.byType(TextField), 'nope');
    await _send(tester);

    expect(find.text(_attachment), findsOneWidget);
    expect(find.text('nope'), findsOneWidget);
  });

  testWidgets('removing the chip unstages the image', (tester) async {
    PickedAttachment? sent;
    var sends = 0;
    await tester.pumpWidget(_harness(
      onSend: (_, attachment) async {
        sends++;
        sent = attachment;
        return true;
      },
      pickAttachment: _pick,
    ));

    await _tapPlus(tester);
    await tester.tap(find.text('REMOVE'));
    await tester.pumpAndSettle();
    expect(find.text(_attachment), findsNothing);

    await tester.enterText(find.byType(TextField), 'text only');
    await _send(tester);
    expect(sends, 1);
    expect(sent, isNull);
  });

  testWidgets('with no picker wired the + button is inert', (tester) async {
    await tester.pumpWidget(_harness(onSend: (_, _) async => true));

    await _tapPlus(tester);
    expect(find.text(_attachment), findsNothing);
  });

  testWidgets('a pasted image stages while the message field has focus',
      (tester) async {
    final source = _FakePasteSource();
    await tester.pumpWidget(
        _harness(onSend: (_, _) async => true, pasteSource: source));

    await tester.tap(find.byType(TextField));
    await tester.pumpAndSettle();
    source.paste(PickedAttachment(name: _attachment, bytes: _png));
    await tester.pumpAndSettle();

    expect(find.text(_attachment), findsOneWidget);
  });

  testWidgets('a paste with the message field unfocused is ignored',
      (tester) async {
    final source = _FakePasteSource();
    await tester.pumpWidget(
        _harness(onSend: (_, _) async => true, pasteSource: source));

    source.paste(PickedAttachment(name: _attachment, bytes: _png));
    await tester.pumpAndSettle();

    expect(find.text(_attachment), findsNothing);
  });

  testWidgets('a pasted image is what the send carries', (tester) async {
    final source = _FakePasteSource();
    PickedAttachment? sent;
    await tester.pumpWidget(_harness(
      onSend: (_, attachment) async {
        sent = attachment;
        return true;
      },
      pasteSource: source,
    ));

    await tester.tap(find.byType(TextField));
    await tester.pumpAndSettle();
    source.paste(PickedAttachment(name: _attachment, bytes: _png));
    await tester.pumpAndSettle();
    await _send(tester);

    expect(sent?.bytes, _png);
  });

  testWidgets('a paste into a channel that refuses sends is ignored',
      (tester) async {
    final source = _FakePasteSource();
    await tester.pumpWidget(_harness(
        onSend: (_, _) async => true, pasteSource: source, enabled: false));

    source.paste(PickedAttachment(name: _attachment, bytes: _png));
    await tester.pumpAndSettle();

    expect(find.text(_attachment), findsNothing);
  });

  testWidgets('the paste subscription is dropped with the compose bar',
      (tester) async {
    final source = _FakePasteSource();
    await tester.pumpWidget(
        _harness(onSend: (_, _) async => true, pasteSource: source));
    expect(source.disposed, isFalse);

    await tester.pumpWidget(const MaterialApp(home: Scaffold()));
    expect(source.disposed, isTrue);
  });

  testWidgets('a staged image follows its own channel', (tester) async {
    await tester.pumpWidget(_harness(
        onSend: (_, _) async => true, pickAttachment: _pick, channelHash: 'aaa'));

    await _tapPlus(tester);
    expect(find.text(_attachment), findsOneWidget);

    await tester.pumpWidget(_harness(
        onSend: (_, _) async => true, pickAttachment: _pick, channelHash: 'bbb'));
    await tester.pumpAndSettle();
    expect(find.text(_attachment), findsNothing);

    await tester.pumpWidget(_harness(
        onSend: (_, _) async => true, pickAttachment: _pick, channelHash: 'aaa'));
    await tester.pumpAndSettle();
    expect(find.text(_attachment), findsOneWidget);
  });
}
