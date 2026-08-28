// The direct-message surfaces: the conversation list in the channel column,
// the request block in the FRIENDS tab, and the state that drives them.
//
// The gate itself is enforced on the backend, at both ends; what these cover
// is that the client shows a conversation for what it is -- who it is with,
// what is unread, and when it can no longer carry anything.
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/models/dm.dart';
import 'package:flutter_ui/app_state.dart';
import 'package:flutter_ui/screens/main_window/channel_column.dart';
import 'package:flutter_ui/screens/main_window/friends_tab.dart';

import '../fake_backend.dart';

const _peer = 'f3a1c2d4e5b6a798f3a1c2d4e5b6a798';
const _conversation = '392ff5b4caefb1952b1a5140d8f1c013';

Map<String, dynamic> _dmJson({
  int unread = 0,
  bool isFriend = true,
  bool isOnline = false,
  String displayName = 'Bee',
}) =>
    {
      'hash': _conversation,
      'peer_hash': _peer,
      'display_name': displayName,
      'created_at': 1.0,
      'last_message_at': 2.0,
      'unread': unread,
      'is_online': isOnline,
      'is_friend': isFriend,
    };

Widget _column({
  required List<DmConversation> dms,
  ValueChanged<String>? onSelectDm,
  VoidCallback? onStartDm,
}) =>
    MaterialApp(
      home: Scaffold(
        body: ChannelColumn(
          serverName: null,
          serverMemberCount: null,
          channels: const [],
          directChannels: const [],
          selectedChannelHash: null,
          onSelectChannel: (_) {},
          dms: dms,
          onSelectDm: onSelectDm,
          onStartDm: onStartDm,
        ),
      ),
    );

