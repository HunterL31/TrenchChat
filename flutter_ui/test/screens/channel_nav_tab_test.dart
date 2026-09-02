// Picking a channel or a conversation from the left column brings the chat
// pane back: with MAP (or any other tab) open, the row you clicked must show
// what it selected rather than leaving the other pane over it.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/models/dm.dart';
import 'package:flutter_ui/api/models/server.dart';
import 'package:flutter_ui/app_state.dart';
import 'package:flutter_ui/screens/main_window/channel_column.dart';
import 'package:flutter_ui/screens/main_window/compose_bar.dart';
import 'package:flutter_ui/screens/main_window/main_window.dart';
import 'package:flutter_ui/screens/main_window/map_tab.dart';
import 'package:flutter_ui/screens/main_window/message_list.dart';

import '../fake_backend.dart';

const _peer = 'f3a1c2d4e5b6a798f3a1c2d4e5b6a798';
const _conversation = '392ff5b4caefb1952b1a5140d8f1c013';

Map<String, Object?> _channelJson(String name) => {
      'hash': 'hash-$name',
      'name': name,
      'description': '',
      'creator_hash': 'creator',
      'open_join': true,
      'created_at': 0,
      'server_hash': null,
    };

Map<String, Object?> _dmJson() => {
      'hash': _conversation,
      'peer_hash': _peer,
      'display_name': 'Bee',
      'created_at': 1.0,
      'last_message_at': 2.0,
      'unread': 0,
      'is_online': false,
      'is_friend': true,
    };

/// Everything selecting a channel reads, so the tap adds no failures of its
/// own to the pane under test.
void _seedChannelReads(FakeBackend backend, String hash) {
  backend.routes['GET /channels/$hash/members'] = <Object>[];
  backend.routes['GET /channels/$hash/messages'] = <Object>[];
  backend.routes['GET /channels/$hash/presence'] = <Object>[];
  backend.routes['GET /channels/$hash/link_quality'] = <Object>[];
  backend.routes['GET /channels/$hash/my_permissions'] = {'invite': false};
  backend.routes['GET /channels/$hash/voice/roster'] = <Object>[];
  backend.routes['GET /channels/$hash/sync_status'] = {'state': 'synced'};
}

/// The whole shell on a desktop-width viewport, with two channels listed and
/// one conversation.
Future<AppState> _pumpShell(WidgetTester tester, FakeBackend backend) async {
  tester.view.physicalSize = const Size(1400, 900);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.reset);

  for (final name in ['alpha', 'beta']) {
    _seedChannelReads(backend, 'hash-$name');
  }
  backend.routes['GET /dms'] = [_dmJson()];
  backend.routes['GET /channels/$_conversation/messages'] = <Object>[];
  backend.routes['POST /dms/$_conversation/read'] = {'ok': true};

  final state = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
  addTearDown(state.dispose);
  state.loading = false;
  state.standaloneChannels = [
    Channel.fromJson(_channelJson('alpha')),
    Channel.fromJson(_channelJson('beta')),
  ];
  state.selectedChannelHash = 'hash-alpha';
  state.dms = [DmConversation.fromJson(_dmJson())];

  await tester.pumpWidget(MaterialApp(home: Scaffold(body: MainWindow(state: state))));
  await tester.pumpAndSettle();
  return state;
}

/// The column's own row for [label]: the header names the open channel too.
Finder _columnRow(String label) =>
    find.descendant(of: find.byType(ChannelColumn), matching: find.text(label));

void main() {
  late FakeBackend backend;

  setUp(() {
    backend = FakeBackend();
  });

  testWidgets('picking a channel from the column returns to the chat pane',
      (tester) async {
    final state = await _pumpShell(tester, backend);

    await tester.tap(find.text('MAP'));
    await tester.pumpAndSettle();
    expect(find.byType(MapTab), findsOneWidget);
    expect(find.byType(ComposeBar), findsNothing);

    await tester.tap(_columnRow('beta'));
    await tester.pumpAndSettle();

    expect(find.byType(MapTab), findsNothing);
    expect(find.byType(MessageList), findsOneWidget);
    expect(find.byType(ComposeBar), findsOneWidget);
    expect(state.selectedChannelHash, 'hash-beta');
  });

  testWidgets('re-picking the open channel also returns to the chat pane',
      (tester) async {
    await _pumpShell(tester, backend);

    await tester.tap(find.text('MAP'));
    await tester.pumpAndSettle();
    expect(find.byType(ComposeBar), findsNothing);

    await tester.tap(_columnRow('alpha'));
    await tester.pumpAndSettle();

    expect(find.byType(MapTab), findsNothing);
    expect(find.byType(MessageList), findsOneWidget);
    expect(find.byType(ComposeBar), findsOneWidget);
  });

  testWidgets('picking a conversation from the column returns to the chat pane',
      (tester) async {
    final state = await _pumpShell(tester, backend);

    await tester.tap(find.text('MAP'));
    await tester.pumpAndSettle();
    expect(find.byType(ComposeBar), findsNothing);

    await tester.tap(_columnRow('Bee'));
    await tester.pumpAndSettle();

    expect(find.byType(MapTab), findsNothing);
    expect(find.byType(MessageList), findsOneWidget);
    expect(find.byType(ComposeBar), findsOneWidget);
    expect(state.selectedDmHash, _conversation);
  });
}
