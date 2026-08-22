// The compose bar's reply state: the "Replying to X" banner and its cancel
// affordance. The reply_to itself is threaded by the caller through onSend;
// here we only cover the banner UI contract.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/screens/main_window/compose_bar.dart';

Widget _harness({
  ({String author, String snippet})? replyPreview,
  VoidCallback? onCancelReply,
}) =>
    MaterialApp(
      home: Scaffold(
        body: ComposeBar(
          channelHash: 'hash-a',
          channelName: 'alpha',
          enabled: true,
          onSend: (_, _) async => true,
          replyPreview: replyPreview,
          onCancelReply: onCancelReply,
          compact: true,
        ),
      ),
    );

void main() {
  testWidgets('no reply banner shows when nothing is being replied to', (tester) async {
    await tester.pumpWidget(_harness());

    expect(find.textContaining('Replying to'), findsNothing);
  });

  testWidgets('the reply banner names the target and shows a snippet', (tester) async {
    await tester.pumpWidget(_harness(
      replyPreview: (author: 'alice', snippet: 'the original text'),
      onCancelReply: () {},
    ));

    expect(find.text('Replying to alice'), findsOneWidget);
    expect(find.text('the original text'), findsOneWidget);
    expect(find.text('CANCEL'), findsOneWidget);
  });

  testWidgets('CANCEL fires onCancelReply', (tester) async {
    var cancelled = false;
    await tester.pumpWidget(_harness(
      replyPreview: (author: 'alice', snippet: 'the original text'),
      onCancelReply: () => cancelled = true,
    ));

    await tester.tap(find.text('CANCEL'));
    await tester.pump();

    expect(cancelled, isTrue);
  });
}
