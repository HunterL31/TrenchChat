// The picker's pending color is only ever read back through its hex field --
// that is the one readout the user and a test share.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/theme/theme_spec.dart';
import 'package:flutter_ui/widgets/tc_color_picker.dart';

Widget _harness(Color initial, void Function(Color?) onResult) {
  return MaterialApp(
    home: Scaffold(
      body: Builder(
        builder: (context) => ElevatedButton(
          onPressed: () async {
            onResult(await showTcColorPicker(context, initial: initial, title: 'Accent'));
          },
          child: const Text('open'),
        ),
      ),
    ),
  );
}

void main() {
  Color? result;
  bool resolved = false;

  setUp(() {
    result = null;
    resolved = false;
  });

  Future<void> openPicker(WidgetTester tester, Color initial) async {
    tester.view.physicalSize = const Size(800, 1200);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    await tester.pumpWidget(_harness(initial, (c) {
      result = c;
      resolved = true;
    }));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();
  }

  String hexText(WidgetTester tester) =>
      tester.widget<TextField>(find.byKey(tcPickerHexKey)).controller!.text;

  Color pending(WidgetTester tester) => parseThemeColor(hexText(tester))!;

  testWidgets('opens on the initial color and USE returns it unchanged', (tester) async {
    await openPicker(tester, const Color(0xFF102030));

    expect(find.text('Accent'), findsOneWidget);
    expect(hexText(tester), '#102030');

    await tester.tap(find.byKey(tcPickerUseKey));
    await tester.pumpAndSettle();

    expect(resolved, isTrue);
    expect(result, const Color(0xFF102030));
  });

  testWidgets('CANCEL resolves to null', (tester) async {
    await openPicker(tester, const Color(0xFF102030));

    await tester.drag(find.byKey(tcPickerHueKey), const Offset(60, 0));
    await tester.pump();
    await tester.tap(find.byKey(tcPickerCancelKey));
    await tester.pumpAndSettle();

    expect(resolved, isTrue);
    expect(result, isNull);
  });

  testWidgets('a typed hex becomes the pending color', (tester) async {
    await openPicker(tester, const Color(0xFF102030));

    await tester.enterText(find.byKey(tcPickerHexKey), '#00ff88');
    await tester.pump();
    await tester.tap(find.byKey(tcPickerUseKey));
    await tester.pumpAndSettle();

    expect(result, const Color(0xFF00FF88));
  });

  testWidgets('an unparseable hex commits nothing', (tester) async {
    await openPicker(tester, const Color(0xFF102030));

    await tester.enterText(find.byKey(tcPickerHexKey), 'not-a-color');
    await tester.pump();
    await tester.tap(find.byKey(tcPickerUseKey));
    await tester.pumpAndSettle();

    expect(result, const Color(0xFF102030));
  });

  testWidgets('dragging the pad changes saturation and value', (tester) async {
    await openPicker(tester, const Color(0xFFFF0000));
    final before = pending(tester);

    await tester.drag(find.byKey(tcPickerPadKey), const Offset(-80, 40));
    await tester.pump();

    final after = pending(tester);
    expect(after, isNot(before));
    expect(HSVColor.fromColor(after).saturation,
        lessThan(HSVColor.fromColor(before).saturation));
    expect(HSVColor.fromColor(after).value, lessThan(HSVColor.fromColor(before).value));
  });

  testWidgets('dragging the hue strip changes hue and keeps alpha', (tester) async {
    await openPicker(tester, const Color(0x80FF0000));
    expect(hexText(tester), '#80ff0000');

    await tester.drag(find.byKey(tcPickerHueKey), const Offset(-60, 0));
    await tester.pump();

    final after = pending(tester);
    expect(HSVColor.fromColor(after).hue, isNot(closeTo(0, 1)));
    expect(after.a, closeTo(0x80 / 0xFF, 0.01));
    expect(hexText(tester).length, 9);
  });

  testWidgets('the alpha strip writes #aarrggbb', (tester) async {
    await openPicker(tester, const Color(0xFF00FF88));
    expect(hexText(tester), '#00ff88');

    await tester.drag(find.byKey(tcPickerAlphaKey), const Offset(-60, 0));
    await tester.pump();

    final hex = hexText(tester);
    expect(hex.length, 9, reason: hex);
    expect(hex.endsWith('00ff88'), isTrue, reason: hex);
    expect(pending(tester).a, lessThan(1.0));

    await tester.tap(find.byKey(tcPickerUseKey));
    await tester.pumpAndSettle();
    expect(result!.a, lessThan(1.0));
  });

  testWidgets('a grey keeps its hue while its value is dragged', (tester) async {
    await openPicker(tester, const Color(0xFF808080));
    final pad = tester.getRect(find.byKey(tcPickerPadKey));
    final hue = tester.getRect(find.byKey(tcPickerHueKey));

    // A grey shows no hue, so set one, drag value, then raise saturation and
    // read the hue back out: 80 of 240 across the strip is 120 degrees.
    await tester.tapAt(hue.centerLeft + const Offset(80, 0));
    await tester.pump();
    expect(pending(tester), const Color(0xFF808080));

    await tester.dragFrom(pad.centerLeft, const Offset(0, -30));
    await tester.pump();
    final greyer = pending(tester);
    expect(HSVColor.fromColor(greyer).saturation, 0);
    expect(HSVColor.fromColor(greyer).value, greaterThan(0.5));

    await tester.tapAt(pad.centerRight - const Offset(0.5, 0));
    await tester.pump();
    expect(HSVColor.fromColor(pending(tester)).hue, closeTo(120, 1));
  });

  testWidgets('the pad corners reach pure white and pure black', (tester) async {
    await openPicker(tester, const Color(0xFFFF0000));
    final pad = tester.getRect(find.byKey(tcPickerPadKey));

    await tester.tapAt(pad.topLeft);
    await tester.pump();
    expect(pending(tester), const Color(0xFFFFFFFF));

    // Dragging past a corner clamps to it, which is how the last row of
    // pixels is reachable at all.
    await tester.dragFrom(pad.center, const Offset(400, 400));
    await tester.pump();
    expect(pending(tester), const Color(0xFF000000));
  });

  testWidgets('the old swatch goes back to the color the picker opened on', (tester) async {
    await openPicker(tester, const Color(0xFF102030));

    await tester.enterText(find.byKey(tcPickerHexKey), '#00ff88');
    await tester.pump();
    expect(pending(tester), const Color(0xFF00FF88));

    await tester.tap(find.byKey(tcPickerOldKey));
    await tester.pump();
    expect(hexText(tester), '#102030');
  });
}
