// A peer with no friend-request concept -- Sideband, MeshChat, anything else
// speaking plain LXMF -- can only ask by messaging. Those words ride the same
// incoming-request queue as a handshake, so the tab has to describe both.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/app_state.dart';
import 'package:flutter_ui/screens/main_window/friends_tab.dart';

import '../fake_backend.dart';

Map<String, Object?> _request(
  String hash, {
  String note = '',
  String? message,
  int messageCount = 0,
  bool fromTrenchchat = false,
}) =>
    {
      'identity_hash': hash,
      'nickname': '',
      'note': note,
      'display_name': 'Stranger',
      'added_at': 1.0,
      'message': message,
      'message_count': messageCount,
      'from_trenchchat': fromTrenchchat,
    };

void main() {
  late FakeBackend backend;
  late AppState state;

  setUp(() {
    backend = FakeBackend();
    backend.routes['GET /friends'] = <Object>[];
    state = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
  });

  tearDown(() => state.dispose());

  Widget harness() => MaterialApp(
        home: Scaffold(body: FriendsTab(state: state)),
      );

  Future<void> open(WidgetTester tester) async {
    await state.loadFriendRequests();
    await tester.pumpWidget(harness());
    await settle(tester);
  }

  testWidgets('a held message is shown with an LXMF marker', (tester) async {
    backend.routes['GET /friends/requests'] = {
      'incoming': [
        _request('aa' * 16, message: 'is this thing on', messageCount: 1),
      ],
      'outgoing': <Object>[],
    };

    await open(tester);

    expect(find.text('SENT YOU A MESSAGE'), findsOneWidget);
    expect(find.text('is this thing on'), findsOneWidget);
    expect(find.text('LXMF'), findsOneWidget);
    expect(find.text('ACCEPT'), findsOneWidget);
    expect(find.text('DECLINE'), findsOneWidget);
  });

  testWidgets('more than one held message says how many are waiting',
      (tester) async {
    backend.routes['GET /friends/requests'] = {
      'incoming': [
        _request('aa' * 16, message: 'newest', messageCount: 3,
            fromTrenchchat: true),
      ],
      'outgoing': <Object>[],
    };

    await open(tester);

    expect(find.text('newest  (+2 more)'), findsOneWidget);
    // A TrenchChat peer is not marked: the marker is there to explain a client
    // that could not have asked any other way.
    expect(find.text('LXMF'), findsNothing);
  });

  testWidgets('a plain friend request is unchanged', (tester) async {
    backend.routes['GET /friends/requests'] = {
      'incoming': [_request('aa' * 16, note: 'met you on the ridge')],
      'outgoing': <Object>[],
    };

    await open(tester);

    expect(find.text('ASKING TO BE ADDED'), findsOneWidget);
    expect(find.text('met you on the ridge'), findsOneWidget);
    expect(find.text('LXMF'), findsNothing);
  });
}
