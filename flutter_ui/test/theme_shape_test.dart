// The shape layer: what a section's corner radius, avatar cut and panel
// edge resolve to at a call site, and that a section setting none of them
// leaves every widget the shape it had before shapes existed.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/models/link_quality.dart';
import 'package:flutter_ui/screens/main_window/map_tab.dart';
import 'package:flutter_ui/theme/notch.dart';
import 'package:flutter_ui/theme/quality_tiers.dart';
import 'package:flutter_ui/theme/section_theme.dart';
import 'package:flutter_ui/theme/shape.dart';
import 'package:flutter_ui/theme/theme_spec.dart';
import 'package:flutter_ui/theme/tint.dart';
import 'package:flutter_ui/theme/tokens.dart';
import 'package:flutter_ui/widgets/avatar.dart';
import 'package:flutter_ui/widgets/signal_meter.dart';
import 'package:flutter_ui/widgets/tc_tooltip.dart';

/// Pumps [child] inside [section] resolved from [spec], and hands the
/// builder a context that sits under the SectionTheme.
Future<BuildContext> _pumpSection(
  WidgetTester tester, {
  required ThemeSpec spec,
  TCSection section = TCSection.content,
}) async {
  late BuildContext seen;
  await tester.pumpWidget(SectionTheme(
    spec: spec,
    section: section,
    child: Builder(builder: (context) {
      seen = context;
      return const SizedBox();
    }),
  ));
  return seen;
}

ThemeSpec _shaped(Map<String, Object> style) =>
    ThemeSpec(styles: {ThemeSpec.baseStyleScope: style});

