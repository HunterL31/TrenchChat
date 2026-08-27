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

/// An admin added us directly: the signed membership record is already held,
/// so there is no token and nothing to expire.
PendingInvite _heldMembership() => const PendingInvite(
      channelHashHex: _channelHash,
      channelName: 'ops',
      expiry: 0,
      adminHex: _adminHash,
      scopeKind: 'channel',
      hasToken: false,
    );

Widget _harness(AppState state, {PendingInvite? invite}) {
  return MaterialApp(
    home: Scaffold(
      body: Builder(
        builder: (context) => ElevatedButton(
          onPressed: () =>
              showIncomingInviteDialog(context, state, invite ?? _invite()),
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

  Future<void> open(WidgetTester tester, {PendingInvite? invite}) async {
    await tester.pumpWidget(_harness(state, invite: invite));
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

  testWidgets('a held membership is offered without an expiry', (tester) async {
    state.pendingInvites = [_heldMembership()];

    await open(tester, invite: _heldMembership());

    expect(find.textContaining('EXPIRED'), findsNothing);
    expect(find.textContaining('AWAITING YOUR CONFIRMATION'), findsOneWidget);
  });
}
