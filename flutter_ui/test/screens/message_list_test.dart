// Verifies author grouping collapses at the GROUP_WINDOW_SECS (300s)
// boundary, and that a date divider appears exactly on a day change --
// mirroring trenchchat/gui/channel_view.py's grouping contract.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/models/message.dart';
import 'package:flutter_ui/format.dart';
import 'package:flutter_ui/screens/main_window/message_list.dart';
import 'package:flutter_ui/widgets/avatar.dart';

Message _msg(String sender, double ts, String content,
        {bool imageStripped = false}) =>
    Message(
      messageId: '$sender-$ts',
      senderHash: sender,
      senderName: sender,
      content: content,
      timestamp: ts,
      replyTo: null,
      hasImage: false,
      reactions: const [],
      imageStripped: imageStripped,
    );

Widget _harness(List<Message> messages,
        {VoidCallback? onLoadOlder, bool hasMoreOlder = false, bool loadingOlder = false}) =>
    MaterialApp(
      home: Scaffold(
        body: SizedBox(
          width: 800,
          height: 400,
          child: MessageList(
            messages: messages,
            meHashHex: 'me',
            displayNameFor: (hash, fallback) => fallback,
            onLoadOlder: onLoadOlder,
            hasMoreOlder: hasMoreOlder,
            loadingOlder: loadingOlder,
          ),
        ),
      ),
    );

void main() {
  testWidgets('messages from the same sender under 300s collapse to one avatar', (tester) async {
    const base = 1_700_000_000.0;
    final messages = [
      _msg('alice', base, 'first'),
      _msg('alice', base + 299, 'still grouped'),
    ];

    await tester.pumpWidget(_harness(messages));
    await tester.pumpAndSettle();

    expect(find.byType(Avatar), findsOneWidget);
    expect(find.text('first'), findsOneWidget);
    expect(find.text('still grouped'), findsOneWidget);
  });

  testWidgets('messages from the same sender at/over 300s start a new group', (tester) async {
    const base = 1_700_000_000.0;
    final messages = [
      _msg('alice', base, 'first'),
      _msg('alice', base + 300, 'new group'),
    ];

    await tester.pumpWidget(_harness(messages));
    await tester.pumpAndSettle();

    expect(find.byType(Avatar), findsNWidgets(2));
  });

  testWidgets('a different sender always starts a new group', (tester) async {
    const base = 1_700_000_000.0;
    final messages = [
      _msg('alice', base, 'first'),
      _msg('bob', base + 5, 'reply'),
    ];

    await tester.pumpWidget(_harness(messages));
    await tester.pumpAndSettle();

    expect(find.byType(Avatar), findsNWidgets(2));
  });

  testWidgets('a day change inserts exactly one date divider', (tester) async {
    final day1 = DateTime(2026, 8, 10, 21, 0).millisecondsSinceEpoch / 1000;
    final day2 = DateTime(2026, 8, 11, 9, 0).millisecondsSinceEpoch / 1000;
    final messages = [
      _msg('alice', day1, 'end of day one'),
      _msg('alice', day2, 'start of day two'),
    ];

    await tester.pumpWidget(_harness(messages));
    await tester.pumpAndSettle();

    expect(find.text(formatDateDivider(day2)), findsOneWidget);
    // No divider before the very first message in history.
    expect(find.text(formatDateDivider(day1)), findsNothing);
  });

  testWidgets('same-day messages produce no divider', (tester) async {
    const base = 1_700_000_000.0;
    final messages = [
      _msg('alice', base, 'a'),
      _msg('bob', base + 3600, 'b'),
    ];

    final dateLabelPattern = RegExp(r'^[A-Z]{3} \d{2} \d{4}$');
    await tester.pumpWidget(_harness(messages));
    await tester.pumpAndSettle();

    final dateLabels = find.byWidgetPredicate(
      (w) => w is Text && w.data != null && dateLabelPattern.hasMatch(w.data!),
    );
    expect(dateLabels, findsNothing);
  });

  testWidgets('scrolling to the top requests an older page when more exist',
      (tester) async {
    const base = 1_700_000_000.0;
    final messages = [for (var i = 0; i < 40; i++) _msg('alice', base + i * 400, 'm$i')];
    var loadOlderCalls = 0;

    await tester.pumpWidget(_harness(messages,
        hasMoreOlder: true, onLoadOlder: () => loadOlderCalls++));
    await tester.pumpAndSettle();

    // Opens pinned to the bottom (newest), so no load fires yet.
    expect(loadOlderCalls, 0);

    // Drag the list down (scrolling up toward the oldest) until it reaches the
    // top, which triggers the older-page fetch.
    await tester.fling(find.byType(MessageList), const Offset(0, 4000), 8000);
    await tester.pumpAndSettle();

    expect(loadOlderCalls, greaterThan(0));
  });

  testWidgets('reaching the top does not request when history is exhausted',
      (tester) async {
    const base = 1_700_000_000.0;
    final messages = [for (var i = 0; i < 40; i++) _msg('alice', base + i * 400, 'm$i')];
    var loadOlderCalls = 0;

    await tester.pumpWidget(_harness(messages,
        hasMoreOlder: false, onLoadOlder: () => loadOlderCalls++));
    await tester.pumpAndSettle();

    await tester.fling(find.byType(MessageList), const Offset(0, 4000), 8000);
    await tester.pumpAndSettle();

    expect(loadOlderCalls, 0);
  });

  testWidgets('an older page prepended at the top keeps the view from jumping to bottom',
      (tester) async {
    const base = 1_700_000_000.0;
    final newest = [for (var i = 0; i < 30; i++) _msg('alice', base + i * 400, 'new$i')];
    await tester.pumpWidget(_harness(newest, hasMoreOlder: true, onLoadOlder: () {}));
    await tester.pumpAndSettle();

    final controller = tester
        .widget<Scrollable>(find.byType(Scrollable))
        .controller!;
    final bottom = controller.position.pixels;

    // Scroll up a bit, then a prepend arrives.
    controller.jumpTo(bottom - 200);
    await tester.pump();
    final beforePrepend = controller.position.pixels;

    final older = [for (var i = 0; i < 20; i++) _msg('alice', base - (20 - i) * 400, 'old$i')];
    await tester.pumpWidget(_harness([...older, ...newest],
        hasMoreOlder: true, onLoadOlder: () {}));
    await tester.pumpAndSettle();

    // The view stayed anchored rather than snapping to the newest message.
    expect(controller.position.pixels, greaterThan(beforePrepend));
    expect(controller.position.pixels,
        lessThan(controller.position.maxScrollExtent));
  });

  testWidgets('a stripped attachment is marked, an intact message is not',
      (tester) async {
    const base = 1_700_000_000.0;
    final messages = [
      _msg('alice', base, 'plain'),
      _msg('bob', base + 600, 'relayed', imageStripped: true),
    ];

    await tester.pumpWidget(_harness(messages));
    await tester.pumpAndSettle();

    expect(
      find.text('Attachment removed \u2014 it could not be displayed safely'),
      findsOneWidget,
    );
  });
}
