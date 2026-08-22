// A listed channel's permissions are preloaded without opening it, so its row
// context menu offers the perm-gated actions (Invite / Edit permissions) on
// the first right-click rather than only after the channel has been visited.
import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/models/server.dart';
import 'package:flutter_ui/app_state.dart';
import 'package:flutter_ui/screens/main_window/main_window.dart';

import '../fake_backend.dart';

Future<void> _rightClick(WidgetTester tester, Finder finder) async {
  final gesture = await tester.startGesture(
    tester.getCenter(finder),
    kind: PointerDeviceKind.mouse,
    buttons: kSecondaryButton,
  );
  await gesture.up();
  await tester.pump();
}

void main() {
  testWidgets('an unvisited channel row exposes perm-gated menu items', (tester) async {
    tester.view.physicalSize = const Size(1280, 800);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    final backend = FakeBackend();
    backend.routes['GET /channels/hash-lounge/my_permissions'] = {
      'invite': true,
      'manage_channel': true,
    };
    backend.routes['GET /channels/hash-lounge/sync_status'] = {'state': 'synced'};

    final state = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
    addTearDown(state.dispose);
    state.loading = false;
    state.standaloneChannels = [
      Channel.fromJson({
        'hash': 'hash-lounge',
        'name': 'lounge',
        'description': '',
        'creator_hash': 'creator',
        'open_join': true,
        'created_at': 0,
        'server_hash': null,
      }),
    ];

    await tester.pumpWidget(MaterialApp(
      home: Scaffold(body: MainWindow(state: state)),
    ));
    await tester.pumpAndSettle();

    // Preload ran without the channel ever being opened.
    expect(state.permissionsByChannel.containsKey('hash-lounge'), isTrue);

    await _rightClick(tester, find.text('lounge'));

    expect(find.text('Invite…'), findsOneWidget);
    expect(find.text('Edit permissions…'), findsOneWidget);
  });
}
