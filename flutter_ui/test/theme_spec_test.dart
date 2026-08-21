// The per-section theming foundation: parsing a theme document written by
// anyone (including a newer client), resolving it per section, and reading
// the result out of the tree.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/theme/section_theme.dart';
import 'package:flutter_ui/theme/theme_spec.dart';
import 'package:flutter_ui/theme/tokens.dart';

const _red = Color(0xFFFF0000);
const _blue = Color(0xFF0000FF);
const _translucent = Color(0x80112233);

void main() {
  group('color parsing', () {
    test('accepts #RRGGBB as fully opaque', () {
      expect(parseThemeColor('#ff0000'), _red);
      expect(parseThemeColor('#FF0000'), _red);
    });

    test('accepts #AARRGGBB with its alpha', () {
      expect(parseThemeColor('#80112233'), _translucent);
    });

    test('rejects malformed strings and non-strings', () {
      for (final bad in <Object?>[
        null,
        '',
        '#fff',
        '#gggggg',
        'red',
        '#ff00001',
        123,
        <String>[],
      ]) {
        expect(parseThemeColor(bad), isNull, reason: 'should reject $bad');
      }
    });

    test('serializes lowercase, with alpha only when not opaque', () {
      expect(encodeThemeColor(_red), '#ff0000');
      expect(encodeThemeColor(_translucent), '#80112233');
    });
  });

  group('ThemeSpec parsing', () {
    test('empty is empty and round trips', () {
      expect(ThemeSpec.empty.isEmpty, isTrue);
      expect(ThemeSpec.fromJson(ThemeSpec.empty.toJson()).isEmpty, isTrue);
      expect(ThemeSpec.fromJson(const {}).isEmpty, isTrue);
    });

    test('a full document survives parse -> serialize -> parse', () {
      final spec = ThemeSpec(
        base: {'bgApp': _red, 'textPrimary': _translucent},
        sections: {
          TCSection.topBar.wireId: {'bgSurface': _blue},
          TCSection.serverRail.wireId: {'accentPrimary': _red},
        },
      );

      final json = spec.toJson();
      expect(json['version'], themeSpecVersion);
      expect((json['base'] as Map)['bgApp'], '#ff0000');
      expect((json['sections'] as Map)['topBar'], {'bgSurface': '#0000ff'});

      expect(ThemeSpec.fromJson(json), spec);
      expect(ThemeSpec.fromJson(json).isEmpty, isFalse);
    });

    test('garbage never throws and yields an empty spec', () {
      for (final junk in <Map<String, dynamic>>[
        {'base': 'not a map'},
        {'base': 42, 'sections': 'nope'},
        {'sections': <String, dynamic>{'topBar': 7}},
        {'version': 'banana', 'base': <String, dynamic>{}},
        {'base': <String, dynamic>{'bgApp': '#zzz'}},
      ]) {
        expect(ThemeSpec.fromJson(junk).isEmpty, isTrue, reason: '$junk');
      }
    });

    test('unknown token keys are dropped, known ones kept', () {
      final spec = ThemeSpec.fromJson(const {
        'base': {'bgApp': '#ff0000', 'notAToken': '#00ff00', 'linkCurrentDomainColor': '#00ff00'},
      });
      expect(spec.base.keys, ['bgApp']);
    });

    test('an unknown section id survives a round trip', () {
      final spec = ThemeSpec.fromJson(const {
        'sections': {
          'topBar': {'bgSurface': '#0000ff'},
          'sectionFromTheFuture': {'bgApp': '#ff0000'},
        },
      });
      expect(spec.sections.keys, containsAll(['topBar', 'sectionFromTheFuture']));
      expect(
        (ThemeSpec.fromJson(spec.toJson()).sections['sectionFromTheFuture'])!['bgApp'],
        _red,
      );
      // ... and does not disturb the sections this client does know.
      expect(spec.resolve(TCSection.topBar).bgSurface, _blue);
      expect(spec.resolve(TCSection.topBar).bgApp, TCColors.bgApp);
    });
  });

  group('resolution precedence', () {
    test('stock defaults exactly match the TCColors aliases', () {
      final stock = TCSectionColors.stock;
      expect(stock.bgApp, TCColors.bgApp);
      expect(stock.bgSurface, TCColors.bgSurface);
      expect(stock.bgHover, TCColors.bgHover);
      expect(stock.borderAccent, TCColors.borderAccent);
      expect(stock.textPrimary, TCColors.textPrimary);
      expect(stock.textOnAccent, TCColors.textOnAccent);
      expect(stock.accentPrimary, TCColors.accentPrimary);
      expect(stock.accentSecondaryMuted, TCColors.accentSecondaryMuted);
      expect(stock.statusDanger, TCColors.statusDanger);
      expect(stock.linkColor, TCColors.linkColor);
      expect(stock.linkHoverColor, TCColors.linkHoverColor);
      expect(stock.asMap().length, 32);
      expect(stock.asMap().keys, TCSectionColors.tokenKeys);
    });

    test('an empty spec resolves every section to stock', () {
      for (final section in TCSection.values) {
        expect(ThemeSpec.empty.resolve(section), TCSectionColors.stock);
      }
    });

    test('default < base < section', () {
      final spec = ThemeSpec(
        base: {'bgApp': _red, 'textPrimary': _red},
        sections: {
          TCSection.content.wireId: {'bgApp': _blue},
        },
      );

      final content = spec.resolve(TCSection.content);
      expect(content.bgApp, _blue, reason: 'section wins over base');
      expect(content.textPrimary, _red, reason: 'base wins over default');
      expect(content.bgInset, TCColors.bgInset, reason: 'untouched stays default');

      // A section with no overrides of its own still sees the base.
      expect(spec.resolve(TCSection.presence).bgApp, _red);
      expect(spec.resolveBase().bgApp, _red);
    });
  });

  group('copy methods', () {
    test('withBaseOverride and withSectionOverride leave the original alone', () {
      final original = ThemeSpec.empty;
      final next = original
          .withBaseOverride('bgApp', _red)
          .withSectionOverride(TCSection.topBar, 'bgSurface', _blue);

      expect(original.isEmpty, isTrue);
      expect(next.resolve(TCSection.topBar).bgApp, _red);
      expect(next.resolve(TCSection.topBar).bgSurface, _blue);
      expect(next.resolve(TCSection.content).bgSurface, TCColors.bgSurface);
    });

    test('a null color clears one override', () {
      final spec = ThemeSpec.empty
          .withBaseOverrides({'bgApp': _red, 'textPrimary': _blue})
          .withBaseOverride('bgApp', null);
      expect(spec.base.keys, ['textPrimary']);
    });

    test('clearSection and clearBase drop their half', () {
      final spec = ThemeSpec(
        base: {'bgApp': _red},
        sections: {
          TCSection.topBar.wireId: {'bgSurface': _blue},
        },
      );
      expect(spec.clearSection(TCSection.topBar).sections, isEmpty);
      expect(spec.clearSection(TCSection.topBar).base, {'bgApp': _red});
      expect(spec.clearBase().base, isEmpty);
      expect(spec.clearBase().sections.keys, ['topBar']);
      expect(spec.clearBase().clearSection(TCSection.topBar).isEmpty, isTrue);
    });

    test('clearing a section by nulling its last token removes the section', () {
      final spec = ThemeSpec.empty
          .withSectionOverride(TCSection.topBar, 'bgSurface', _blue)
          .withSectionOverride(TCSection.topBar, 'bgSurface', null);
      expect(spec.sections, isEmpty);
      expect(spec.isEmpty, isTrue);
    });
  });

  group('SectionTheme', () {
    testWidgets('falls back to stock defaults with no ancestor', (tester) async {
      late TCSectionColors seen;
      await tester.pumpWidget(Builder(builder: (context) {
        seen = SectionTheme.of(context);
        return const SizedBox();
      }));

      expect(seen, TCSectionColors.stock);
      expect(seen.bgApp, TCColors.bgApp);
    });

    testWidgets('serves the enclosing section its resolved colors', (tester) async {
      final spec = ThemeSpec(
        base: {'textPrimary': _red},
        sections: {
          TCSection.topBar.wireId: {'bgApp': _blue},
        },
      );
      late TCSectionColors topBar;
      late TCSectionColors content;

      await tester.pumpWidget(Column(
        textDirection: TextDirection.ltr,
        children: [
          SectionTheme(
            spec: spec,
            section: TCSection.topBar,
            child: Builder(builder: (context) {
              topBar = SectionTheme.of(context);
              return const SizedBox();
            }),
          ),
          SectionTheme(
            spec: spec,
            section: TCSection.content,
            child: Builder(builder: (context) {
              content = SectionTheme.of(context);
              return const SizedBox();
            }),
          ),
        ],
      ));

      expect(topBar.bgApp, _blue);
      expect(topBar.textPrimary, _red);
      expect(content.bgApp, TCColors.bgApp);
      expect(content.textPrimary, _red);
    });

    testWidgets('the nearest section wins when they nest', (tester) async {
      final spec = ThemeSpec(sections: {
        TCSection.content.wireId: {'bgApp': _red},
        TCSection.topBar.wireId: {'bgApp': _blue},
      });
      late TCSection section;
      late TCSectionColors colors;

      await tester.pumpWidget(SectionTheme(
        spec: spec,
        section: TCSection.content,
        child: SectionTheme(
          spec: spec,
          section: TCSection.topBar,
          child: Builder(builder: (context) {
            section = SectionTheme.sectionOf(context)!;
            colors = SectionTheme.of(context);
            return const SizedBox();
          }),
        ),
      ));

      expect(section, TCSection.topBar);
      expect(colors.bgApp, _blue);
    });

    testWidgets('a changed theme rebuilds dependents', (tester) async {
      final colors = <Color>[];
      Widget shell(ThemeSpec spec) => SectionTheme(
            spec: spec,
            section: TCSection.content,
            child: Builder(builder: (context) {
              colors.add(SectionTheme.of(context).bgApp);
              return const SizedBox();
            }),
          );

      await tester.pumpWidget(shell(ThemeSpec.empty));
      await tester.pumpWidget(shell(ThemeSpec(base: {'bgApp': _red})));

      expect(colors, [TCColors.bgApp, _red]);
    });
  });

  group('style layer', () {
    test('an unset document resolves every section to the stock style', () {
      expect(TCSectionStyle.stock.textScale, 1.0);
      expect(TCSectionStyle.stock.glow, isTrue);
      expect(TCSectionStyle.stock.displayFont, 'VT323');
      for (final section in TCSection.values) {
        expect(ThemeSpec.empty.resolveStyle(section), TCSectionStyle.stock);
      }
      expect(ThemeSpec.empty.resolveBaseStyle(), TCSectionStyle.stock);
      expect(ThemeSpec.fromJson(const {'styles': {}}).isEmpty, isTrue);
    });

    test('a style document survives parse -> serialize -> parse', () {
      final spec = ThemeSpec(styles: {
        ThemeSpec.baseStyleScope: {'textScale': 1.1, 'glow': false},
        TCSection.content.wireId: {'displayFont': 'IBM Plex Mono'},
      });

      final json = spec.toJson();
      expect((json['styles'] as Map)['base'], {'textScale': 1.1, 'glow': false});
      expect((json['styles'] as Map)['content'], {'displayFont': 'IBM Plex Mono'});

      expect(ThemeSpec.fromJson(json), spec);
      expect(ThemeSpec.fromJson(json).isEmpty, isFalse);
      expect(ThemeSpec.fromJson(json).resolveStyle(TCSection.content).displayFont,
          'IBM Plex Mono');
    });

    test('isEmpty accounts for styles alone', () {
      final spec = ThemeSpec.empty.withStyleOverride(TCSection.topBar, 'glow', false);
      expect(spec.isEmpty, isFalse);
      expect(spec.clearSection(TCSection.topBar).isEmpty, isTrue);
      expect(ThemeSpec.empty.withStyleOverride(null, 'glow', false).clearBase().isEmpty, isTrue);
    });

    test('a text scale is clamped to the allowed range', () {
      final spec = ThemeSpec.fromJson(const {
        'styles': {
          'base': {'textScale': 9},
          'content': {'textScale': 0.1},
        },
      });
      expect(spec.resolveBaseStyle().textScale, TCSectionStyle.maxTextScale);
      expect(spec.resolveStyle(TCSection.content).textScale, TCSectionStyle.minTextScale);
      expect(
        ThemeSpec.empty.withStyleOverride(null, 'textScale', 4.0).styleOverridesFor(null),
        {'textScale': TCSectionStyle.maxTextScale},
      );
    });

    test('unknown style keys and malformed values are dropped', () {
      final spec = ThemeSpec.fromJson(const {
        'styles': {
          'base': {
            'textScale': 'big',
            'glow': 'yes',
            'displayFont': 'Comic Sans',
            'scanlines': true,
            'displayFontSize': 12,
          },
        },
      });
      expect(spec.isEmpty, isTrue);
      expect(spec.resolveBaseStyle(), TCSectionStyle.stock);

      final kept = ThemeSpec.fromJson(const {
        'styles': {
          'base': {'glow': false, 'scanlines': true},
        },
      });
      expect(kept.styleOverridesFor(null), {'glow': false});
    });

    test('garbage in styles never throws', () {
      for (final junk in <Map<String, dynamic>>[
        {'styles': 'not a map'},
        {'styles': 7},
        {'styles': <String, dynamic>{'base': 'nope'}},
        {'styles': <String, dynamic>{'base': <String, dynamic>{'textScale': null}}},
      ]) {
        expect(ThemeSpec.fromJson(junk).isEmpty, isTrue, reason: '$junk');
      }
    });

    test('an unknown section id inside styles survives a round trip', () {
      final spec = ThemeSpec.fromJson(const {
        'styles': {
          'content': {'glow': false},
          'sectionFromTheFuture': {'textScale': 1.25},
        },
      });
      expect(spec.styles.keys, containsAll(['content', 'sectionFromTheFuture']));
      expect(
        ThemeSpec.fromJson(spec.toJson()).styles['sectionFromTheFuture'],
        {'textScale': 1.25},
      );
      expect(spec.resolveStyle(TCSection.content).glow, isFalse);
    });

    test('default < base < section', () {
      final spec = ThemeSpec(styles: {
        ThemeSpec.baseStyleScope: {'textScale': 1.25, 'glow': false},
        TCSection.content.wireId: {'textScale': 0.9},
      });

      final content = spec.resolveStyle(TCSection.content);
      expect(content.textScale, 0.9, reason: 'section wins over base');
      expect(content.glow, isFalse, reason: 'base wins over default');
      expect(content.displayFont, 'VT323', reason: 'untouched stays default');

      expect(spec.resolveStyle(TCSection.presence).textScale, 1.25);
      expect(spec.resolveBaseStyle().textScale, 1.25);
      expect(spec.styleOverridesFor(TCSection.presence), isEmpty);
      expect(spec.styleOverridesFor(TCSection.content), {'textScale': 0.9});
      expect(spec.styleOverridesFor(null), {'textScale': 1.25, 'glow': false});
    });

    test('style editors leave the original alone and clear on null', () {
      final original = ThemeSpec(base: {'bgApp': _red});
      final next = original
          .withStyleOverride(null, 'glow', false)
          .withStyleOverride(TCSection.topBar, 'displayFont', 'IBM Plex Mono');

      expect(original.styles, isEmpty);
      expect(next.base, {'bgApp': _red}, reason: 'colors are untouched');
      expect(next.resolveStyle(TCSection.topBar).glow, isFalse);
      expect(next.resolveStyle(TCSection.topBar).displayFont, 'IBM Plex Mono');
      expect(next.resolveStyle(TCSection.content).displayFont, 'VT323');

      final cleared = next.withStyleOverride(TCSection.topBar, 'displayFont', null);
      expect(cleared.styles.containsKey('topBar'), isFalse);
      expect(cleared.resolveStyle(TCSection.topBar).glow, isFalse, reason: 'base survives');
    });

    test('a value the style layer cannot store clears the key', () {
      final spec = ThemeSpec.empty
          .withStyleOverride(null, 'displayFont', 'IBM Plex Mono')
          .withStyleOverride(null, 'displayFont', 'Comic Sans');
      expect(spec.isEmpty, isTrue);
    });

    test('clearing a scope drops its colors and its styles together', () {
      final spec = ThemeSpec(
        base: {'bgApp': _red},
        sections: {
          TCSection.topBar.wireId: {'bgSurface': _blue},
        },
        styles: {
          ThemeSpec.baseStyleScope: {'glow': false},
          TCSection.topBar.wireId: {'textScale': 1.1},
        },
      );

      expect(spec.clearSection(TCSection.topBar).styles.keys, ['base']);
      expect(spec.clearSection(TCSection.topBar).sections, isEmpty);
      expect(spec.clearBase().styles.keys, ['topBar']);
      expect(spec.clearBase().base, isEmpty);
      expect(spec.clearBase().clearSection(TCSection.topBar).isEmpty, isTrue);
    });
  });

  group('SectionTheme styles', () {
    testWidgets('styleOf falls back to the stock style with no ancestor', (tester) async {
      late TCSectionStyle seen;
      await tester.pumpWidget(Builder(builder: (context) {
        seen = SectionTheme.styleOf(context);
        return const SizedBox();
      }));

      expect(seen, TCSectionStyle.stock);
      expect(seen.displayFont, 'VT323');
      expect(seen.glow, isTrue);
    });

    testWidgets('serves the enclosing section its resolved style', (tester) async {
      final spec = ThemeSpec(styles: {
        ThemeSpec.baseStyleScope: {'glow': false},
        TCSection.topBar.wireId: {'displayFont': 'IBM Plex Mono'},
      });
      late TCSectionStyle topBar;

      await tester.pumpWidget(SectionTheme(
        spec: spec,
        section: TCSection.topBar,
        child: Builder(builder: (context) {
          topBar = SectionTheme.styleOf(context);
          return const SizedBox();
        }),
      ));

      expect(topBar.glow, isFalse);
      expect(topBar.displayFont, 'IBM Plex Mono');
    });

    testWidgets('a text scale multiplies the inherited text scaling', (tester) async {
      final spec = ThemeSpec.empty.withStyleOverride(null, 'textScale', 1.25);
      late TextScaler scaler;

      await tester.pumpWidget(MediaQuery(
        data: const MediaQueryData(textScaler: TextScaler.linear(1.2)),
        child: SectionTheme(
          spec: spec,
          section: TCSection.content,
          child: Builder(builder: (context) {
            scaler = MediaQuery.textScalerOf(context);
            return const SizedBox();
          }),
        ),
      ));

      expect(scaler.scale(10), closeTo(10 * 1.2 * 1.25, 0.0001));
    });

    testWidgets('the default scale leaves the inherited scaling alone', (tester) async {
      late TextScaler scaler;
      await tester.pumpWidget(MediaQuery(
        data: const MediaQueryData(textScaler: TextScaler.linear(1.2)),
        child: SectionTheme(
          spec: ThemeSpec.empty,
          section: TCSection.content,
          child: Builder(builder: (context) {
            scaler = MediaQuery.textScalerOf(context);
            return const SizedBox();
          }),
        ),
      ));

      expect(scaler.scale(10), closeTo(12, 0.0001));
    });

    testWidgets('a nested section replaces the enclosing scale, never compounds it',
        (tester) async {
      final spec = ThemeSpec.empty
          .withStyleOverride(null, 'textScale', 1.25)
          .withStyleOverride(TCSection.topBar, 'textScale', 1.0);
      late TextScaler outer;
      late TextScaler inner;

      await tester.pumpWidget(MediaQuery(
        data: const MediaQueryData(textScaler: TextScaler.linear(1.2)),
        child: SectionTheme(
          spec: spec,
          section: TCSection.content,
          child: Builder(builder: (context) {
            outer = MediaQuery.textScalerOf(context);
            return SectionTheme(
              spec: spec,
              section: TCSection.topBar,
              child: Builder(builder: (context) {
                inner = MediaQuery.textScalerOf(context);
                return const SizedBox();
              }),
            );
          }),
        ),
      ));

      expect(outer.scale(10), closeTo(15, 0.0001));
      expect(inner.scale(10), closeTo(12, 0.0001), reason: 'the top bar opts back out');
    });

    testWidgets('a changed style rebuilds dependents', (tester) async {
      final seen = <bool>[];
      Widget shell(ThemeSpec spec) => SectionTheme(
            spec: spec,
            section: TCSection.content,
            child: Builder(builder: (context) {
              seen.add(SectionTheme.styleOf(context).glow);
              return const SizedBox();
            }),
          );

      await tester.pumpWidget(shell(ThemeSpec.empty));
      await tester.pumpWidget(shell(ThemeSpec.empty.withStyleOverride(null, 'glow', false)));

      expect(seen, [true, false]);
    });
  });

  group('TCSection wire ids', () {
    test('the wire id is the enum name, both ways', () {
      expect(TCSection.serverRail.wireId, 'serverRail');
      expect(TCSection.channelList.wireId, 'channelList');
      expect(TCSection.presence.wireId, 'presence');
      expect(TCSection.topBar.wireId, 'topBar');
      expect(TCSection.content.wireId, 'content');
      expect(TCSection.dialogs.wireId, 'dialogs');
      for (final section in TCSection.values) {
        expect(TCSection.fromWireId(section.wireId), section);
      }
      expect(TCSection.fromWireId('sectionFromTheFuture'), isNull);
    });
  });
}
