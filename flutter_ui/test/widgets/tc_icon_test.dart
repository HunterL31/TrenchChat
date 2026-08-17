import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/theme/tokens.dart';
import 'package:flutter_ui/widgets/tc_icon.dart';

void main() {
  test('catalog names are unique and non-empty', () {
    final names = TcIcons.all.map((i) => i.name).toList();
    expect(names, isNot(contains(isEmpty)));
    expect(names.toSet().length, names.length);
  });

  test('every icon has geometry and stays inside the 16-unit grid', () {
    for (final icon in TcIcons.all) {
      expect(icon.strokes.isNotEmpty || icon.fills.isNotEmpty, isTrue,
          reason: '${icon.name} has no geometry');
      for (final path in [...icon.strokes, ...icon.fills]) {
        expect(path.length, greaterThanOrEqualTo(2),
            reason: '${icon.name} has a degenerate path');
        for (final p in path) {
          expect(p.dx, inInclusiveRange(0, 16), reason: '${icon.name} x out of grid');
          expect(p.dy, inInclusiveRange(0, 16), reason: '${icon.name} y out of grid');
        }
      }
    }
  });

  testWidgets('TcIcon lays out at the requested size', (tester) async {
    await tester.pumpWidget(const Center(child: TcIcon(TcIcons.settings, size: 24)));
    expect(tester.getSize(find.byType(TcIcon)), const Size(24, 24));
  });

  testWidgets('TcIcon defaults to textSecondary and honors an explicit color', (tester) async {
    await tester.pumpWidget(const Center(child: TcIcon(TcIcons.lock)));
    CustomPaint paintOf() => tester.widget<CustomPaint>(
        find.descendant(of: find.byType(TcIcon), matching: find.byType(CustomPaint)));
    expect((paintOf().painter as dynamic).color, TCColors.textSecondary);

    await tester.pumpWidget(Center(child: TcIcon(TcIcons.lock, color: TCColors.accentPrimary)));
    expect((paintOf().painter as dynamic).color, TCColors.accentPrimary);
  });
}
