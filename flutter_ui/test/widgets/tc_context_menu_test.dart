// A long-press near a phone's right or bottom edge is the common case on
// touch, so the menu has to be pulled back on-screen rather than rendered
// past the viewport where it can't be read or tapped.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/widgets/tc_context_menu.dart';

Widget _harness(void Function(BuildContext context) show) => MaterialApp(
      home: Scaffold(
        body: Builder(
          builder: (context) => Center(
            child: ElevatedButton(
              onPressed: () => show(context),
              child: const Text('open'),
            ),
          ),
        ),
      ),
    );

void main() {
  testWidgets('a menu anchored past the bottom-right corner stays inside the screen',
      (tester) async {
    tester.view.physicalSize = const Size(390, 844);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    await tester.pumpWidget(_harness((context) => showTcContextMenu(
          context: context,
          position: const Offset(380, 838),
          items: [TcContextMenuItem(label: 'Add friend…', onTap: () {})],
        )));
    await tester.tap(find.text('open'));
    await tester.pump();

    final menu = tester.getRect(find.text('Add friend…'));
    expect(menu.right, lessThanOrEqualTo(390));
    expect(menu.bottom, lessThanOrEqualTo(844));
  });

  testWidgets('a menu that fits is anchored at the press point', (tester) async {
    await tester.pumpWidget(_harness((context) => showTcContextMenu(
          context: context,
          position: const Offset(100, 120),
          items: [TcContextMenuItem(label: 'Add friend…', onTap: () {})],
        )));
    await tester.tap(find.text('open'));
    await tester.pump();

    // IntrinsicWidth is the panel's own box -- nothing else in this harness
    // uses one, so its top-left is the menu's anchored position.
    expect(tester.getTopLeft(find.byType(IntrinsicWidth)), const Offset(100, 120));
  });

  testWidgets('TcContextMenuRegion with no items opens nothing on long-press', (tester) async {
    await tester.pumpWidget(const MaterialApp(
      home: Scaffold(
        body: TcContextMenuRegion(items: [], child: Text('row')),
      ),
    ));

    await tester.longPress(find.text('row'));
    await tester.pump();

    expect(find.byType(IntrinsicWidth), findsNothing);
  });
}