void main() {
  late FakeBackend backend;
  late AppState state;

  setUp(() {
    backend = FakeBackend();
    backend.routes['GET /friends'] = <dynamic>[];
    backend.routes['GET /friends/requests'] = {
      'incoming': <dynamic>[],
      'outgoing': <dynamic>[],
    };
    backend.routes['GET /dms'] = <dynamic>[];
    state = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
  });

  tearDown(() => state.dispose());

  group('conversation list', () {
    testWidgets('a conversation is listed under DIRECT MESSAGES, not DIRECT CHANNELS',
        (tester) async {
      await tester.pumpWidget(_column(
        dms: [DmConversation.fromJson(_dmJson())],
        onStartDm: () {},
      ));

      expect(find.text('DIRECT MESSAGES'), findsOneWidget);
      expect(find.text('Bee'), findsOneWidget);
    });

    testWidgets('tapping a conversation selects it', (tester) async {
      String? selected;
      await tester.pumpWidget(_column(
        dms: [DmConversation.fromJson(_dmJson())],
        onSelectDm: (hash) => selected = hash,
      ));

      await tester.tap(find.text('Bee'));
      expect(selected, _conversation);
    });

    testWidgets('unread messages are counted on the row', (tester) async {
      await tester.pumpWidget(_column(dms: [DmConversation.fromJson(_dmJson(unread: 3))]));
      expect(find.text('3'), findsOneWidget);
    });

    testWidgets('a conversation with a former friend says so instead of a count',
        (tester) async {
      await tester.pumpWidget(_column(
        dms: [DmConversation.fromJson(_dmJson(unread: 3, isFriend: false))],
      ));

      expect(find.text('NOT A FRIEND'), findsOneWidget);
      expect(find.text('3'), findsNothing);
    });

    testWidgets('with no conversations the section is still reachable',
        (tester) async {
      await tester.pumpWidget(_column(dms: const [], onStartDm: () {}));
      expect(find.text('DIRECT MESSAGES'), findsOneWidget);
    });
  });

  group('app state', () {
    testWidgets('opening a conversation selects it and marks it read',
        (tester) async {
      backend.routes['POST /dms/$_peer'] = {'hash': _conversation};
      backend.routes['GET /dms'] = [_dmJson(unread: 2)];
      backend.routes['GET /channels/$_conversation/messages'] = <dynamic>[];
      backend.routes['POST /dms/$_conversation/read'] = {'ok': true};

      await state.openDm(_peer);

      expect(state.selectedDmHash, _conversation);
      expect(state.selectedChannelHash, _conversation);
      expect(
        backend.requests.any((r) => r.path == '/dms/$_conversation/read'),
        isTrue,
      );
    });

    testWidgets('a conversation with a non-friend is refused, not opened',
        (tester) async {
      backend.routes['POST /dms/$_peer'] =
          const FakeError(403, {'error': 'not an accepted friend'});

      expect(await state.openDm(_peer), isNull);
      expect(state.selectedDmHash, isNull);
    });

    testWidgets('sending goes to the peer, not the conversation address',
        (tester) async {
      backend.routes['GET /dms'] = [_dmJson()];
      backend.routes['POST /dms/$_peer/messages'] = {
        'ok': true,
        'hash': _conversation,
        'message_id': 'm1',
      };
      backend.routes['GET /channels/$_conversation/messages'] = <dynamic>[];
      await state.loadDms();
      state.selectedDmHash = _conversation;

      expect(await state.sendDirectMessage('hello'), isTrue);
      final posted = backend.requests
          .where((r) => r.method == 'POST' && r.path == '/dms/$_peer/messages');
      expect(posted, hasLength(1));
      expect((jsonDecode(posted.single.body) as Map)['content'], 'hello');
    });

    testWidgets('selecting a channel clears the conversation selection',
        (tester) async {
      backend.routes['GET /channels/abc/messages'] = <dynamic>[];
      state.selectedDmHash = _conversation;

      await state.selectChannel('abc');

      expect(state.selectedDmHash, isNull);
      expect(state.selectedChannelHash, 'abc');
    });
  });

  group('friend requests', () {
    Widget harness() => MaterialApp(home: Scaffold(body: FriendsTab(state: state)));

    testWidgets('an incoming request offers accept and decline', (tester) async {
      backend.routes['GET /friends/requests'] = {
        'incoming': [
          {
            'identity_hash': _peer,
            'display_name': 'Bee',
            'nickname': '',
            'note': 'met at the ridge',
            'added_at': 1.0,
          }
        ],
        'outgoing': <dynamic>[],
      };
      backend.routes['POST /friends/requests/$_peer/accept'] = {'ok': true};
      await state.loadFriendRequests();

      await tester.pumpWidget(harness());
      expect(find.text('ASKING TO BE ADDED'), findsOneWidget);
      expect(find.text('met at the ridge'), findsOneWidget);

      await tester.tap(find.text('ACCEPT'));
      await tester.pump();

      expect(
        backend.requests.any((r) => r.path == '/friends/requests/$_peer/accept'),
        isTrue,
      );
    });

    testWidgets('a request we sent is shown as waiting, with a way to withdraw it',
        (tester) async {
      backend.routes['GET /friends/requests'] = {
        'incoming': <dynamic>[],
        'outgoing': [
          {
            'identity_hash': _peer,
            'display_name': 'Bee',
            'nickname': '',
            'note': '',
            'added_at': 1.0,
          }
        ],
      };
      backend.routes['DELETE /friends/requests/$_peer'] = {'ok': true};
      await state.loadFriendRequests();

      await tester.pumpWidget(harness());
      expect(find.text('WAITING ON THEM'), findsOneWidget);

      await tester.tap(find.text('CANCEL'));
      await tester.pump();

      expect(
        backend.requests.any(
            (r) => r.method == 'DELETE' && r.path == '/friends/requests/$_peer'),
        isTrue,
      );
    });

    testWidgets('with nothing pending, neither block is shown', (tester) async {
      await state.loadFriendRequests();
      await tester.pumpWidget(harness());

      expect(find.text('ASKING TO BE ADDED'), findsNothing);
      expect(find.text('WAITING ON THEM'), findsNothing);
    });
  });
}
