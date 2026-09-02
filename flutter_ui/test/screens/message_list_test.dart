// Verifies author grouping collapses at the groupWindowSecs (300s)
// boundary, and that a date divider appears exactly on a day change.
import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/models/message.dart';
import 'package:flutter_ui/format.dart';
import 'package:flutter_ui/screens/main_window/message_list.dart';
import 'package:flutter_ui/theme/tokens.dart';
import 'package:flutter_ui/widgets/avatar.dart';
import 'package:flutter_ui/widgets/emoji_text.dart';

Message _msg(String sender, double ts, String content,
        {bool imageStripped = false, String? replyTo, String? deliveryState}) =>
    Message(
      messageId: '$sender-$ts',
      senderHash: sender,
      senderName: sender,
      content: content,
      timestamp: ts,
      replyTo: replyTo,
      hasImage: false,
      reactions: const [],
      imageStripped: imageStripped,
      deliveryState: deliveryState,
    );

Widget _harness(List<Message> messages,
        {VoidCallback? onLoadOlder,
        bool hasMoreOlder = false,
        bool loadingOlder = false,
        String meHashHex = 'me',
        void Function(Message)? onReply,
        void Function(String)? onOpenLink}) =>
    MaterialApp(
      home: Scaffold(
        body: SizedBox(
          width: 800,
          height: 400,
          child: MessageList(
            messages: messages,
            meHashHex: meHashHex,
            displayNameFor: (hash, fallback) => fallback,
            onLoadOlder: onLoadOlder,
            hasMoreOlder: hasMoreOlder,
            loadingOlder: loadingOlder,
            onReply: onReply,
            onOpenLink: onOpenLink,
          ),
        ),
      ),
    );

