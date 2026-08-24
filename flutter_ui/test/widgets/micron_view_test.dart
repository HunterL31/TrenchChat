import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/micron/micron_view.dart';

Widget _harness(String source, {void Function(String)? onLinkTap}) =>
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
      onLinkTap: tapped.add,
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

  testWidgets('input fields render as inert placeholders', (tester) async {
    await tester.pumpWidget(_harness('Name: `<24|user`guest>'));
    expect(find.textContaining('[ guest ]', findRichText: true), findsOneWidget);
  });
}
