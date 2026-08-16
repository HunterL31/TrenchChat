import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/app_state.dart';
import 'package:flutter_ui/screens/main_window/iface_tab.dart';
import 'package:flutter_ui/widgets/tc_button.dart';
import 'package:flutter_ui/widgets/tc_checkbox.dart';

import '../fake_backend.dart';

Widget _harness(AppState state) =>
    MaterialApp(home: Scaffold(body: IfaceTab(state: state)));

void main() {
  late FakeBackend backend;
  late AppState state;

  setUp(() {
    backend = FakeBackend();
    backend.routes['GET /reticulum/interfaces'] = [
      {
        'name': 'TrenchChat Hub',
        'type': 'TCPClientInterface',
        'enabled': true,
        'editable': true,
        'config': {
          'type': 'TCPClientInterface',
          'target_host': 'rmap.world',
          'target_port': '4242',
          'kiss_framing': 'Yes',
          'networkname': 'coast-mesh',
        },
        'status': true,
        'rxb': 2048,
        'txb': 512,
      },
      {
        'name': 'Default Interface',
        'type': 'AutoInterface',
        'enabled': true,
        'editable': true,
        'status': false,
        'rxb': 0,
        'txb': 0,
      },
      {
        'name': 'I2P Tunnel',
        'type': 'I2PInterface',
        'enabled': false,
        'editable': false,
        'status': null,
        'rxb': null,
        'txb': null,
      },
    ];
    state = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
  });

  tearDown(() {
    state.dispose();
  });

  test('formatByteCount steps through B, KB, and MB', () {
    expect(formatByteCount(512), '512 B');
    expect(formatByteCount(2048), '2.0 KB');
    expect(formatByteCount(3 * 1024 * 1024), '3.0 MB');
  });

  testWidgets('lists interfaces with status and read-only marker', (tester) async {
    await tester.pumpWidget(_harness(state));
    await settle(tester);

    expect(find.text('TrenchChat Hub'), findsOneWidget);
    expect(find.text('UP'), findsOneWidget);
    expect(find.text('2.0 KB'), findsOneWidget);
    expect(find.text('Default Interface'), findsOneWidget);
    expect(find.text('DOWN'), findsOneWidget);
    expect(find.text('I2P Tunnel'), findsOneWidget);
    expect(find.text('READ-ONLY'), findsOneWidget);
    // Editable rows get EDIT/DEL; the read-only row gets neither.
    expect(find.text('EDIT'), findsNWidgets(2));
    expect(find.text('DEL'), findsNWidgets(2));
  });

  testWidgets('delete asks for inline confirmation before calling the API', (tester) async {
    backend.routes['DELETE /reticulum/interfaces/TrenchChat%20Hub'] = {'ok': true};
    await tester.pumpWidget(_harness(state));
    await settle(tester);

    await tester.tap(find.text('DEL').first);
    await tester.pump();
    expect(find.text('DELETE?'), findsOneWidget);
    expect(backend.requests.where((r) => r.method == 'DELETE'), isEmpty);

    await tester.tap(find.text('YES'));
    await settle(tester);

    expect(backend.requests.where((r) => r.method == 'DELETE'), hasLength(1));
    expect(find.textContaining('RESTART RETICULUM'), findsOneWidget);
  });

  testWidgets('ADD opens the interface dialog with type choices', (tester) async {
    await tester.pumpWidget(_harness(state));
    await settle(tester);

    await tester.tap(find.widgetWithText(TcGhostButton, 'ADD'));
    await tester.pumpAndSettle();

    expect(find.text('Add Interface'), findsOneWidget);
    expect(find.text('AutoInterface'), findsWidgets);
    expect(find.text('TYPE-SPECIFIC SETTINGS'), findsOneWidget);

    // AutoInterface is the default type; its fields are visible.
    expect(find.text('Group ID'), findsOneWidget);

    // Required-field validation: switch to TCPClientInterface, clear nothing
    // (target_host defaults to empty), and try to save.
    await tester.tap(find.text('TCPClientInterface').last);
    await tester.pumpAndSettle();
    await tester.enterText(
        find.widgetWithText(TextField, 'e.g. My TCP Hub'), 'ridge-hub');
    await tester.tap(find.text('SAVE'));
    await tester.pump();
    expect(find.text("'Target host' is required."), findsOneWidget);
  });

  testWidgets('EDIT pre-fills the dialog from the interface config', (tester) async {
    await tester.pumpWidget(_harness(state));
    await settle(tester);

    await tester.tap(find.text('EDIT').first);
    await tester.pumpAndSettle();

    expect(find.text('Edit Interface'), findsOneWidget);
    expect(find.widgetWithText(TextField, 'rmap.world'), findsOneWidget);
    expect(find.widgetWithText(TextField, '4242'), findsOneWidget);

    // Lower fields build lazily below the dialog's scroll fold; drag the
    // dialog's own list (the last ListView) to reach them.
    final dialogList = find.byType(ListView).last;
    Future<void> scrollDown() async {
      await tester.drag(dialogList, const Offset(0, -250));
      await tester.pumpAndSettle();
    }

    await scrollDown();
    final kiss = tester.widget<TcCheckbox>(
        find.widgetWithText(TcCheckbox, 'KISS framing'));
    expect(kiss.value, isTrue);

    // A key absent from the config falls back to the type default.
    await scrollDown();
    expect(find.widgetWithText(TextField, '5'), findsOneWidget);

    await scrollDown();
    await scrollDown();
    expect(find.widgetWithText(TextField, 'coast-mesh'), findsOneWidget);
  });
}
