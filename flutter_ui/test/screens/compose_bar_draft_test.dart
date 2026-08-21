// A draft belongs to the channel it was typed in. Switching channels must
// leave it behind and bring back whatever was left in the one being opened --
// including the token maps, so a picked emoji or a staged theme still expands
// when the reader comes back to finish the message.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/screens/main_window/compose_bar.dart';
import 'package:flutter_ui/theme/theme_code.dart';
import 'package:flutter_ui/theme/theme_spec.dart';
import 'package:flutter_ui/widgets/tc_icon.dart';

final ThemeSpec _spec = ThemeSpec(base: {'bgApp': const Color(0xFF221100)});
final String _code = encodeThemeCode('Deep', _spec);

const _emojiHash =
    'aa11bb22cc33dd44ee55ff6677889900aa11bb22cc33dd44ee55ff6677889900';

/// Rebuilds the same ComposeBar element with a different channel, the way the
/// shell does when the reader picks another row.
class _Host extends StatefulWidget {
  const _Host({super.key, required this.onSend, this.pickEmoji, this.staged});

  final Future<bool> Function(String) onSend;
  final Future<String?> Function()? pickEmoji;
  final ({String name, String code})? staged;

  @override
  State<_Host> createState() => _HostState();
}

class _HostState extends State<_Host> {
  String _hash = 'hash-a';
  String _name = 'alpha';
  ({String name, String code})? _staged;

  @override
  void initState() {
    super.initState();
    _staged = widget.staged;
  }

  void select(String hash, String name) => setState(() {
        _hash = hash;
        _name = name;
      });

  @override
  Widget build(BuildContext context) => MaterialApp(
        home: Scaffold(
          body: ComposeBar(
            channelHash: _hash,
            channelName: _name,
            enabled: true,
            onSend: widget.onSend,
            pickEmoji: widget.pickEmoji,
            pendingThemeShare: _staged,
            onThemeShareConsumed: () => setState(() => _staged = null),
            compact: true, // gives a tappable send button
          ),
        ),
      );
}

String _draft(WidgetTester tester) =>
    tester.widget<TextField>(find.byType(TextField)).controller!.text;

Future<void> _select(WidgetTester tester, GlobalKey<_HostState> key,
    String hash, String name) async {
  key.currentState!.select(hash, name);
  await tester.pumpAndSettle();
}

Future<void> _send(WidgetTester tester) async {
  await tester.tap(find.byTooltip('Send'));
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('a draft stays in the channel it was typed in', (tester) async {
    final key = GlobalKey<_HostState>();
    await tester.pumpWidget(_Host(key: key, onSend: (_) async => true));

    await tester.enterText(find.byType(TextField), 'half a thought');
    await tester.pump();

    await _select(tester, key, 'hash-b', 'beta');
    expect(_draft(tester), '');

    await _select(tester, key, 'hash-a', 'alpha');
    expect(_draft(tester), 'half a thought');
  });

  testWidgets('each channel keeps its own draft', (tester) async {
    final key = GlobalKey<_HostState>();
    await tester.pumpWidget(_Host(key: key, onSend: (_) async => true));

    await tester.enterText(find.byType(TextField), 'for alpha');
    await tester.pump();
    await _select(tester, key, 'hash-b', 'beta');
    await tester.enterText(find.byType(TextField), 'for beta');
    await tester.pump();

    await _select(tester, key, 'hash-a', 'alpha');
    expect(_draft(tester), 'for alpha');
    await _select(tester, key, 'hash-b', 'beta');
    expect(_draft(tester), 'for beta');
  });

  testWidgets('sending in one channel sends only that channel\'s words',
      (tester) async {
    final sent = <String>[];
    final key = GlobalKey<_HostState>();
    await tester.pumpWidget(_Host(
      key: key,
      onSend: (content) async {
        sent.add(content);
        return true;
      },
    ));

    await tester.enterText(find.byType(TextField), 'for alpha');
    await tester.pump();
    await _select(tester, key, 'hash-b', 'beta');
    await tester.enterText(find.byType(TextField), 'for beta');
    await tester.pump();
    await _send(tester);

    expect(sent, ['for beta']);

    await _select(tester, key, 'hash-a', 'alpha');
    expect(_draft(tester), 'for alpha');
  });

  testWidgets('a picked emoji still expands after leaving and coming back',
      (tester) async {
    final sent = <String>[];
    final key = GlobalKey<_HostState>();
    await tester.pumpWidget(_Host(
      key: key,
      onSend: (content) async {
        sent.add(content);
        return true;
      },
      pickEmoji: () async => ':salute@$_emojiHash:',
    ));

    await tester.tap(find.ancestor(
      of: find.byWidgetPredicate((w) => w is TcIcon && w.icon == TcIcons.emoji),
      matching: find.byType(GestureDetector),
    ));
    await tester.pumpAndSettle();
    expect(_draft(tester), ':salute:');

    await _select(tester, key, 'hash-b', 'beta');
    await _select(tester, key, 'hash-a', 'alpha');
    await _send(tester);

    expect(sent, [':salute@$_emojiHash:']);
  });

  testWidgets('a theme staged mid-switch lands in the channel now open',
      (tester) async {
    final sent = <String>[];
    final key = GlobalKey<_HostState>();
    await tester.pumpWidget(_Host(
      key: key,
      onSend: (content) async {
        sent.add(content);
        return true;
      },
      staged: (name: 'Deep', code: _code),
    ));
    await tester.pumpAndSettle();
    expect(_draft(tester), '[theme:Deep]');

    await _select(tester, key, 'hash-b', 'beta');
    expect(_draft(tester), '');

    await _select(tester, key, 'hash-a', 'alpha');
    await _send(tester);
    expect(sent, [_code]);
  });
}
