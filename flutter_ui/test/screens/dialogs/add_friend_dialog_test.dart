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

  group('adding by LXMF address', () {
    testWidgets('the field says which kind of hash it wants', (tester) async {
      await tester.pumpWidget(_harness(state));
      await tester.tap(find.text('open'));
      await tester.pumpAndSettle();
      expect(find.text('Identity hash'), findsOneWidget);

      await tester.tap(find.text('LXMF ADDRESS'));
      await tester.pumpAndSettle();

      expect(find.text('LXMF address'), findsOneWidget);
      expect(find.text('Identity hash'), findsNothing);
    });

    testWidgets('REQUEST is hidden for an address, which cannot answer one',
        (tester) async {
      await tester.pumpWidget(_harness(state));
      await tester.tap(find.text('open'));
      await tester.pumpAndSettle();
      expect(find.text('REQUEST'), findsOneWidget);

      await tester.tap(find.text('LXMF ADDRESS'));
      await tester.pumpAndSettle();

      expect(find.text('REQUEST'), findsNothing);
    });

    testWidgets('an address is sent to the resolving endpoint, not /friends',
        (tester) async {
      backend.routes['POST /friends/lxmf'] = {
        'ok': true,
        'state': 'added',
        'identity_hash': _hash,
      };
      await tester.pumpWidget(_harness(state));
      await tester.tap(find.text('open'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('LXMF ADDRESS'));
      await tester.pumpAndSettle();
      tester.testTextInput.enterText(_hash);
      await tester.pump();

      await tester.tap(find.text('ADD'));
      await tester.pumpAndSettle();

      expect(backend.requests.where((r) => r.path == '/friends/lxmf'),
          isNotEmpty);
      // GET /friends is the refresh afterwards; the save itself must not
      // have gone to the identity-hash endpoint.
      expect(
          backend.requests.where(
              (r) => r.method == 'POST' && r.path == '/friends'),
          isEmpty);
    });

    testWidgets('an address with no announce yet says so and stays open',
        (tester) async {
      backend.routes['POST /friends/lxmf'] = {
        'ok': true,
        'state': 'resolving',
        'identity_hash': null,
      };
      await tester.pumpWidget(_harness(state));
      await tester.tap(find.text('open'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('LXMF ADDRESS'));
      await tester.pumpAndSettle();
      tester.testTextInput.enterText(_hash);
      await tester.pump();

      await tester.tap(find.text('ADD'));
      await tester.pumpAndSettle();

      expect(find.textContaining('once its announce arrives'), findsOneWidget);
      expect(find.text('LXMF address'), findsOneWidget);
    });
  });
}
