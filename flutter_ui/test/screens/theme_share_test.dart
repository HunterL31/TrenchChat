// A theme code in a message: the inline chip, the preview card, and ADD
// putting the theme in the library. A code this client cannot read must stay
// readable as text.
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/models/message.dart';
import 'package:flutter_ui/app_state.dart';
import 'package:flutter_ui/screens/main_window/message_list.dart';
import 'package:flutter_ui/theme/theme_code.dart';
import 'package:flutter_ui/theme/theme_spec.dart';
import 'package:flutter_ui/widgets/theme_share.dart';

import '../fake_backend.dart';

final ThemeSpec _shared = ThemeSpec(
  base: {'bgApp': const Color(0xFF221100), 'accentPrimary': const Color(0xFFFFAA00)},
  styles: {
    'base': {'glow': false},
  },
);

Message _msg(String content) => Message(
      messageId: 'm1',
      senderHash: 'alice',
      senderName: 'alice',
      content: content,
      timestamp: 1_700_000_000,
      replyTo: null,
      hasImage: false,
      reactions: const [],
      imageStripped: false,
    );

Widget _harness(AppState state, String content) => MaterialApp(
      home: Scaffold(
        body: SizedBox(
          width: 800,
          height: 600,
          child: MessageList(
            messages: [_msg(content)],
            meHashHex: 'me',
            displayNameFor: (hash, fallback) => fallback,
            onAddTheme: state.saveThemeAs,
          ),
        ),
      ),
    );

void main() {
  late FakeBackend backend;
  late AppState state;

  setUp(() {
    backend = FakeBackend();
    state = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
  });

  tearDown(() {
    state.dispose();
  });

  testWidgets('a shared code renders a chip and a preview card', (tester) async {
    final code = encodeThemeCode('Deep', _shared);

    await tester.pumpWidget(_harness(state, 'try this $code'));
    await tester.pumpAndSettle();

    expect(find.text('[THEME: Deep]'), findsOneWidget);
    expect(find.byType(ThemeCodeCard), findsOneWidget);
    expect(find.text('SHARED THEME'), findsOneWidget);
    expect(find.text('Deep'), findsOneWidget);
    expect(find.text('ADD'), findsOneWidget);
    // The raw token is gone from the text; the words around it are not.
    expect(find.textContaining(code), findsNothing);
  });

  testWidgets('ADD saves the theme under its own name and says so', (tester) async {
    final code = encodeThemeCode('Deep', _shared);

    await tester.pumpWidget(_harness(state, code));
    await tester.pumpAndSettle();

    await tester.tap(find.text('ADD'));
    await tester.pumpAndSettle();

    final post = backend.requests.lastWhere((r) => r.path == '/ui_theme_library');
    final body = jsonDecode(post.body) as Map<String, dynamic>;
    expect(post.method, 'POST');
    expect(body['name'], 'Deep');
    expect(((body['theme'] as Map)['base'] as Map)['bgApp'], '#221100');
    expect(state.themeLibrary['Deep'], _shared);
    expect(find.text('ADDED'), findsOneWidget);
    expect(find.text('ADD'), findsNothing);
  });

  testWidgets('a failed ADD leaves the button offering to try again', (tester) async {
    backend.routes.remove('POST /ui_theme_library');
    await tester.pumpWidget(_harness(state, encodeThemeCode('Deep', _shared)));
    await tester.pumpAndSettle();

    await tester.tap(find.text('ADD'));
    await tester.pumpAndSettle();

    expect(find.text('ADD'), findsOneWidget);
    expect(state.themeLibrary, isEmpty);
    expect(state.actionError, isNotNull);
  });

  testWidgets('an unreadable code stays literal text', (tester) async {
    const content = 'look: tct1:notarealcode and tct9:whatever';

    await tester.pumpWidget(_harness(state, content));
    await tester.pumpAndSettle();

    expect(find.text(content), findsOneWidget);
    expect(find.byType(ThemeCodeCard), findsNothing);
  });

  testWidgets('two codes in one message get one card each', (tester) async {
    final first = encodeThemeCode('Deep', _shared);
    final second = encodeThemeCode('Shallow', ThemeSpec.empty);

    await tester.pumpWidget(_harness(state, '$first vs $second'));
    await tester.pumpAndSettle();

    expect(find.byType(ThemeCodeCard), findsNWidgets(2));
    expect(find.text('[THEME: Deep]'), findsOneWidget);
    expect(find.text('[THEME: Shallow]'), findsOneWidget);
  });
}
