import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/models/invite.dart';
import 'package:flutter_ui/app_state.dart';
import 'package:flutter_ui/screens/dialogs/incoming_invite_dialog.dart';

import '../../fake_backend.dart';

const _channelHash = 'channel-ops';
const _adminHash = 'f3a1c2d4e5b6a798f3a1c2d4e5b6a798';

PendingInvite _invite() => PendingInvite(
      channelHashHex: _channelHash,
      channelName: 'ops',
      expiry: DateTime.now().millisecondsSinceEpoch / 1000 + 3600 * 24,
      adminHex: _adminHash,
      scopeKind: 'channel',
    );

Widget _harness(AppState state) {
  return MaterialApp(
    home: Scaffold(
      body: Builder(
        builder: (context) => ElevatedButton(
          onPressed: () => showIncomingInviteDialog(context, state, _invite()),
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
    backend.routes['POST /invites/$_channelHash/decline'] = {'ok': true};
    state = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
    state.pendingInvites = [_invite()];
  });

  tearDown(() {
    state.dispose();
  });

  Future<void> open(WidgetTester tester) async {
    await tester.pumpWidget(_harness(state));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();
  }

  testWidgets('shows the inviter, expiry, and accept/decline actions', (tester) async {
    await open(tester);

    expect(find.text('Invite — #ops'), findsOneWidget);
    expect(find.text(_adminHash), findsOneWidget);
    expect(find.textContaining('EXPIRES IN'), findsOneWidget);
    expect(find.text('ACCEPT'), findsOneWidget);
    expect(find.text('DECLINE'), findsOneWidget);
  });

  testWidgets('decline removes the pending invite and closes', (tester) async {
    await open(tester);

    await tester.tap(find.text('DECLINE'));
    await settle(tester);
    await tester.pumpAndSettle();

    expect(find.text('Invite — #ops'), findsNothing);
    expect(state.pendingInvites, isEmpty);
    expect(
      backend.requests.where((r) => r.path.endsWith('/decline')),
      hasLength(1),
    );
  });
}
