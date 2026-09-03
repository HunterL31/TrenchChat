// A message that names the reader has to look like one: the mention reads as
// a name rather than a hash, and the row it sits in is marked so a ping is
// not missed in a busy channel.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/models/message.dart';
import 'package:flutter_ui/screens/main_window/message_list.dart';

const _me = 'ccccccccccccccccccccccccccccccc0';
const _alice = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa0';

Message _msg(String sender, String content) => Message(
      messageId: '$sender-$content',
      senderHash: sender,
      senderName: sender == _me ? 'me' : 'alice',
      content: content,
      timestamp: 1700000000,
      replyTo: null,
      hasImage: false,
      reactions: const [],
    );

Widget _harness(List<Message> messages) => MaterialApp(
      home: Scaffold(
        body: SizedBox(
          width: 800,
          height: 400,
          child: MessageList(
            messages: messages,
            meHashHex: _me,
            displayNameFor: (hash, fallback) => fallback,
            resolveMentionName: (hash) =>
                {_me: 'Me', _alice: 'Alice'}[hash],
          ),
        ),
      ),
    );

/// The colour painted behind a message row, which is where a ping shows.
Color _rowColour(WidgetTester tester, String messageContent) {
  final container = tester.widget<Container>(
    find
        .ancestor(
          of: find.textContaining(messageContent),
          matching: find.byType(Container),
        )
        .first,
  );
  final decoration = container.decoration;
  return decoration is BoxDecoration
      ? (decoration.color ?? container.color ?? Colors.transparent)
      : (container.color ?? Colors.transparent);
}

void main() {
  testWidgets('a mention renders as a name, never as the raw hash',
      (tester) async {
    await tester.pumpWidget(_harness([_msg(_alice, 'over to you @$_me')]));

    expect(find.textContaining('@Me'), findsOneWidget);
    expect(find.textContaining(_me), findsNothing);
  });

  testWidgets('an identity the client cannot name renders short', (tester) async {
    const stranger = 'ffffffffffffffffffffffffffffffff';
    await tester.pumpWidget(_harness([_msg(_alice, 'hello @$stranger')]));

    expect(find.textContaining('@ffffffff…'), findsOneWidget);
  });

  testWidgets('a row that pings the reader is marked', (tester) async {
    await tester.pumpWidget(_harness([
      _msg(_alice, 'nothing for you'),
      _msg(_alice, 'but this is for @$_me'),
    ]));

    expect(_rowColour(tester, 'but this is for'),
        isNot(_rowColour(tester, 'nothing for you')));
  });

  testWidgets('our own mention of ourselves does not mark the row',
      (tester) async {
    // Against another of our own rows, not against somebody else's: an own
    // row is already tinted, so only this comparison can fail.
    await tester.pumpWidget(_harness([
      _msg(_me, 'plain enough'),
      _msg(_me, 'note to self @$_me'),
    ]));

    expect(_rowColour(tester, 'note to self'), _rowColour(tester, 'plain enough'));
  });
}
