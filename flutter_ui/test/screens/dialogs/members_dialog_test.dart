import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/models/invite.dart';
import 'package:flutter_ui/api/models/member.dart';
import 'package:flutter_ui/api/models/permissions.dart';
import 'package:flutter_ui/app_state.dart';
import 'package:flutter_ui/screens/dialogs/members_dialog.dart';

import '../../fake_backend.dart';

const _channelHash = 'channel-general';
const _selfHash = 'a9f13c02e7d84b119876543210fedcba';
const _aliceHash = 'f3a1c2d4e5b6a798f3a1c2d4e5b6a798';
const _bobHash = '7b8d41aa9c2e7b8d41aa9c2e7b8d41aa';

Member _member(String hash, String name, String role) => Member(
      channelHash: _channelHash,
      identityHash: hash,
      displayName: name,
      role: role,
      addedAt: 0,
    );

Widget _harness(AppState state) {
  return MaterialApp(
    home: Scaffold(
      body: Builder(
        builder: (context) => ElevatedButton(
          onPressed: () => showMembersDialog(
            context,
            state,
            channelHashHex: _channelHash,
            channelName: 'general',
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
    backend.routes['POST /channels/$_channelHash/roles'] = {'ok': true};
    backend.routes['GET /channels/$_channelHash/members'] = [];
    state = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
    state.meHashHex = _selfHash;
    state.membersByChannel[_channelHash] = [
      _member(_aliceHash, 'Alice', 'owner'),
      _member(_selfHash, 'operator', 'admin'),
      _member(_bobHash, 'Bob', 'member'),
    ];
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

  testWidgets('renders members with role tags and self marker', (tester) async {
    await open(tester);

    expect(find.text('Members — #general'), findsOneWidget);
    expect(find.text('Alice'), findsOneWidget);
    expect(find.text('OWNER'), findsOneWidget);
    expect(find.text('operator  (you)'), findsOneWidget);
    expect(find.text('Bob'), findsOneWidget);
  });

  testWidgets('a member with no display name resolves via the directory, not a raw hash',
      (tester) async {
    state.membersByChannel[_channelHash] = [
      _member(_bobHash, '', 'member'),
    ];
    state.directory = const [
      DirectoryEntry(identityHash: _bobHash, displayName: 'Bob Announced', isOnline: false),
    ];
    await open(tester);

    expect(find.text('Bob Announced'), findsOneWidget);
    expect(find.textContaining(_bobHash.substring(0, 12)), findsNothing);
  });

  testWidgets('a member with no name anywhere falls back to a truncated hash',
      (tester) async {
    state.membersByChannel[_channelHash] = [
      _member(_bobHash, '', 'member'),
    ];
    await open(tester);

    expect(find.text('${_bobHash.substring(0, 12)}…'), findsOneWidget);
  });

  testWidgets('kick and admin controls are hidden without permissions', (tester) async {
    state.permissionsByChannel[_channelHash] = const ChannelPermissions(
      invite: false,
      kick: false,
      manageRoles: false,
      manageChannel: false,
      sendMessage: true,
      voiceChat: true,
    );
    await open(tester);

    expect(find.text('KICK'), findsNothing);
    expect(find.text('+ADMIN'), findsNothing);
    expect(find.text('INVITE'), findsNothing);
  });

  testWidgets('kick asks for inline confirmation, then posts the removal', (tester) async {
    state.permissionsByChannel[_channelHash] = const ChannelPermissions(
      invite: true,
      kick: true,
      manageRoles: true,
      manageChannel: false,
      sendMessage: true,
      voiceChat: true,
    );
    await open(tester);

    expect(find.text('INVITE'), findsOneWidget);
    // Owner and self rows are never actionable, so exactly one KICK (Bob's).
    expect(find.text('KICK'), findsOneWidget);
    await tester.tap(find.text('KICK'));
    await tester.pump();
    expect(find.text('KICK?'), findsOneWidget);

    await tester.tap(find.text('YES'));
    await settle(tester);

    final post = backend.requests
        .singleWhere((r) => r.method == 'POST' && r.path.endsWith('/roles'));
    final body = jsonDecode(post.body) as Map<String, dynamic>;
    expect(body['remove_members'], [_bobHash]);
    expect(body['add_admins'], isEmpty);
  });
}
