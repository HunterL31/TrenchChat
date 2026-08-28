// Verifies the three-column shell lays out at the widths the mockup (1b)
// specifies: 60px server rail, 206px channel column, remainder to content --
// and that below the compact breakpoint the shell collapses to a drawer.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/app_state.dart';
import 'package:flutter_ui/screens/main_window/channel_column.dart';
import 'package:flutter_ui/screens/main_window/main_window.dart';
import 'package:flutter_ui/screens/main_window/server_rail.dart';
import 'package:flutter_ui/widgets/tc_icon.dart';

import '../fake_backend.dart';

void main() {
  testWidgets('server rail is exactly 60px wide', (tester) async {
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: Row(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              ServerRail(
                servers: const [ServerRailEntry(hash: 'a', name: 'Mesh Crew')],
                selectedHash: 'a',
                onSelect: (_) {},
              ),
            ],
          ),
        ),
      ),
    );

    final size = tester.getSize(find.byType(ServerRail));
    expect(size.width, 60);
  });

  testWidgets('channel column is exactly 206px wide', (tester) async {
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: Row(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              ChannelColumn(
                serverName: 'mesh-crew',
                serverMemberCount: 12,
                channels: const [],
                directChannels: const [],
                selectedChannelHash: null,
                onSelectChannel: (_) {},
              ),
            ],
          ),
        ),
      ),
    );

    final size = tester.getSize(find.byType(ChannelColumn));
    expect(size.width, 206);
  });

  testWidgets('rail + column + content fill the window left to right', (tester) async {
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: Row(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              ServerRail(
                servers: const [ServerRailEntry(hash: 'a', name: 'Mesh Crew')],
                selectedHash: 'a',
                onSelect: (_) {},
              ),
              ChannelColumn(
                serverName: 'mesh-crew',
                serverMemberCount: 12,
                channels: const [],
                directChannels: const [],
                selectedChannelHash: null,
                onSelectChannel: (_) {},
              ),
              const Expanded(child: SizedBox()),
            ],
          ),
        ),
      ),
    );

    final railRight = tester.getTopRight(find.byType(ServerRail)).dx;
    final columnLeft = tester.getTopLeft(find.byType(ChannelColumn)).dx;
    final columnRight = tester.getTopRight(find.byType(ChannelColumn)).dx;

    expect(railRight, 60);
    expect(columnLeft, 60);
    expect(columnRight, 60 + 206);
  });

  testWidgets('below the compact breakpoint the shell collapses to a drawer',
      (tester) async {
    tester.view.physicalSize = const Size(390, 844);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    final backend = FakeBackend();
    final state = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
    addTearDown(state.dispose);
    state.loading = false;

    await tester.pumpWidget(MaterialApp(
      home: Scaffold(body: MainWindow(state: state)),
    ));
    await tester.pump();

    // Rail and channel column live in the drawer, which isn't even mounted
    // while closed.
    expect(find.byType(ServerRail, skipOffstage: true), findsNothing);

    // The header's menu button opens it.
    final menuButton = find.byWidgetPredicate(
        (w) => w is TcIcon && w.icon == TcIcons.menu);
    expect(menuButton, findsOneWidget);
    await tester.tap(menuButton);
    await tester.pumpAndSettle();
    expect(find.byType(ServerRail, skipOffstage: true), findsOneWidget);
    expect(find.byType(ChannelColumn, skipOffstage: true), findsOneWidget);
  });

  testWidgets('every tab lays out on a phone viewport without overflowing', (tester) async {
    tester.view.physicalSize = const Size(390, 844);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    final backend = FakeBackend();
    final state = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
    addTearDown(state.dispose);
    state.loading = false;

    await tester.pumpWidget(MaterialApp(
      home: Scaffold(body: MainWindow(state: state)),
    ));
    await tester.pump();

    for (final icon in [TcIcons.map, TcIcons.iface, TcIcons.users, TcIcons.hash]) {
      // The members button shares the users icon, so take the tab strip's own
      // copy -- it is the last one in the header.
      await tester.tap(find.byWidgetPredicate((w) => w is TcIcon && w.icon == icon).last);
      await tester.pump(const Duration(milliseconds: 100));
      expect(tester.takeException(), isNull, reason: 'tab $icon overflowed at 390px');
    }
  });
}
