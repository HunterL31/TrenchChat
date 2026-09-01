import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/micron/micron_view.dart';

Widget _harness(String source,
        {void Function(String, Map<String, String>)? onLinkTap,
        Future<String?> Function(String, Map<String, String>)? onPartialLoad,
        String? initialAnchor}) =>
    MaterialApp(
      home: Scaffold(
        body: SingleChildScrollView(
          child: MicronView(
            source: source,
            onLinkTap: onLinkTap,
            onPartialLoad: onPartialLoad,
            initialAnchor: initialAnchor,
          ),
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

  group('page colours', () {
    testWidgets('a #!bg= header paints behind the page', (tester) async {
      await tester.pumpWidget(_harness('#!bg=123\nhello'));
      final painted = tester.widgetList<Container>(find.byType(Container))
          .where((c) => c.color == const Color(0xFF112233));
      expect(painted, isNotEmpty);
    });

    testWidgets('a page without the header paints nothing of its own',
        (tester) async {
      await tester.pumpWidget(_harness('hello'));
      final painted = tester.widgetList<Container>(find.byType(Container))
          .where((c) => c.color == const Color(0xFF112233));
      expect(painted, isEmpty);
    });
  });

  group('partials', () {
    testWidgets('a partial is fetched and its content rendered',
        (tester) async {
      final asked = <String>[];
      await tester.pumpWidget(_harness(
        'before\n`{:/page/side.mu}\nafter',
        onPartialLoad: (url, _) async {
          asked.add(url);
          return 'from the partial';
        },
      ));
      await tester.pumpAndSettle();
      expect(asked, [':/page/side.mu']);
      expect(find.textContaining('from the partial', findRichText: true),
          findsOneWidget);
    });

    testWidgets('a partial that cannot be fetched says so', (tester) async {
      await tester.pumpWidget(_harness(
        '`{:/page/side.mu}',
        onPartialLoad: (url, _) async => null,
      ));
      await tester.pumpAndSettle();
      expect(find.textContaining('Could not load', findRichText: true),
          findsOneWidget);
    });

    testWidgets('with no loader a partial stays an unloaded placeholder',
        (tester) async {
      await tester.pumpWidget(_harness('`{:/page/side.mu}'));
      await tester.pump();
      expect(find.textContaining('⧖', findRichText: true), findsOneWidget);
    });

    testWidgets('a partial submits the fields its tag names', (tester) async {
      Map<String, String>? sent;
      await tester.pumpWidget(_harness(
        'Name: `<12|who`ada>\n`{:/page/side.mu`0`who}',
        onPartialLoad: (url, data) async {
          sent = data;
          return 'ok';
        },
      ));
      await tester.pumpAndSettle();
      expect(sent, {'field_who': 'ada'});
    });

    testWidgets('a p: link reloads the partial it names', (tester) async {
      var loads = 0;
      await tester.pumpWidget(_harness(
        '`{:/page/side.mu`0`pid=side}\n`[Refresh`p:side]',
        onPartialLoad: (url, _) async {
          loads++;
          return 'load $loads';
        },
        onLinkTap: (url, _) => fail('p: links never navigate'),
      ));
      await tester.pumpAndSettle();
      expect(loads, 1);

      (_findLinkSpan(tester, 'Refresh')!.recognizer as TapGestureRecognizer)
          .onTap!();
      await tester.pumpAndSettle();
      expect(loads, 2);
      expect(find.textContaining('load 2', findRichText: true), findsOneWidget);
    });

    testWidgets('a p: link naming no partial of ours changes nothing',
        (tester) async {
      var loads = 0;
      await tester.pumpWidget(_harness(
        '`{:/page/side.mu`0`pid=side}\n`[Refresh`p:other]',
        onPartialLoad: (url, _) async {
          loads++;
          return 'loaded';
        },
        onLinkTap: (url, _) => fail('p: links never navigate'),
      ));
      await tester.pumpAndSettle();
      (_findLinkSpan(tester, 'Refresh')!.recognizer as TapGestureRecognizer)
          .onTap!();
      await tester.pumpAndSettle();
      expect(loads, 1);
    });

    testWidgets('replacing the page stops the old partial refreshing',
        (tester) async {
      var loads = 0;
      Widget page(String source) => _harness(
            source,
            onPartialLoad: (url, _) async {
              loads++;
              return 'loaded';
            },
          );
      await tester.pumpWidget(page('`{:/page/side.mu`1}'));
      await tester.pumpAndSettle();
      await tester.pumpWidget(page('plain page'));
      await tester.pumpAndSettle();
      final after = loads;
      await tester.pump(const Duration(seconds: 3));
      expect(loads, after);
    });
  });

  testWidgets('an initial anchor scrolls the page to it', (tester) async {
    await tester.pumpWidget(_harness(
      '${'filler\n' * 200}>The end\ntail',
      initialAnchor: 'the-end',
    ));
    await tester.pumpAndSettle();
    final scrollable = tester.widget<Scrollable>(find.byType(Scrollable).first);
    expect(scrollable.controller?.offset ?? 0, greaterThan(0));
  });

  testWidgets('an initial anchor nothing declares leaves the page alone',
      (tester) async {
    await tester.pumpWidget(_harness(
      '${'filler\n' * 200}>The end',
      initialAnchor: 'nowhere',
    ));
    await tester.pumpAndSettle();
    final scrollable = tester.widget<Scrollable>(find.byType(Scrollable).first);
    expect(scrollable.controller?.offset ?? 0, 0);
  });
}
