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
    backend.routes['GET /bandwidth'] = {
      'sampled_at': 1000.0,
      'totals': {'rx': 3 * 1024 * 1024, 'tx': 2048},
      'windows': [
        {
          'secs': 10,
          'span_secs': 10.0,
          'rx_bytes': 20480,
          'tx_bytes': 5120,
          'rx_per_sec': 2048.0,
          'tx_per_sec': 512.0,
        },
        {
          'secs': 60,
          'span_secs': 60.0,
          'rx_bytes': 61440,
          'tx_bytes': 6144,
          'rx_per_sec': 1024.0,
          'tx_per_sec': 102.4,
        },
        {
          'secs': 300,
          'span_secs': 4.0,
          'rx_bytes': 0,
          'tx_bytes': 0,
          'rx_per_sec': null,
          'tx_per_sec': null,
        },
      ],
    };
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

  test('formatRate steps through B/s, KB/s, and MB/s and dashes null', () {
    expect(formatRate(null), '—');
    expect(formatRate(512), '512 B/s');
    expect(formatRate(2048), '2.0 KB/s');
    expect(formatRate(3 * 1024 * 1024), '3.0 MB/s');
  });

  test('windowLabel renders seconds, minutes, and hours', () {
    expect(windowLabel(10), '10S');
    expect(windowLabel(60), '1M');
    expect(windowLabel(300), '5M');
    expect(windowLabel(3600), '1H');
  });

  testWidgets('shows windowed bandwidth rates and session totals', (tester) async {
    await tester.pumpWidget(_harness(state));
    await settle(tester);

    expect(find.text('BANDWIDTH 10S'), findsOneWidget);
    expect(find.text('RX 2.0 KB/s'), findsOneWidget);
    expect(find.text('TX 512 B/s'), findsOneWidget);
    expect(find.text('BANDWIDTH 1M'), findsOneWidget);
    expect(find.text('RX 1.0 KB/s'), findsOneWidget);
    // A window not yet spanned by samples dashes out instead of claiming 0.
    expect(find.text('BANDWIDTH 5M'), findsOneWidget);
    expect(find.text('RX —'), findsOneWidget);
    expect(find.text('SESSION TOTAL'), findsOneWidget);
    expect(find.text('RX 3.0 MB'), findsOneWidget);
  });

  testWidgets('a failing bandwidth endpoint hides the strip, not the tab',
      (tester) async {
    backend.routes.remove('GET /bandwidth');
    await tester.pumpWidget(_harness(state));
    await settle(tester);

    expect(find.text('SESSION TOTAL'), findsNothing);
    expect(find.text('TrenchChat Hub'), findsOneWidget);
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

  testWidgets('the table keeps its minimum width and pans sideways on a phone viewport',
      (tester) async {
    tester.view.physicalSize = const Size(390, 844);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    await tester.pumpWidget(_harness(state));
    await settle(tester);

    // Squeezed to 390 the seven columns would ellipsize away; the row keeps
    // its full width behind a horizontal scroll instead.
    expect(tester.getSize(find.text('NAME').hitTestable()).width, greaterThan(0));
    final scroller = find.byWidgetPredicate(
      (w) => w is SingleChildScrollView && w.scrollDirection == Axis.horizontal,
    );
    expect(scroller, findsOneWidget);
    expect(tester.getSize(find.byType(SizedBox).at(0)).width, isNot(390));
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
