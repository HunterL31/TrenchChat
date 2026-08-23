import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/app_state.dart';
import 'package:flutter_ui/screens/main_window/friends_tab.dart';
import 'package:flutter_ui/widgets/tc_button.dart';

import '../fake_backend.dart';

const _node = 'ffeeffeeffeeffeeffeeffeeffeeffee';

Map<String, Object?> _friendJson(String hash, {String? nodeHash}) => {
      'identity_hash': hash,
      'nickname': '',
      'note': '',
      'display_name': hash == 'aa' * 16 ? 'Hosting Pal' : 'Quiet Pal',
      'added_at': 1.0,
      'last_seen_at': 2.0,
      'is_online': true,
      'nomad_node_hash': nodeHash,
    };

void main() {
  late FakeBackend backend;
  late AppState state;

  setUp(() {
    backend = FakeBackend();
    backend.routes['GET /friends'] = [
      _friendJson('aa' * 16, nodeHash: _node),
      _friendJson('bb' * 16),
    ];
    state = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
  });

  tearDown(() => state.dispose());

  Widget harness({void Function(String)? onOpenNomadPage}) => MaterialApp(
        home: Scaffold(
          body: FriendsTab(state: state, onOpenNomadPage: onOpenNomadPage),
        ),
      );

  testWidgets('only the hosting friend gets a page button, and it opens '
      'their index', (tester) async {
    final opened = <String>[];
    await state.loadFriends();
    await tester.pumpWidget(harness(onOpenNomadPage: opened.add));
    await settle(tester);

    expect(find.text('Hosting Pal'), findsOneWidget);
    expect(find.text('Quiet Pal'), findsOneWidget);
    final buttons = find.byWidgetPredicate(
        (w) => w is TcIconButton && w.tooltip == 'Open their page');
    expect(buttons, findsOneWidget);

    await tester.tap(buttons);
    await settle(tester);
    expect(opened, ['$_node:/page/index.mu']);
  });

  testWidgets('no callback means no page buttons', (tester) async {
    await state.loadFriends();
    await tester.pumpWidget(harness());
    await settle(tester);
    expect(
      find.byWidgetPredicate(
          (w) => w is TcIconButton && w.tooltip == 'Open their page'),
      findsNothing,
    );
  });
}