void main() {
  group('corner radius', () {
    testWidgets('a section that rounds nothing hands back the stock radius',
        (tester) async {
      final context = await _pumpSection(tester, spec: ThemeSpec.empty);

      expect(tcRadius(context), 0);
      expect(tcRadius(context, stock: TCSpace.radiusSm), TCSpace.radiusSm);
      expect(tcCorners(context), isNull, reason: 'no radius means no rounding at all');
      expect(tcIsRounded(context), isFalse);
    });

    testWidgets('a rounded section wins over the stock radius, scaled',
        (tester) async {
      final context = await _pumpSection(
        tester,
        spec: _shaped({TCSectionStyle.keyCornerRadius: 8.0}),
      );

      expect(tcRadius(context), 8);
      expect(tcRadius(context, stock: TCSpace.radiusSm), 8);
      expect(tcRadius(context, scale: 0.5), 4);
      expect(tcCorners(context), BorderRadius.circular(8));
      expect(tcIsRounded(context), isTrue);
    });

    testWidgets('a section overrides the base radius', (tester) async {
      final spec = ThemeSpec(styles: {
        ThemeSpec.baseStyleScope: {TCSectionStyle.keyCornerRadius: 8.0},
        TCSection.serverRail.wireId: {TCSectionStyle.keyCornerRadius: 12.0},
      });

      final rail =
          await _pumpSection(tester, spec: spec, section: TCSection.serverRail);
      expect(tcRadius(rail), 12);

      final content =
          await _pumpSection(tester, spec: spec, section: TCSection.content);
      expect(tcRadius(content), 8);
    });

    testWidgets('with no SectionTheme above it, nothing is rounded', (tester) async {
      late BuildContext seen;
      await tester.pumpWidget(Builder(builder: (context) {
        seen = context;
        return const SizedBox();
      }));

      expect(tcRadius(seen), 0);
      expect(tcIsRounded(seen), isFalse);
    });
  });

  group('avatar shape', () {
    testWidgets('square leaves the call site whatever stock it asked for',
        (tester) async {
      final context = await _pumpSection(tester, spec: ThemeSpec.empty);
      expect(tcAvatarCorners(context, 36, stock: TCSpace.radiusSm),
          BorderRadius.circular(TCSpace.radiusSm));
      expect(tcAvatarCorners(context, 36), isNull,
          reason: 'a square that was never rounded stays unrounded');
    });

    testWidgets('circle is half the size, whatever the corner radius',
        (tester) async {
      final context = await _pumpSection(
        tester,
        spec: _shaped({
          TCSectionStyle.keyAvatarShape: TCSectionStyle.avatarCircle,
          TCSectionStyle.keyCornerRadius: 4.0,
        }),
      );
      expect(tcAvatarCorners(context, 36), BorderRadius.circular(18));
    });

    testWidgets('rounded sits between square and circle', (tester) async {
      final context = await _pumpSection(
        tester,
        spec: _shaped({TCSectionStyle.keyAvatarShape: TCSectionStyle.avatarRounded}),
      );
      final radius = tcAvatarCorners(context, 36)!.topLeft.x;
      expect(radius, greaterThan(TCSpace.radiusSm));
      expect(radius, lessThan(18));
    });

    testWidgets('a square section with a corner radius rounds its avatars too',
        (tester) async {
      final context = await _pumpSection(
        tester,
        spec: _shaped({TCSectionStyle.keyCornerRadius: 6.0}),
      );
      expect(tcAvatarCorners(context, 36), BorderRadius.circular(6));
      expect(tcAvatarCorners(context, 36, stock: TCSpace.radiusSm),
          BorderRadius.circular(6),
          reason: 'the section radius wins over the stock one');
    });

    testWidgets('Avatar clips its image to the section shape', (tester) async {
      await tester.pumpWidget(MaterialApp(
        home: SectionTheme(
          spec: _shaped({TCSectionStyle.keyAvatarShape: TCSectionStyle.avatarCircle}),
          section: TCSection.content,
          child: const Avatar(name: 'Ada', size: 40),
        ),
      ));

      final decoration = tester
          .widget<Container>(find.descendant(
            of: find.byType(Avatar),
            matching: find.byType(Container),
          ))
          .decoration as BoxDecoration;
      expect(decoration.borderRadius, BorderRadius.circular(20));
    });
  });

  group('panel edge', () {
    testWidgets('the stock edge is the angular notch', (tester) async {
      await tester.pumpWidget(SectionTheme(
        spec: ThemeSpec.empty,
        section: TCSection.dialogs,
        child: const TcPanel(child: SizedBox(width: 40, height: 40)),
      ));

      expect(find.byType(NotchedPanel), findsOneWidget);
    });

    testWidgets('a plain edge rounds by the section radius instead',
        (tester) async {
      await tester.pumpWidget(SectionTheme(
        spec: _shaped({
          TCSectionStyle.keyPanelEdge: TCSectionStyle.panelPlain,
          TCSectionStyle.keyCornerRadius: 10.0,
        }),
        section: TCSection.dialogs,
        child: const TcPanel(child: SizedBox(width: 40, height: 40)),
      ));

      expect(find.byType(NotchedPanel), findsNothing);
      final decoration = tester
          .widget<Container>(find.descendant(
            of: find.byType(TcPanel),
            matching: find.byType(Container),
          ))
          .decoration as BoxDecoration;
      expect(decoration.borderRadius, BorderRadius.circular(10));
    });
  });

  group('fixed design-system colors follow the theme', () {
    testWidgets('a section that moves nothing leaves them exactly as drawn',
        (tester) async {
      final context = await _pumpSection(tester, spec: ThemeSpec.empty);
      const drawn = Color(0xFF8BC34A);

      expect(tcShiftLike(drawn, from: TCColors.statusOnline, to: TCColors.statusOnline),
          drawn);

      // The good tier is the one meter color no token names: unthemed, it is
      // still the yellow-green the design system drew.
      final bars = await _meterBarColors(tester, context, LinkQualityLevel.good);
      expect(bars.first, HSLColor.fromAHSL(1, 70, 0.85, 0.55).toColor());
    });

    testWidgets('the meter carries its good tier onto the section',
        (tester) async {
      final spec = ThemeSpec(base: {'statusOnline': const Color(0xFF23A55A)});
      final context = await _pumpSection(tester, spec: spec);
      final bars = await _meterBarColors(tester, context, LinkQualityLevel.good);

      expect(bars.first, isNot(HSLColor.fromAHSL(1, 70, 0.85, 0.55).toColor()),
          reason: 'a themed online color must move the tier beside it');
      final moved = HSLColor.fromColor(bars.first);
      final stock = HSLColor.fromColor(HSLColor.fromAHSL(1, 70, 0.85, 0.55).toColor());
      final online = HSLColor.fromColor(const Color(0xFF23A55A));
      final stockOnline = HSLColor.fromColor(TCColors.statusOnline);
      expect(moved.hue, closeTo(stock.hue + (online.hue - stockOnline.hue), 0.5));
    });

    testWidgets('the map draws the same tier the meter does', (tester) async {
      final spec = ThemeSpec(base: {'statusOnline': const Color(0xFF00A884)});
      final context = await _pumpSection(tester, spec: spec);
      final colors = SectionTheme.of(context);
      final bars = await _meterBarColors(tester, context, LinkQualityLevel.good);

      expect(mapQualityColor(3, colors: colors), bars.first,
          reason: 'the map and the meter must never disagree about good');
      expect(mapQualityColor(3, colors: colors), isNot(stockGoodQuality));
      expect(mapQualityColor(3), stockGoodQuality,
          reason: 'the stock palette still draws it as the design system did');
    });

    testWidgets('tooltips wear the section palette, not Material grey',
        (tester) async {
      final spec = ThemeSpec(base: {'bgSurfaceRaised': const Color(0xFF313338)});
      final context = await _pumpSection(tester, spec: spec);

      final decoration = tcTooltipDecoration(context) as BoxDecoration;
      expect(decoration.color, const Color(0xFF313338));
      expect(tcTooltipTextStyle(context).color, isNotNull);
    });

    testWidgets('a tooltip rounds with its section', (tester) async {
      final square = await _pumpSection(tester, spec: ThemeSpec.empty);
      expect((tcTooltipDecoration(square) as BoxDecoration).borderRadius, isNull);

      final round = await _pumpSection(
        tester,
        spec: _shaped({TCSectionStyle.keyCornerRadius: 8.0}),
      );
      expect((tcTooltipDecoration(round) as BoxDecoration).borderRadius,
          BorderRadius.circular(4));
    });
  });
}

/// The lit-bar colors a [SignalMeter] paints for [level] under [context]'s
/// section.
Future<List<Color>> _meterBarColors(
    WidgetTester tester, BuildContext context, LinkQualityLevel level) async {
  final section = SectionTheme.of(context);
  final style = SectionTheme.styleOf(context);
  await tester.pumpWidget(SectionTheme.resolved(
    section: TCSection.topBar,
    colors: section,
    style: style,
    child: Directionality(
      textDirection: TextDirection.ltr,
      child: SignalMeter(level: level),
    ),
  ));
  return tester
      .widgetList<Container>(find.descendant(
        of: find.byType(SignalMeter),
        matching: find.byType(Container),
      ))
      .map((c) => (c.decoration as BoxDecoration).color!)
      .toList();
}