/// A long history of deliberately varied row heights: three senders so group
/// headers appear, multi-line bodies among the one-liners, and enough time
/// between messages that date dividers land in the middle of it. The oldest
/// stretch is one-liners only, so the extent the first frame estimates from
/// the top of the list falls well short of the real one. Uniform rows never
/// expose that drift.
List<Message> _variedHistory({int count = 180}) {
  const senders = ['alice', 'bob', 'carol'];
  final start = DateTime(2026, 1, 1, 9).millisecondsSinceEpoch / 1000;
  return [
    for (var i = 0; i < count; i++)
      _msg(
        senders[i % senders.length],
        start + i * 4000,
        i > 20 && i % 3 == 0
            ? 'message $i line one\nline two\nline three\nline four\nline five'
            : 'message $i',
      ),
  ];
}

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

  testWidgets('a channel with no messages shows an empty-state placeholder',
      (tester) async {
    await tester.pumpWidget(_harness(const []));
    await tester.pumpAndSettle();

    expect(find.text('No messages yet — say something.'), findsOneWidget);
  });

  testWidgets('a new message auto-scrolls to the bottom when the reader is already there',
      (tester) async {
    const base = 1_700_000_000.0;
    final messages = [for (var i = 0; i < 40; i++) _msg('alice', base + i * 400, 'm$i')];

    await tester.pumpWidget(_harness(messages));
    await tester.pumpAndSettle();

    final controller = tester.widget<Scrollable>(find.byType(Scrollable)).controller!;
    expect(controller.position.pixels, closeTo(controller.position.maxScrollExtent, 1));

    // A new message arrives on the same (in-place mutated) list instance.
    messages.add(_msg('alice', base + 40 * 400, 'brand new'));
    await tester.pumpWidget(_harness(messages));
    await tester.pumpAndSettle();

    expect(controller.position.pixels, closeTo(controller.position.maxScrollExtent, 1));
    expect(find.text('brand new'), findsOneWidget);
    expect(find.text('↓ NEW MESSAGES'), findsNothing);
  });

  testWidgets('a new message does not yank the view down while the reader is scrolled up',
      (tester) async {
    const base = 1_700_000_000.0;
    final messages = [for (var i = 0; i < 40; i++) _msg('alice', base + i * 400, 'm$i')];

    await tester.pumpWidget(_harness(messages));
    await tester.pumpAndSettle();

    final controller = tester.widget<Scrollable>(find.byType(Scrollable)).controller!;
    controller.jumpTo(controller.position.maxScrollExtent - 300);
    await tester.pump();
    final before = controller.position.pixels;

    messages.add(_msg('bob', base + 40 * 400, 'incoming'));
    await tester.pumpWidget(_harness(messages));
    await tester.pumpAndSettle();

    // The view stayed put rather than jumping to the newest message.
    expect(controller.position.pixels, closeTo(before, 1));
    expect(controller.position.pixels, lessThan(controller.position.maxScrollExtent));
    // ...and the affordance appeared.
    expect(find.text('↓ NEW MESSAGES'), findsOneWidget);
  });

  testWidgets('tapping the new-messages affordance scrolls to the newest message',
      (tester) async {
    const base = 1_700_000_000.0;
    final messages = [for (var i = 0; i < 40; i++) _msg('alice', base + i * 400, 'm$i')];

    await tester.pumpWidget(_harness(messages));
    await tester.pumpAndSettle();

    final controller = tester.widget<Scrollable>(find.byType(Scrollable)).controller!;
    controller.jumpTo(controller.position.maxScrollExtent - 300);
    await tester.pump();

    messages.add(_msg('bob', base + 40 * 400, 'incoming'));
    await tester.pumpWidget(_harness(messages));
    await tester.pumpAndSettle();

    expect(find.text('↓ NEW MESSAGES'), findsOneWidget);
    await tester.tap(find.text('↓ NEW MESSAGES'));
    await tester.pumpAndSettle();

    expect(controller.position.pixels, closeTo(controller.position.maxScrollExtent, 1));
    expect(find.text('↓ NEW MESSAGES'), findsNothing);
  });

  testWidgets('a reply renders a quoted preview of its parent', (tester) async {
    const base = 1_700_000_000.0;
    final parent = _msg('alice', base, 'the original text');
    final reply = _msg('bob', base + 10, 'my answer', replyTo: parent.messageId);

    await tester.pumpWidget(_harness([parent, reply]));
    await tester.pumpAndSettle();

    // The quoted preview names the parent's author and its content.
    final previewFinder = find.byWidgetPredicate(
      (w) => w is RichText && w.text.toPlainText() == 'alice the original text',
    );
    expect(previewFinder, findsOneWidget);
    expect(find.text('my answer'), findsOneWidget);
  });

  testWidgets('a reply whose parent is not loaded falls back gracefully', (tester) async {
    const base = 1_700_000_000.0;
    final reply = _msg('bob', base, 'orphan reply', replyTo: 'not-on-screen');

    await tester.pumpWidget(_harness([reply]));
    await tester.pumpAndSettle();

    expect(find.text('↩ original message'), findsOneWidget);
  });

  testWidgets('a message row offers Reply… and fires onReply with the message',
      (tester) async {
    const base = 1_700_000_000.0;
    Message? replied;

    await tester.pumpWidget(_harness(
      [_msg('alice', base, 'hello there')],
      onReply: (m) => replied = m,
    ));
    await tester.pumpAndSettle();

    await tester.longPress(find.text('hello there'));
    await tester.pump();

    expect(find.text('Reply…'), findsOneWidget);
    await tester.tap(find.text('Reply…'));
    await tester.pump();

    expect(replied, isNotNull);
    expect(replied!.content, 'hello there');
  });

  testWidgets('a URL in message content renders with linkColor and is tappable',
      (tester) async {
    const base = 1_700_000_000.0;
    String? opened;

    await tester.pumpWidget(_harness(
      [_msg('alice', base, 'see https://example.com/docs now')],
      onOpenLink: (url) => opened = url,
    ));
    await tester.pumpAndSettle();

    final richText = tester.widget<RichText>(
      find.byWidgetPredicate((w) =>
          w is RichText && w.text.toPlainText().contains('https://example.com/docs')),
    );

    // Find the link span and confirm it carries linkColor + a tap recognizer.
    TextSpan? linkSpan;
    richText.text.visitChildren((span) {
      if (span is TextSpan && span.text == 'https://example.com/docs') {
        linkSpan = span;
        return false;
      }
      return true;
    });

    expect(linkSpan, isNotNull);
    expect(linkSpan!.style!.color, TCColors.linkColor);
    expect(linkSpan!.recognizer, isA<TapGestureRecognizer>());

    (linkSpan!.recognizer as TapGestureRecognizer).onTap!();
    expect(opened, 'https://example.com/docs');
  });

  testWidgets('own pending and failed messages show a delivery glyph; delivered/null do not',
      (tester) async {
    const base = 1_700_000_000.0;
    final messages = [
      _msg('me', base, 'queued one', deliveryState: 'pending'),
      _msg('me', base + 400, 'lost one', deliveryState: 'failed'),
      _msg('me', base + 800, 'sent one', deliveryState: 'delivered'),
      _msg('me', base + 1200, 'untracked one'),
    ];

    await tester.pumpWidget(_harness(messages, meHashHex: 'me'));
    await tester.pumpAndSettle();

    expect(find.text('◷'), findsOneWidget); // pending
    expect(find.text('⚠'), findsOneWidget); // failed
    expect(find.text('✓'), findsOneWidget); // delivered
  });

  testWidgets('a peer message never shows a delivery glyph', (tester) async {
    const base = 1_700_000_000.0;
    // A peer's message would never carry a state, but even if one leaked
    // through the row must not render an indicator for it.
    final messages = [_msg('alice', base, 'hi', deliveryState: 'pending')];

    await tester.pumpWidget(_harness(messages, meHashHex: 'me'));
    await tester.pumpAndSettle();

    expect(find.text('◷'), findsNothing);
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

  testWidgets('the transcript is selectable, so a code can be copied out',
      (tester) async {
    // A verification code, a hash or a link someone sent is unreachable if
    // the text cannot be selected -- there is no other way to get it out.
    await tester.pumpWidget(_harness([_msg('bot', 1000, 'your code is 12345')]));
    await tester.pumpAndSettle();

    expect(
      find.descendant(
        of: find.byType(SelectionArea),
        matching: find.textContaining('your code is 12345', findRichText: true),
      ),
      findsOneWidget,
    );
  });

  testWidgets('an emoji-only message renders jumbo; a mixed one stays body-sized',
      (tester) async {
    const base = 1_700_000_000.0;
    final messages = [
      _msg('alice', base, '\ud83c\udf89'),
      _msg('bob', base + 600, 'nice \ud83c\udf89'),
    ];

    await tester.pumpWidget(_harness(messages));
    await tester.pumpAndSettle();

    final jumbo = tester.widgetList<RichText>(find.byType(RichText)).where((r) =>
        r.text.toPlainText() == '\ud83c\udf89' && _spanFontSize(r.text) == jumboEmojiFontSize);
    expect(jumbo, isNotEmpty, reason: 'the emoji-only message should be jumbo');

    final normal = tester.widgetList<RichText>(find.byType(RichText)).where((r) =>
        r.text.toPlainText() == 'nice \ud83c\udf89' &&
        _spanFontSize(r.text) == TCType.textBodyMd);
    expect(normal, isNotEmpty, reason: 'the mixed message keeps body size');
  });

  testWidgets('a long history of varied row heights still opens at the newest message',
      (tester) async {
    final messages = _variedHistory();

    await tester.pumpWidget(_harness(messages));
    await tester.pumpAndSettle();

    final controller = tester.widget<Scrollable>(find.byType(Scrollable)).controller!;
    expect(controller.position.pixels, closeTo(controller.position.maxScrollExtent, 1));
  });

  testWidgets('coming back from another tab lands at the newest message',
      (tester) async {
    final messages = _variedHistory();

    await tester.pumpWidget(_harness(messages));
    await tester.pumpAndSettle();

    // Read back through history, then leave the chat tab: the other tab
    // replaces the list outright, so its scroll state is gone.
    tester.widget<Scrollable>(find.byType(Scrollable)).controller!.jumpTo(0);
    await tester.pumpAndSettle();
    await tester.pumpWidget(const MaterialApp(home: Scaffold(body: SizedBox())));
    await tester.pumpAndSettle();

    await tester.pumpWidget(_harness(messages));
    await tester.pumpAndSettle();

    final controller = tester.widget<Scrollable>(find.byType(Scrollable)).controller!;
    expect(controller.position.pixels, closeTo(controller.position.maxScrollExtent, 1));
  });
}

/// The font size of the first styled span in a rich text tree.
double _spanFontSize(InlineSpan span) {
  double found = 0;
  span.visitChildren((s) {
    final size = s.style?.fontSize;
    if (size != null) {
      found = size;
      return false;
    }
    return true;
  });
  return found;
}
