import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/models/app_version.dart';
import 'package:flutter_ui/api/models/dm.dart';
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
    expect(find.text('512'), findsOneWidget);
    expect(find.text(state.meHashHex), findsOneWidget);
  });

  // The dialog nests more than one scrollable, so the settings list has to be
  // named explicitly.
  Future<void> scrollTo(WidgetTester tester, Finder target) =>
      tester.scrollUntilVisible(
        target,
        100,
        scrollable: find
            .descendant(of: find.byType(ListView), matching: find.byType(Scrollable))
            .first,
      );

  testWidgets('the about section names the running build', (tester) async {
    state.appVersion = const AppVersionInfo(
        version: '1.4.0',
        transition: VersionTransition.downgrade,
        previous: '1.5.0');

    await open(tester);
    await scrollTo(tester, find.text('1.4.0'));

    expect(find.text('ABOUT'), findsOneWidget);
    expect(find.text('1.4.0'), findsOneWidget);
    // The build it replaced is recorded, but never shown.
    expect(find.textContaining('1.5.0'), findsNothing);
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
    // The node offline direct messages go through is chosen through
    // /propagation/node, which sets the live router too -- so it is never
    // part of a settings save.
    expect(body.containsKey('outbound_propagation_node'), isFalse);
  });

  group('voice devices', () {
    void stubDevices() {
      backend.routes['GET /voice/devices'] = {
        'available': true,
        'reason': '',
        'input': ['Built-in Mic', 'USB Headset'],
        'output': ['Built-in Speakers', 'USB Headset'],
        'selected': {'input': null, 'output': null},
      };
      backend.routes['POST /voice/devices'] = {
        'ok': true,
        'devices': backend.routes['GET /voice/devices']!,
      };
    }

    testWidgets('defaults to the system default for both directions',
        (tester) async {
      stubDevices();
      await open(tester);
      await scrollTo(tester, find.text('SPEAKERS'));

      expect(find.text('MICROPHONE'), findsOneWidget);
      expect(find.text('System default'), findsNWidgets(2));
    });

    testWidgets('without an audio stack the section says so, with no pickers',
        (tester) async {
      backend.routes['GET /voice/devices'] = {
        'available': false,
        'reason': 'sounddevice unavailable',
        'input': <String>[],
        'output': <String>[],
        'selected': {'input': null, 'output': null},
      };
      await open(tester);
      await scrollTo(tester, find.textContaining('not available'));

      expect(find.textContaining('sounddevice unavailable'), findsOneWidget);
      expect(find.text('MICROPHONE'), findsNothing);
    });

    testWidgets('picking a device and saving posts the choice', (tester) async {
      stubDevices();
      await open(tester);
      await scrollTo(tester, find.text('MICROPHONE'));
      await tester.ensureVisible(find.text('System default').first);
      await tester.pumpAndSettle();

      await tester.tap(find.text('System default').first);
      await tester.pumpAndSettle();
      await tester.tap(find.text('USB Headset').first);
      await tester.pumpAndSettle();

      await tester.tap(find.text('SAVE'));
      await settle(tester);

      final post = backend.requests
          .singleWhere((r) => r.path == '/voice/devices' && r.method == 'POST');
      final body = jsonDecode(post.body) as Map<String, dynamic>;
      expect(body['input_device'], 'USB Headset');
      expect(body['output_device'], isNull);
    });

    testWidgets('an unchanged selection posts nothing', (tester) async {
      stubDevices();
      await open(tester);

      await tester.tap(find.text('SAVE'));
      await settle(tester);

      expect(
        backend.requests.where(
            (r) => r.path == '/voice/devices' && r.method == 'POST'),
        isEmpty,
      );
    });
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

  testWidgets('with a session PIN set, no copy claims at-rest protection',
      (tester) async {
    await open(tester);

    Finder pinField(String hint) => find.byWidgetPredicate(
        (w) => w is TextField && w.decoration?.hintText == hint);

    await tester.dragUntilVisible(
      find.text('SET PIN…'),
      find.byType(ListView),
      const Offset(0, -80),
    );
    // Before a PIN is set the copy is already honest (stored unencrypted).
    expect(find.textContaining('protected by a PIN'), findsNothing);

    await tester.tap(find.text('SET PIN…'));
    await settle(tester);
    await tester.enterText(pinField('New PIN'), '1234');
    await tester.enterText(pinField('Confirm PIN'), '1234');
    await tester.tap(find.text('SET PIN'));
    await settle(tester);

    // Now that a PIN is set, the headline must not claim at-rest protection.
    expect(find.textContaining('protected by a PIN'), findsNothing);
    expect(find.textContaining('does not encrypt your identity or message database'),
        findsOneWidget);
    // No visible text asserts the database is encrypted/protected at rest.
    final claimsAtRest = find.byWidgetPredicate((w) =>
        w is Text &&
        w.data != null &&
        (w.data!.contains('are protected by a PIN') ||
            RegExp(r'\bis encrypted\b').hasMatch(w.data!)));
    expect(claimsAtRest, findsNothing);
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

  // Up to MAX_TRACKED_NODES are held at once and ordered nearest first, so
  // the pane lists only the nearest few and moves the rest behind a button.
  group('the propagation node list', () {
    PropagationStatus heard(int count) => PropagationStatus(
          selected: 'node-0' * 5,
          pinned: '',
          nodes: [
            for (var i = 0; i < count; i++)
              PropagationNode(
                hash: 'node${i.toString().padLeft(2, '0')}' * 4,
                hops: i + 1,
                lastHeard: 0,
                selected: i == 0,
              ),
          ],
          syncState: 0,
        );

    testWidgets('lists at most five nodes inline', (tester) async {
      state.propagation = heard(9);

      await open(tester);
      await scrollTo(tester, find.text('ALL 9 NODES'));

      expect(find.text('USE'), findsNWidgets(5));
      expect(find.text('ALL 9 NODES'), findsOneWidget);
    });

    testWidgets('shows every node with no button when five or fewer',
        (tester) async {
      state.propagation = heard(4);

      await open(tester);
      await scrollTo(tester, find.text('COLLECT NOW'));

      expect(find.text('USE'), findsNWidgets(4));
      expect(find.textContaining('NODES'), findsNothing);
    });

    testWidgets('the button opens the full list', (tester) async {
      state.propagation = heard(9);
      await open(tester);
      await scrollTo(tester, find.text('ALL 9 NODES'));
      await tester.ensureVisible(find.text('ALL 9 NODES'));
      await tester.pumpAndSettle();
      // The ninth node is past the inline cap, so it is only reachable here.
      expect(find.textContaining('node08…node08'), findsNothing);

      await tester.tap(find.text('ALL 9 NODES'));
      await settle(tester);

      expect(find.text('Propagation nodes'), findsOneWidget);
      expect(find.textContaining('9 nodes heard'), findsOneWidget);
      expect(find.textContaining('node08…node08'), findsOneWidget);
    });
  });
}
