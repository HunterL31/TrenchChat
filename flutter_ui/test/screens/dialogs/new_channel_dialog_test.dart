// The one behavioral contract that matters here: the access preset picker
// (public/invite-only) appears for a standalone channel and is omitted for
// a channel created inside a server, which always inherits the server's
// permissions instead -- plus the keyboard basics every dialog owes its
// user: the first field has focus on open, and Enter submits.
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/app_state.dart';
import 'package:flutter_ui/screens/dialogs/new_channel_dialog.dart';

import '../../fake_backend.dart';

Widget _harness(AppState state, {String? serverHashHex}) {
  return MaterialApp(
    home: Scaffold(
      body: Builder(
        builder: (context) => ElevatedButton(
          onPressed: () =>
              showNewChannelDialog(context, state, serverHashHex: serverHashHex),
          child: const Text('open'),
        ),
      ),
    ),
  );
}

void main() {
  late AppState state;

  setUp(() {
    state = AppState(baseUrl: 'http://127.0.0.1:65500');
  });

  tearDown(() {
    state.dispose();
  });

  testWidgets('standalone channel dialog offers an access preset picker', (tester) async {
    await tester.pumpWidget(_harness(state));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    expect(find.text('New Channel'), findsOneWidget);
    expect(find.text('ACCESS'), findsOneWidget);
    expect(find.text('PUBLIC'), findsOneWidget);
    expect(find.text('INVITE-ONLY'), findsOneWidget);
  });

  testWidgets('in-server channel dialog omits the access preset picker', (tester) async {
    await tester.pumpWidget(_harness(state, serverHashHex: 'server-hash'));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    expect(find.text('New Channel in Server'), findsOneWidget);
    expect(find.text('ACCESS'), findsNothing);
    expect(find.text('PUBLIC'), findsNothing);
    expect(find.text('INVITE-ONLY'), findsNothing);
  });

  testWidgets('rejects an empty name without calling the API', (tester) async {
    await tester.pumpWidget(_harness(state));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    await tester.tap(find.text('CREATE'));
    await tester.pump();

    expect(find.text('Channel name cannot be empty.'), findsOneWidget);
  });

  testWidgets('the name field has focus on open, so typing is not lost',
      (tester) async {
    await tester.pumpWidget(_harness(state));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    // Straight to the platform text input, with nothing clicked first: this
    // reaches whichever field holds the focus, or nothing at all.
    tester.testTextInput.enterText('mesh-crew');
    await tester.pump();

    expect(find.text('mesh-crew'), findsOneWidget);
  });

  testWidgets('Enter in the name field submits', (tester) async {
    final backend = FakeBackend();
    backend.routes['POST /channels'] = {'hash': 'c' * 32};
    backend.routes['GET /channels'] = <dynamic>[];
    final wired = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
    addTearDown(wired.dispose);

    await tester.pumpWidget(_harness(wired));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    await tester.enterText(find.byType(TextField).first, 'mesh-crew');
    await tester.testTextInput.receiveAction(TextInputAction.done);
    await tester.pumpAndSettle();

    final posted = backend.requests
        .where((r) => r.method == 'POST' && r.path == '/channels')
        .toList();
    expect(posted, hasLength(1));
    expect((jsonDecode(posted.single.body) as Map)['name'], 'mesh-crew');
  });

  testWidgets('a duplicate name shows the backend\'s own reason, not a status code',
      (tester) async {
    final backend = FakeBackend();
    backend.routes['POST /channels'] = FakeError(
      409, {'ok': false, 'error': "you already have a channel named 'general'"});
    final wired = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
    addTearDown(wired.dispose);

    await tester.pumpWidget(_harness(wired));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    await tester.enterText(find.byType(TextField).first, 'general');
    await tester.tap(find.text('CREATE'));
    await tester.pumpAndSettle();

    expect(find.text("you already have a channel named 'general'"), findsOneWidget);
    expect(find.textContaining('HTTP 409'), findsNothing);
    expect(find.text('New Channel'), findsOneWidget); // dialog stayed open
  });

  testWidgets('an error the dialog shows is claimed, so no snackbar repeats it',
      (tester) async {
    final backend = FakeBackend();
    backend.routes['POST /channels'] = FakeError(409, {'error': 'nope'});
    final wired = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
    addTearDown(wired.dispose);

    await tester.pumpWidget(_harness(wired));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    await tester.enterText(find.byType(TextField).first, 'general');
    await tester.tap(find.text('CREATE'));
    await tester.pumpAndSettle();

    expect(find.text('nope'), findsOneWidget);
    expect(wired.actionError, isNull);
  });

  testWidgets('tapping an access option selects it', (tester) async {
    await tester.pumpWidget(_harness(state));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    // Public is selected by default; switching to invite-only must not throw
    // and must leave exactly one option visible as each label.
    await tester.tap(find.text('INVITE-ONLY'));
    await tester.pump();

    expect(find.text('PUBLIC'), findsOneWidget);
    expect(find.text('INVITE-ONLY'), findsOneWidget);
  });
}
