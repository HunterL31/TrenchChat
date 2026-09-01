import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/micron/micron_view.dart';

Widget _harness(String source,
        {void Function(String, Map<String, String>)? onLinkTap}) =>
    MaterialApp(
      home: Scaffold(
        body: SingleChildScrollView(
          child: MicronView(source: source, onLinkTap: onLinkTap),
        ),
      ),
    );

TextSpan? _findLinkSpan(WidgetTester tester, String text) {
  TextSpan? found;
  for (final richText in tester.widgetList<RichText>(find.byType(RichText))) {
    richText.text.visitChildren((span) {
      if (span is TextSpan && span.text == text && span.recognizer != null) {
        found = span;
        return false;
      }
      return true;
    });
  }
  return found;
}

void main() {
  testWidgets('renders headings, text, and dividers', (tester) async {
    await tester.pumpWidget(_harness('>Welcome\nSome text\n-'));
    expect(find.textContaining('Welcome', findRichText: true), findsOneWidget);
    expect(find.textContaining('Some text', findRichText: true), findsOneWidget);
  });

  testWidgets('a tapped link fires the callback with the raw micron URL',
      (tester) async {
    final tapped = <String>[];
    await tester.pumpWidget(_harness(
      'go `[here`aabb:/page/next.mu] now',
      onLinkTap: (url, _) => tapped.add(url),
    ));
    final span = _findLinkSpan(tester, 'here');
    expect(span, isNotNull);
    (span!.recognizer as TapGestureRecognizer).onTap!();
    expect(tapped, ['aabb:/page/next.mu']);
  });

  testWidgets('without a tap handler links render as plain styled text',
      (tester) async {
    await tester.pumpWidget(_harness('go `[here`aabb:/page/next.mu] now'));
    expect(_findLinkSpan(tester, 'here'), isNull);
    expect(find.textContaining('here', findRichText: true), findsOneWidget);
  });

  testWidgets('source updates re-render the document', (tester) async {
    Widget page(String source) => _harness(source);
    await tester.pumpWidget(page('first page'));
    expect(find.textContaining('first page', findRichText: true),
        findsOneWidget);
    await tester.pumpWidget(page('second page'));
    expect(find.textContaining('second page', findRichText: true),
        findsOneWidget);
    expect(
        find.textContaining('first page', findRichText: true), findsNothing);
  });

  testWidgets('a text field is editable and submits what was typed',
      (tester) async {
    Map<String, String>? sent;
    await tester.pumpWidget(_harness(
      'Name: `<24|user`guest>\n`[Send`:/page/e.mu`*]',
      onLinkTap: (_, data) => sent = data,
    ));
    expect(find.byType(TextField), findsOneWidget);
    await tester.enterText(find.byType(TextField), 'nomad');
    await tester.pump();
    (_findLinkSpan(tester, 'Send')!.recognizer as TapGestureRecognizer).onTap!();
    expect(sent, {'field_user': 'nomad'});
  });

  testWidgets('a masked field hides its starting text', (tester) async {
    await tester.pumpWidget(_harness('`<!16|pin`hunter2>'));
    final field = tester.widget<TextField>(find.byType(TextField));
    expect(field.obscureText, isTrue);
  });

  testWidgets('a checkbox submits its value only when checked',
      (tester) async {
    Map<String, String>? sent;
    await tester.pumpWidget(_harness(
      '`<?|opt|yes`>\n`[Send`:/page/e.mu`*]',
      onLinkTap: (_, data) => sent = data,
    ));
    (_findLinkSpan(tester, 'Send')!.recognizer as TapGestureRecognizer).onTap!();
    expect(sent, isEmpty);
    await tester.tap(find.text('[ ]'));
    await tester.pump();
    (_findLinkSpan(tester, 'Send')!.recognizer as TapGestureRecognizer).onTap!();
    expect(sent, {'field_opt': 'yes'});
  });

  testWidgets('a link field list picks only the named fields and variables',
      (tester) async {
    Map<String, String>? sent;
    await tester.pumpWidget(_harness(
      '`<a`one>`<b`two>\n`[Send`:/page/e.mu`a|mode=view]',
      onLinkTap: (_, data) => sent = data,
    ));
    (_findLinkSpan(tester, 'Send')!.recognizer as TapGestureRecognizer).onTap!();
    expect(sent, {'field_a': 'one', 'var_mode': 'view'});
  });

  testWidgets('an anchor link scrolls instead of navigating', (tester) async {
    final tapped = <String>[];
    await tester.pumpWidget(_harness(
      '`[Jump`#the-end]\n${'filler\n' * 200}>The end',
      onLinkTap: (url, _) => tapped.add(url),
    ));
    (_findLinkSpan(tester, 'Jump')!.recognizer as TapGestureRecognizer).onTap!();
    await tester.pumpAndSettle();
    expect(tapped, isEmpty);
  });

  testWidgets('a background-coloured space run keeps its width',
      (tester) async {
    await tester.pumpWidget(_harness('`B900 `Bf00 `b'));
    expect(find.textContaining('\u00A0', findRichText: true), findsOneWidget);
  });
}
