import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/events.dart';
import 'package:flutter_ui/app_state.dart';
import 'package:flutter_ui/screens/dialogs/invite_dialog.dart';
import 'package:flutter_ui/widgets/tc_button.dart';

import '../../fake_backend.dart';

const _channelHash = 'channel-ops';
const _peerHash = 'f3a1c2d4e5b6a798f3a1c2d4e5b6a798';

Widget _harness(AppState state) {
  return MaterialApp(
    home: Scaffold(
      body: Builder(
        builder: (context) => ElevatedButton(
          onPressed: () => showInviteDialog(
            context,
            state,
            channelHashHex: _channelHash,
            channelName: 'ops',
          ),
          child: const Text('open'),
        ),
      ),
    ),
  );
}

void main() {
  late FakeBackend backend;
  late AppState state;

  setUp(() {
    backend = FakeBackend();
    backend.routes['GET /directory'] = [
      {'identity_hash': _peerHash, 'display_name': 'Alice', 'is_online': true},
    ];
    backend.routes['POST /channels/$_channelHash/invite'] = {'ok': true};
    state = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
  });

  tearDown(() {
    state.dispose();
  });

  Future<void> open(WidgetTester tester) async {
    await tester.pumpWidget(_harness(state));
    await tester.tap(find.text('open'));
    await tester.pump();
    await settle(tester);
  }

  TcPrimaryButton inviteButton(WidgetTester tester) =>
      tester.widget<TcPrimaryButton>(find.widgetWithText(TcPrimaryButton, 'INVITE'));

  testWidgets('lists discovered users and disables INVITE with no selection',
      (tester) async {
    await open(tester);

    expect(find.text('Invite to #ops'), findsOneWidget);
    expect(find.text('Alice'), findsOneWidget);
    expect(inviteButton(tester).onPressed, isNull);
  });

  testWidgets('selecting a discovered user enables INVITE and posts the invite',
      (tester) async {
    await open(tester);

    await tester.tap(find.text('Alice'));
    await tester.pump();
    expect(inviteButton(tester).onPressed, isNotNull);

    await tester.tap(find.widgetWithText(TcPrimaryButton, 'INVITE'));
    await settle(tester);
    await tester.pumpAndSettle();

    expect(find.text('Invite to #ops'), findsNothing);
    final post = backend.requests
        .singleWhere((r) => r.method == 'POST' && r.path.endsWith('/invite'));
    expect(post.body, contains(_peerHash));
  });

  testWidgets('a directory_updated event refreshes the shown name live',
      (tester) async {
    await open(tester);
    expect(find.text('Alice'), findsOneWidget);

    state.applyEvent(const DirectoryUpdatedEvent(_peerHash, 'Alicia'));
    await tester.pump();

    expect(find.text('Alicia'), findsOneWidget);
    expect(find.text('Alice'), findsNothing);
  });

  testWidgets('manual hash entry enables INVITE only for a valid 32-char hex',
      (tester) async {
    await open(tester);

    final manualField = find.byType(TextField).last;
    await tester.enterText(manualField, 'not-a-hash');
    await tester.pump();
    expect(inviteButton(tester).onPressed, isNull);

    await tester.enterText(manualField, _peerHash);
    await tester.pump();
    expect(inviteButton(tester).onPressed, isNotNull);
  });

  testWidgets('rejects inviting your own identity hash', (tester) async {
    const selfHash = 'a9f13c02e7d84b119876543210fedcba';
    state.meHashHex = selfHash;
    await open(tester);

    final manualField = find.byType(TextField).last;
    await tester.enterText(manualField, selfHash);
    await tester.pump();
    expect(inviteButton(tester).onPressed, isNull);
    expect(find.text("You can't invite yourself."), findsOneWidget);

    await tester.enterText(manualField, _peerHash);
    await tester.pump();
    expect(inviteButton(tester).onPressed, isNotNull);
    expect(find.text("You can't invite yourself."), findsNothing);
  });
}
