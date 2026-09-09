import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/models/server.dart';
import 'package:flutter_ui/app_state.dart';
import 'package:flutter_ui/screens/dialogs/permissions_dialog.dart';
import 'package:flutter_ui/widgets/tc_checkbox.dart';

import '../../fake_backend.dart';

const _channelHash = 'channel-ops';

Widget _harness(AppState state) {
  return MaterialApp(
    home: Scaffold(
      body: Builder(
        builder: (context) => ElevatedButton(
          onPressed: () => showPermissionsDialog(
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
    backend.routes['GET /channels/$_channelHash/permissions'] = {
      'all_permissions': ['send_message', 'invite', 'kick'],
      'admin': ['send_message', 'invite', 'kick'],
      'member': ['send_message'],
    };
    backend.routes['POST /channels/$_channelHash/permissions'] = {'ok': true};
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

  testWidgets('renders the role matrix with a read-only owner section', (tester) async {
    await open(tester);

    expect(find.text('Permissions — #ops'), findsOneWidget);
    expect(find.text('OWNER'), findsOneWidget);
    expect(find.text('ADMIN'), findsOneWidget);
    expect(find.text('MEMBER'), findsOneWidget);
    // One row per permission per role section.
    expect(find.text('Send messages'), findsNWidgets(3));

    // Owner checkboxes are disabled; the member section reflects the fetch.
    final ownerBox = tester.widget<TcCheckbox>(find.byType(TcCheckbox).first);
    expect(ownerBox.onChanged, isNull);
    expect(ownerBox.value, isTrue);
  });

  testWidgets('toggling and saving posts the updated matrix', (tester) async {
    await open(tester);

    // Grant 'Invite members' to member (the last of the three Invite rows).
    await tester.tap(find.text('Invite members').last);
    await tester.pump();
    await tester.tap(find.text('SAVE'));
    await settle(tester);
    await tester.pumpAndSettle();

    expect(find.text('Permissions — #ops'), findsNothing);
    final post = backend.requests
        .singleWhere((r) => r.method == 'POST' && r.path.endsWith('/permissions'));
    final body = jsonDecode(post.body) as Map<String, dynamic>;
    expect((body['member'] as List).toSet(), {'send_message', 'invite'});
    expect((body['admin'] as List).toSet(), {'send_message', 'invite', 'kick'});
  });

  Channel channel(String hash, {String? serverHash}) => Channel(
        hash: hash,
        name: 'ops',
        description: '',
        creatorHash: 'creator',
        openJoin: false,
        createdAt: 0,
        serverHash: serverHash,
      );

  testWidgets('standalone channel omits the create-channels row', (tester) async {
    backend.routes['GET /channels/$_channelHash/permissions'] = {
      'all_permissions': ['send_message', 'create_channel'],
      'admin': ['send_message', 'create_channel'],
      'member': ['send_message'],
    };
    state.standaloneChannels = [channel(_channelHash)];
    await open(tester);

    expect(find.text('Create channels in this server'), findsNothing);
  });

  testWidgets('server channel keeps the create-channels row', (tester) async {
    backend.routes['GET /channels/$_channelHash/permissions'] = {
      'all_permissions': ['send_message', 'create_channel'],
      'admin': ['send_message', 'create_channel'],
      'member': ['send_message'],
    };
    state.channelsByServer['server-1'] =
        [channel(_channelHash, serverHash: 'server-1')];
    await open(tester);

    expect(find.text('Create channels in this server'), findsWidgets);
  });

  testWidgets('share_files is offered under a readable label', (tester) async {
    backend.routes['GET /channels/$_channelHash/permissions'] = {
      'all_permissions': ['send_message', 'share_files'],
      'admin': ['send_message', 'share_files'],
      'member': ['send_message', 'share_files'],
    };
    await open(tester);

    // Owner, admin and member sections each carry the row.
    expect(find.text('Share files'), findsNWidgets(3));
    expect(find.text('share_files'), findsNothing);
  });
}
