// Server lifecycle from the rail (#47/#48): a right-click menu that offers
// Leave and, when permitted, Invite/Edit permissions; the >_ logo as a home
// affordance; and a full-name tooltip on each 2-letter tile.
import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/screens/main_window/server_rail.dart';
import 'package:flutter_ui/widgets/tc_tooltip.dart';

Future<void> _rightClick(WidgetTester tester, Finder finder) async {
  final gesture = await tester.startGesture(
    tester.getCenter(finder),
    kind: PointerDeviceKind.mouse,
    buttons: kSecondaryButton,
  );
  await gesture.up();
  await tester.pump();
}

Widget _harness({
  String? selectedHash = 'srv-a',
  bool canInvite = false,
  bool canManage = false,
  ValueChanged<String>? onLeaveServer,
  ValueChanged<String>? onInviteServer,
  ValueChanged<String>? onEditServerPermissions,
  VoidCallback? onHome,
}) =>
    MaterialApp(
      home: Scaffold(
        body: ServerRail(
          servers: [
            ServerRailEntry(
              hash: 'srv-a',
              name: 'mesh-crew',
              canInvite: canInvite,
              canManage: canManage,
            ),
          ],
          selectedHash: selectedHash,
          onSelect: (_) {},
          onHome: onHome,
          onLeaveServer: onLeaveServer,
          onInviteServer: onInviteServer,
          onEditServerPermissions: onEditServerPermissions,
        ),
      ),
    );

void main() {
  testWidgets('the tile carries a tooltip with the full server name', (tester) async {
    await tester.pumpWidget(_harness());
    // The tile shows initials but the tooltip holds the full name.
    expect(find.text('MC'), findsOneWidget);
    final tooltips = tester
        .widgetList<TcTooltip>(find.byType(TcTooltip))
        .map((t) => t.message)
        .toList();
    expect(tooltips, contains('mesh-crew'));
  });

  testWidgets('right-click offers only Leave when the reader lacks permissions',
      (tester) async {
    await tester.pumpWidget(_harness(onLeaveServer: (_) {}));
    await _rightClick(tester, find.text('MC'));

    expect(find.text('Leave server'), findsOneWidget);
    expect(find.text('Invite…'), findsNothing);
    expect(find.text('Edit permissions…'), findsNothing);
  });

  testWidgets('right-click offers Invite and Edit permissions when permitted',
      (tester) async {
    await tester.pumpWidget(_harness(
      canInvite: true,
      canManage: true,
      onLeaveServer: (_) {},
      onInviteServer: (_) {},
      onEditServerPermissions: (_) {},
    ));
    await _rightClick(tester, find.text('MC'));

    expect(find.text('Invite…'), findsOneWidget);
    expect(find.text('Edit permissions…'), findsOneWidget);
    expect(find.text('Leave server'), findsOneWidget);
  });

  testWidgets('Leave server fires the callback with the server hash', (tester) async {
    String? left;
    await tester.pumpWidget(_harness(onLeaveServer: (hash) => left = hash));
    await _rightClick(tester, find.text('MC'));
    await tester.tap(find.text('Leave server'));
    await tester.pump();

    expect(left, 'srv-a');
  });

  testWidgets('tapping the >_ logo fires the home affordance', (tester) async {
    var home = 0;
    await tester.pumpWidget(_harness(onHome: () => home++));
    await tester.tap(find.text('>_'));
    await tester.pump();

    expect(home, 1);
  });
}
