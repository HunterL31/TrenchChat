// Keyboard basics for the add/edit friend dialog: the field the flow starts
// in has focus on open, and Enter submits from any of them.
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/app_state.dart';
import 'package:flutter_ui/screens/dialogs/add_friend_dialog.dart';

import '../../fake_backend.dart';

const _hash = 'f3a1c2d4e5b6a798f3a1c2d4e5b6a798';

Widget _harness(AppState state, {String? identityHash}) => MaterialApp(
      home: Scaffold(
        body: Builder(
          builder: (context) => ElevatedButton(
            onPressed: () =>
                showAddFriendDialog(context, state, identityHash: identityHash),
            child: const Text('open'),
          ),
        ),
      ),
    );

void main() {
  late FakeBackend backend;
  late AppState state;

  setUp(() {
    backend = FakeBackend();
    backend.routes['POST /friends'] = {'ok': true};
    backend.routes['GET /friends'] = <dynamic>[];
    state = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
  });

  tearDown(() => state.dispose());

  testWidgets('the hash field has focus on open, so typing is not lost',
      (tester) async {
    await tester.pumpWidget(_harness(state));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    tester.testTextInput.enterText(_hash);
    await tester.pump();

    expect(find.text(_hash), findsOneWidget);
  });

  testWidgets('with the hash already known, the nickname field takes focus',
      (tester) async {
    await tester.pumpWidget(_harness(state, identityHash: _hash));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    tester.testTextInput.enterText('Alice');
    await tester.pump();

    expect(find.text('Alice'), findsOneWidget);
  });

  testWidgets('Enter submits the friend', (tester) async {
    await tester.pumpWidget(_harness(state));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    await tester.enterText(find.byType(TextField).first, _hash);
    await tester.testTextInput.receiveAction(TextInputAction.done);
    await tester.pumpAndSettle();

    final posted =
        backend.requests.where((r) => r.method == 'POST' && r.path == '/friends').toList();
    expect(posted, hasLength(1));
    expect((jsonDecode(posted.single.body) as Map)['identity_hash'], _hash);
    expect(find.text('Add Friend'), findsNothing); // dialog closed on success
  });
}
