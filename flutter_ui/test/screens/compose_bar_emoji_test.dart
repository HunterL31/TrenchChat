// A picked custom emoji must read as :name: in the draft but still go out as
// the unambiguous :name@hash:, so a 64-char hash never faces the user.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:flutter_ui/attachments.dart';
import 'package:flutter_ui/screens/main_window/compose_bar.dart';
import 'package:flutter_ui/widgets/tc_icon.dart';

const _hash =
    'aa11bb22cc33dd44ee55ff6677889900aa11bb22cc33dd44ee55ff6677889900';

Widget _harness({
  required Future<bool> Function(String, PickedAttachment?) onSend,
  required Future<String?> Function() pickEmoji,
}) =>
    MaterialApp(
      home: Scaffold(
        body: ComposeBar(
          channelName: 'general',
          enabled: true,
          onSend: onSend,
          pickEmoji: pickEmoji,
          compact: true, // gives a tappable send button
        ),
      ),
    );

Future<void> _pickEmoji(WidgetTester tester) async {
  await tester.tap(find.ancestor(
    of: find.byWidgetPredicate(
        (w) => w is TcIcon && w.icon == TcIcons.emoji),
    matching: find.byType(GestureDetector),
  ));
  await tester.pumpAndSettle();
}

Future<void> _send(WidgetTester tester) async {
  await tester.tap(find.byTooltip('Send'));
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('the draft shows :name:, not the hash', (tester) async {
    await tester.pumpWidget(_harness(
      onSend: (_, _) async => true,
      pickEmoji: () async => ':salute@$_hash:',
    ));

    await _pickEmoji(tester);

    expect(find.text(':salute:'), findsOneWidget);
    expect(find.textContaining(_hash), findsNothing);
  });

  testWidgets('the sent content re-expands to :name@hash:', (tester) async {
    String? sent;
    await tester.pumpWidget(_harness(
      onSend: (c, _) async {
        sent = c;
        return true;
      },
      pickEmoji: () async => ':salute@$_hash:',
    ));

    await _pickEmoji(tester);
    await tester.enterText(find.byType(TextField), 'hi :salute: there');
    await _send(tester);

    expect(sent, 'hi :salute@$_hash: there');
  });

  testWidgets('a unicode pick is inserted and sent unchanged', (tester) async {
    String? sent;
    await tester.pumpWidget(_harness(
      onSend: (c, _) async {
        sent = c;
        return true;
      },
      pickEmoji: () async => '👍',
    ));

    await _pickEmoji(tester);
    expect(find.text('👍'), findsWidgets);

    await _send(tester);
    expect(sent, '👍');
  });

  testWidgets('a name the user typed themselves is left alone', (tester) async {
    String? sent;
    await tester.pumpWidget(_harness(
      onSend: (c, _) async {
        sent = c;
        return true;
      },
      pickEmoji: () async => ':salute@$_hash:',
    ));

    await _pickEmoji(tester);
    await tester.enterText(find.byType(TextField), ':salute: and :unknown:');
    await _send(tester);

    expect(sent, ':salute@$_hash: and :unknown:');
  });

  testWidgets('a refused send keeps the short form and still expands on retry',
      (tester) async {
    final sends = <String>[];
    var accept = false;
    await tester.pumpWidget(_harness(
      onSend: (c, _) async {
        sends.add(c);
        return accept;
      },
      pickEmoji: () async => ':salute@$_hash:',
    ));

    await _pickEmoji(tester);
    await _send(tester);

    // Restored to the readable form, not the raw token.
    expect(find.text(':salute:'), findsOneWidget);

    accept = true;
    await _send(tester);

    expect(sends, [':salute@$_hash:', ':salute@$_hash:']);
  });
}
