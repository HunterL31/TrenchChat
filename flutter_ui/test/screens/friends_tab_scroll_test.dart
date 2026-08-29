// A queue of message-requests is not bounded by anything the tab controls, so
// the requests and the friends list each have to scroll inside their own share
// of the column rather than pushing the other off the window.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/app_state.dart';
import 'package:flutter_ui/screens/main_window/friends_tab.dart';

import '../fake_backend.dart';

Map<String, Object?> _friendJson(int i) => {
      'identity_hash': i.toString().padLeft(2, '0') * 16,
      'nickname': '',
      'note': '',
      'display_name': 'Friend $i',
      'added_at': 1.0,
      'last_seen_at': 2.0,
      'is_online': false,
      'nomad_node_hash': null,
    };

Map<String, Object?> _requestJson(int i) => {
      'identity_hash': (i + 40).toString().padLeft(2, '0') * 16,
      'nickname': '',
      'note': '',
      'display_name': 'Stranger $i',
      'added_at': 1.0,
      'message': 'hello from $i',
      'message_count': 1,
      'from_trenchchat': false,
    };

void main() {
  late FakeBackend backend;
  late AppState state;

  setUp(() {
    backend = FakeBackend();
    backend.routes['GET /friends'] = [for (var i = 0; i < 6; i++) _friendJson(i)];
    backend.routes['GET /friends/requests'] = {
      'incoming': [for (var i = 0; i < 20; i++) _requestJson(i)],
      'outgoing': <Object>[],
    };
    state = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
  });

  tearDown(() => state.dispose());

  const tabHeight = 500.0;

  Widget harness() => MaterialApp(
        home: Scaffold(
          body: Center(
            child: SizedBox(
              width: 420,
              height: tabHeight,
              child: FriendsTab(state: state),
            ),
          ),
        ),
      );

  testWidgets('a short request queue leaves the rest of the tab to the '
      'friends list', (tester) async {
    backend.routes['GET /friends/requests'] = {
      'incoming': [_requestJson(0)],
      'outgoing': <Object>[],
    };
    await state.loadFriends();
    await state.loadFriendRequests();
    await tester.pumpWidget(harness());
    await settle(tester);

    final friendsList = find
        .ancestor(of: find.text('Friend 0'), matching: find.byType(ListView))
        .last;
    final tabBottom = tester.getRect(find.byType(FriendsTab)).bottom;
    expect(tester.getRect(friendsList).bottom, greaterThan(tabBottom - 20));
  });

  testWidgets('a long request queue scrolls instead of squeezing the friends '
      'list off the tab', (tester) async {
    await state.loadFriends();
    await state.loadFriendRequests();
    await tester.pumpWidget(harness());
    await settle(tester);

    expect(tester.takeException(), isNull);
    expect(find.text('Friend 0'), findsOneWidget);

    final requestBlock = find.ancestor(
      of: find.text('Stranger 0'),
      matching: find.byType(Scrollable),
    );
    expect(requestBlock, findsOneWidget);
    expect(find.text('Stranger 19'), findsNothing);

    await tester.drag(requestBlock, const Offset(0, -2000));
    await settle(tester);
    expect(find.text('Stranger 19'), findsOneWidget);
  });
}
