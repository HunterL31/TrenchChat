import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/app_state.dart';
import 'package:flutter_ui/screens/dialogs/settings_dialog.dart';

import '../../fake_backend.dart';

Widget _harness(AppState state, {TargetPlatform? platform}) {
  return MaterialApp(
    theme: platform == null ? null : ThemeData(platform: platform),
    home: Scaffold(
      body: Builder(
        builder: (context) => ElevatedButton(
          onPressed: () => showSettingsDialog(context, state),
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
    backend.routes['GET /settings'] = {
      'propagation_enabled': true,
      'propagation_node_name': 'my-relay',
      'propagation_storage_limit_mb': 512,
      'channel_filter_mode': 'allowlist',
      'channel_filter_hashes': <String>[],
      'outbound_propagation_node': 'relay.example',
    };
    backend.routes['POST /settings'] = {'ok': true};
    backend.routes['POST /me/display_name'] = {'ok': true};
    state = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
    state.meHashHex = 'a9f13c02e7d84b119876543210fedcba';
    state.meDisplayName = 'operator';
  });

  tearDown(() {
    state.dispose();
  });

  Future<void> open(WidgetTester tester, {TargetPlatform? platform}) async {
    await tester.pumpWidget(_harness(state, platform: platform));
    await tester.tap(find.text('open'));
    await tester.pump();
    await settle(tester);
  }

  testWidgets('loads and renders the identity and propagation sections', (tester) async {
    await open(tester);

    expect(find.text('Settings'), findsOneWidget);
    expect(find.text('IDENTITY'), findsOneWidget);
    expect(find.text('PROPAGATION NODE'), findsOneWidget);
    expect(find.text('operator'), findsOneWidget);
    expect(find.text('my-relay'), findsOneWidget);
    expect(find.text('relay.example'), findsOneWidget);
    expect(find.text('512'), findsOneWidget);
    expect(find.text(state.meHashHex), findsOneWidget);
  });

  testWidgets('rejects an empty display name without saving', (tester) async {
    await open(tester);

    await tester.enterText(find.widgetWithText(TextField, 'operator'), '');
    await tester.tap(find.text('SAVE'));
    await tester.pump();

    expect(find.text('Display name cannot be empty.'), findsOneWidget);
    expect(backend.requests.where((r) => r.method == 'POST'), isEmpty);
  });

  testWidgets('save posts the edited settings and closes', (tester) async {
    await open(tester);

    await tester.enterText(find.widgetWithText(TextField, 'my-relay'), 'ridge-node');
    await tester.tap(find.text('SAVE'));
    await settle(tester);
    await tester.pumpAndSettle();

    expect(find.text('Settings'), findsNothing);
    final post = backend.requests.singleWhere((r) => r.path == '/settings' && r.method == 'POST');
    final body = jsonDecode(post.body) as Map<String, dynamic>;
    expect(body['propagation_node_name'], 'ridge-node');
    expect(body['propagation_enabled'], true);
    expect(body['propagation_storage_limit_mb'], 512);
  });

  testWidgets('the scrollable content keeps clear of the desktop scrollbar',
      (tester) async {
    // The scroll behavior draws the scrollbar inside the viewport, over
    // whatever is at its right edge -- the SECURITY notes are the longest
    // lines and ran under it.
    await open(tester, platform: TargetPlatform.linux);

    final list = tester.widget<ListView>(find.byType(ListView));
    expect((list.padding! as EdgeInsets).right, greaterThan(0));
  });

  testWidgets('platforms that draw no scrollbar reserve no room', (tester) async {
    await open(tester, platform: TargetPlatform.android);

    final list = tester.widget<ListView>(find.byType(ListView));
    expect((list.padding! as EdgeInsets).right, 0);
  });

  testWidgets('Enter in a settings field saves', (tester) async {
    await open(tester);

    await tester.enterText(find.widgetWithText(TextField, 'my-relay'), 'ridge-node');
    await tester.testTextInput.receiveAction(TextInputAction.done);
    await settle(tester);
    await tester.pumpAndSettle();

    expect(find.text('Settings'), findsNothing);
    expect(backend.requests.any((r) => r.path == '/settings' && r.method == 'POST'), isTrue);
  });
}
