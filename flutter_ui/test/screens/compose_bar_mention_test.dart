// A ping names an identity, so `@Name` must read as a name in the draft and
// go out as the hash. Typing a name by hand is words, not a ping: only a pick
// off the list expands.
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:flutter_ui/attachments.dart';
import 'package:flutter_ui/mentions.dart';
import 'package:flutter_ui/screens/main_window/compose_bar.dart';

const _alice = 'aa11bb22cc33dd44ee55ff6677889900';
const _alsoAlice = 'bb11bb22cc33dd44ee55ff6677889900';
const _bob = 'cc11bb22cc33dd44ee55ff6677889900';

const _roster = [
  MentionCandidate(identityHash: _alice, displayName: 'Alice'),
  MentionCandidate(identityHash: _bob, displayName: 'Bob'),
];

Widget _harness({
  required Future<bool> Function(String, PickedAttachment?) onSend,
  List<MentionCandidate> candidates = _roster,
}) =>
    MaterialApp(
      home: Scaffold(
        body: ComposeBar(
          channelName: 'general',
          enabled: true,
          onSend: onSend,
          mentionCandidates: candidates,
          compact: true, // gives a tappable send button
        ),
      ),
    );

Future<void> _type(WidgetTester tester, String text) async {
  await tester.enterText(find.byType(TextField), text);
  await tester.pumpAndSettle();
}

Future<void> _send(WidgetTester tester) async {
  await tester.tap(find.byTooltip('Send'));
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('typing @ offers the roster', (tester) async {
    await tester.pumpWidget(_harness(onSend: (_, _) async => true));

    await _type(tester, 'hey @');

    expect(find.text('Alice'), findsOneWidget);
    expect(find.text('Bob'), findsOneWidget);
  });

  testWidgets('the query narrows the list', (tester) async {
    await tester.pumpWidget(_harness(onSend: (_, _) async => true));

    await _type(tester, 'hey @bo');

    expect(find.text('Bob'), findsOneWidget);
    expect(find.text('Alice'), findsNothing);
  });

  testWidgets('a query matching nobody closes the list', (tester) async {
    await tester.pumpWidget(_harness(onSend: (_, _) async => true));

    await _type(tester, 'hey @zebra');

    expect(find.text('Alice'), findsNothing);
    expect(find.text('Bob'), findsNothing);
  });

  testWidgets('an @ inside a word is an address, not a mention', (tester) async {
    await tester.pumpWidget(_harness(onSend: (_, _) async => true));

    await _type(tester, 'mail me at bob@');

    expect(find.text('Alice'), findsNothing);
    expect(find.text('Bob'), findsNothing);
  });

  testWidgets('no candidates means no picker at all', (tester) async {
    await tester.pumpWidget(
        _harness(onSend: (_, _) async => true, candidates: const []));

    await _type(tester, 'hey @');

    expect(find.text('Alice'), findsNothing);
  });

  testWidgets('picking writes the name into the draft, not the hash',
      (tester) async {
    await tester.pumpWidget(_harness(onSend: (_, _) async => true));

    await _type(tester, 'hey @al');
    await tester.tap(find.text('Alice'));
    await tester.pumpAndSettle();

    expect(find.text('hey @Alice '), findsOneWidget);
    expect(find.textContaining(_alice), findsNothing);
  });

  testWidgets('the sent content carries the hash', (tester) async {
    String? sent;
    await tester.pumpWidget(_harness(onSend: (c, _) async {
      sent = c;
      return true;
    }));

    await _type(tester, 'hey @al');
    await tester.tap(find.text('Alice'));
    await tester.pumpAndSettle();
    await _send(tester);

    expect(sent, 'hey @$_alice ');
  });

  testWidgets('a name typed by hand is words, not a ping', (tester) async {
    String? sent;
    await tester.pumpWidget(_harness(onSend: (c, _) async {
      sent = c;
      return true;
    }));

    await _type(tester, 'tell @Alice yourself');
    await _send(tester);

    expect(sent, 'tell @Alice yourself');
  });

  testWidgets('a picked mention the user then deleted is not sent',
      (tester) async {
    String? sent;
    await tester.pumpWidget(_harness(onSend: (c, _) async {
      sent = c;
      return true;
    }));

    await _type(tester, 'hey @al');
    await tester.tap(find.text('Alice'));
    await tester.pumpAndSettle();
    await _type(tester, 'never mind');
    await _send(tester);

    expect(sent, 'never mind');
  });

  testWidgets('two peers of one name get separate labels', (tester) async {
    String? sent;
    await tester.pumpWidget(_harness(
      onSend: (c, _) async {
        sent = c;
        return true;
      },
      candidates: const [
        MentionCandidate(identityHash: _alice, displayName: 'Alice'),
        MentionCandidate(identityHash: _alsoAlice, displayName: 'Alice'),
      ],
    ));

    await _type(tester, '@al');
    await tester.tap(find.text('Alice').first);
    await tester.pumpAndSettle();
    await _type(tester, '@Alice and @al');
    await tester.tap(find.text('Alice').last);
    await tester.pumpAndSettle();
    await _send(tester);

    expect(sent, '@$_alice and @$_alsoAlice ');
  });

  testWidgets('enter picks instead of sending while the list is open',
      (tester) async {
    var sends = 0;
    await tester.pumpWidget(_harness(onSend: (_, _) async {
      sends++;
      return true;
    }));

    await _type(tester, 'hey @al');
    await tester.sendKeyEvent(LogicalKeyboardKey.enter);
    await tester.pumpAndSettle();

    expect(sends, 0);
    expect(find.text('hey @Alice '), findsOneWidget);
  });

  testWidgets('arrow keys move the pick', (tester) async {
    String? sent;
    await tester.pumpWidget(_harness(onSend: (c, _) async {
      sent = c;
      return true;
    }));

    await _type(tester, '@');
    await tester.sendKeyEvent(LogicalKeyboardKey.arrowDown);
    await tester.pumpAndSettle();
    await tester.sendKeyEvent(LogicalKeyboardKey.enter);
    await tester.pumpAndSettle();
    await _send(tester);

    expect(sent, '@$_bob ');
  });

  testWidgets('escape closes the list and leaves the draft alone',
      (tester) async {
    await tester.pumpWidget(_harness(onSend: (_, _) async => true));

    await _type(tester, 'hey @al');
    await tester.sendKeyEvent(LogicalKeyboardKey.escape);
    await tester.pumpAndSettle();

    expect(find.text('Alice'), findsNothing);
    expect(find.text('hey @al'), findsOneWidget);
  });
}
