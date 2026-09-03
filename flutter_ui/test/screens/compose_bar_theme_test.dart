// A theme staged from the appearance editor must land in the draft as a
// short token and go out as the full code -- and stay unsent if the user
// deletes the token.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:flutter_ui/attachments.dart';
import 'package:flutter_ui/screens/main_window/compose_bar.dart';
import 'package:flutter_ui/theme/theme_code.dart';
import 'package:flutter_ui/theme/theme_spec.dart';

final ThemeSpec _spec = ThemeSpec(base: {'bgApp': const Color(0xFF221100)});
final String _code = encodeThemeCode('Deep', _spec);

Widget _harness({
  required Future<bool> Function(String, PickedAttachment?) onSend,
  ({String name, String code})? staged,
  VoidCallback? onConsumed,
  bool peerReadsTrenchchat = true,
}) =>
    MaterialApp(
      home: Scaffold(
        body: ComposeBar(
          channelName: 'general',
          enabled: true,
          onSend: onSend,
          pendingThemeShare: staged,
          onThemeShareConsumed: onConsumed,
          peerReadsTrenchchat: peerReadsTrenchchat,
          compact: true, // gives a tappable send button
        ),
      ),
    );

String _draft(WidgetTester tester) =>
    tester.widget<TextField>(find.byType(TextField)).controller!.text;

Future<void> _send(WidgetTester tester) async {
  await tester.tap(find.byTooltip('Send'));
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('a staged share drops its token into the draft, not its code',
      (tester) async {
    var consumed = 0;
    await tester.pumpWidget(_harness(
      onSend: (_, _) async => true,
      staged: (name: 'Deep', code: _code),
      onConsumed: () => consumed++,
    ));
    await tester.pumpAndSettle();

    expect(find.text('[theme:Deep]'), findsOneWidget);
    expect(find.textContaining(_code), findsNothing);
    expect(consumed, 1);
  });

  testWidgets('nothing is staged when nothing was shared', (tester) async {
    await tester.pumpWidget(_harness(onSend: (_, _) async => true));
    await tester.pumpAndSettle();

    expect(find.textContaining('[theme:'), findsNothing);
  });

  testWidgets('sending expands the token to the theme code', (tester) async {
    String? sent;
    await tester.pumpWidget(_harness(
      onSend: (c, _) async {
        sent = c;
        return true;
      },
      staged: (name: 'Deep', code: _code),
    ));
    await tester.pumpAndSettle();

    await tester.enterText(find.byType(TextField), 'try [theme:Deep] out');
    await _send(tester);

    expect(sent, 'try $_code out');
    expect(decodeThemeCode(_code)!.spec, _spec);
  });

  testWidgets('deleting the token sends the message without it', (tester) async {
    String? sent;
    await tester.pumpWidget(_harness(
      onSend: (c, _) async {
        sent = c;
        return true;
      },
      staged: (name: 'Deep', code: _code),
    ));
    await tester.pumpAndSettle();

    await tester.enterText(find.byType(TextField), 'changed my mind');
    await _send(tester);

    expect(sent, 'changed my mind');
  });

  testWidgets('backspacing into the token takes the whole token out',
      (tester) async {
    String? sent;
    await tester.pumpWidget(_harness(
      onSend: (c, _) async {
        sent = c;
        return true;
      },
      staged: (name: 'Deep', code: _code),
    ));
    await tester.pumpAndSettle();

    // One backspace at the end of the token: its ']' is gone.
    await tester.enterText(find.byType(TextField), 'try [theme:Deep');
    await tester.pumpAndSettle();

    expect(_draft(tester), 'try ');
    expect(find.textContaining('[theme:'), findsNothing);

    await tester.enterText(find.byType(TextField), 'try something else');
    await _send(tester);

    expect(sent, 'try something else');
    expect(sent, isNot(contains(_code)));
  });

  testWidgets('a delete inside the name takes the whole token out too',
      (tester) async {
    await tester.pumpWidget(_harness(
      onSend: (_, _) async => true,
      staged: (name: 'Deep', code: _code),
    ));
    await tester.pumpAndSettle();

    await tester.enterText(find.byType(TextField), 'a [theme:Dep] b');
    await tester.pumpAndSettle();

    expect(_draft(tester), 'a  b');
  });

  testWidgets('a token deleted in full leaves the rest of the draft alone',
      (tester) async {
    String? sent;
    await tester.pumpWidget(_harness(
      onSend: (c, _) async {
        sent = c;
        return true;
      },
      staged: (name: 'Deep', code: _code),
    ));
    await tester.pumpAndSettle();

    await tester.enterText(find.byType(TextField), 'changed my mind');
    await tester.pumpAndSettle();

    expect(_draft(tester), 'changed my mind');

    await _send(tester);
    expect(sent, 'changed my mind');
  });

  testWidgets('an unrelated theme token the user typed is left alone',
      (tester) async {
    String? sent;
    await tester.pumpWidget(_harness(
      onSend: (c, _) async {
        sent = c;
        return true;
      },
      staged: (name: 'Deep', code: _code),
    ));
    await tester.pumpAndSettle();

    await tester.enterText(find.byType(TextField), 'about [theme:Other] really');
    await tester.pumpAndSettle();

    expect(_draft(tester), 'about [theme:Other] really');

    await _send(tester);
    expect(sent, 'about [theme:Other] really');
  });

  testWidgets('the token is appended after what is already typed', (tester) async {
    await tester.pumpWidget(_harness(onSend: (_, _) async => true));
    await tester.pumpAndSettle();

    await tester.enterText(find.byType(TextField), 'look at');
    await tester.pumpWidget(_harness(
      onSend: (_, _) async => true,
      staged: (name: 'Deep', code: _code),
    ));
    await tester.pumpAndSettle();

    expect(find.text('look at [theme:Deep]'), findsOneWidget);
  });

  testWidgets('a plain LXMF peer gets the label, never the code',
      (tester) async {
    String? sent;
    await tester.pumpWidget(_harness(
      onSend: (c, _) async {
        sent = c;
        return true;
      },
      staged: (name: 'Deep', code: _code),
      peerReadsTrenchchat: false,
    ));
    await tester.pumpAndSettle();
    await _send(tester);

    expect(sent, '[theme:Deep]');
  });
}
