// A tooltip must never cost the user a press. Material's default trigger arms
// a long-press recognizer for touch, stylus and trackpad pointers, which
// competes with the control underneath -- so every tooltip over something
// clickable goes through TcTooltip, whose trigger is manual.
import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/widgets/tc_button.dart';
import 'package:flutter_ui/widgets/tc_icon.dart';
import 'package:flutter_ui/widgets/tc_tooltip.dart';

void main() {
  testWidgets('TcTooltip leaves the press to the child', (tester) async {
    var taps = 0;
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: Center(
          child: TcTooltip(
            message: 'Members',
            child: GestureDetector(onTap: () => taps++, child: const Text('press me')),
          ),
        ),
      ),
    ));

    await tester.tap(find.text('press me'));
    await tester.pump();

    expect(taps, 1);
  });

  testWidgets('TcTooltip does not show its tip on a long press', (tester) async {
    await tester.pumpWidget(const MaterialApp(
      home: Scaffold(
        body: Center(child: TcTooltip(message: 'Members', child: Text('press me'))),
      ),
    ));

    await tester.longPress(find.text('press me'));
    await tester.pumpAndSettle();

    expect(find.text('Members'), findsNothing);
  });

  testWidgets('a tooltipped icon button fires on the first tap', (tester) async {
    var taps = 0;
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: Center(
          child: TcIconButton(
            icon: TcIcons.users,
            tooltip: 'Members',
            onPressed: () => taps++,
          ),
        ),
      ),
    ));

    await tester.tap(find.byType(TcIconButton));
    await tester.pump();
    expect(taps, 1);
  });

  testWidgets('a tooltipped icon button fires on a slow touch press', (tester) async {
    // The gesture the default trigger takes for itself: held past the
    // long-press threshold, a touch press would show the tip and never reach
    // the button, so the user has to press a second time.
    var taps = 0;
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: Center(
          child: TcIconButton(
            icon: TcIcons.users,
            tooltip: 'Members',
            onPressed: () => taps++,
          ),
        ),
      ),
    ));

    final gesture = await tester.startGesture(
      tester.getCenter(find.byType(TcIconButton)),
      kind: PointerDeviceKind.touch,
    );
    await tester.pump(const Duration(seconds: 1)); // past the long-press threshold
    await gesture.up();
    await tester.pumpAndSettle();

    expect(taps, 1);
  });
}
