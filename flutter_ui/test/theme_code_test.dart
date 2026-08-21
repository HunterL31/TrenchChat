import 'package:flutter/painting.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/theme/theme_code.dart';
import 'package:flutter_ui/theme/theme_presets.dart';
import 'package:flutter_ui/theme/theme_spec.dart';

void main() {
  test('a code round-trips colors, styles and the name', () {
    final spec = ThemeSpec(
      base: {'bgApp': const Color(0xFF102030), 'accentPrimary': const Color(0xFF00FF88)},
      sections: {
        'content': {'textPrimary': const Color(0xCC112233)},
      },
      styles: {
        'base': {'glow': false},
        'content': {'textScale': 1.1, 'displayFont': 'IBM Plex Mono'},
      },
    );

    final decoded = decodeThemeCode(encodeThemeCode('Deep Trench', spec));

    expect(decoded, isNotNull);
    expect(decoded!.name, 'Deep Trench');
    expect(decoded.spec, spec);
  });

  test('a code carries section ids this client does not know', () {
    final spec = ThemeSpec.fromJson({
      'version': 1,
      'sections': {
        'holoDeck': {'textPrimary': '#ff00ff'},
      },
    });

    final decoded = decodeThemeCode(encodeThemeCode('Holo', spec));

    expect(decoded!.spec.sections['holoDeck']!['textPrimary'], const Color(0xFFFF00FF));
  });

  test('an empty spec round-trips as an empty spec', () {
    final decoded = decodeThemeCode(encodeThemeCode('Stock', ThemeSpec.empty));

    expect(decoded!.name, 'Stock');
    expect(decoded.spec.isEmpty, isTrue);
  });

  test('a code with no name falls back to the default', () {
    // A spec whose document carries no name key at all.
    final code = encodeThemeCode('   ', ThemeSpec.empty);

    expect(decodeThemeCode(code)!.name, defaultThemeCodeName);
  });

  test('a long name is clamped to what the library accepts', () {
    final decoded = decodeThemeCode(encodeThemeCode('x' * 200, ThemeSpec.empty));

    expect(decoded!.name.length, maxThemeNameLength);
  });

  test('malformed codes decode to null', () {
    for (final bad in [
      '',
      'hello',
      'tct1:',
      'tct0:eyJ2ZXJzaW9uIjoxfQ',
      'tct1:!!!!',
      'tct1:zzzzzzzzzzzz',
      'tct1:${'A' * 40}',
    ]) {
      expect(decodeThemeCode(bad), isNull, reason: bad);
    }
  });

  test('a code that inflates to a JSON non-object decodes to null', () {
    // "[1,2,3]" deflated, then encoded the same way encodeThemeCode does.
    final list = encodeThemeCode('x', ThemeSpec.empty).replaceRange(0, 5, 'tct2:');

    expect(decodeThemeCode(list), isNull);
  });

  test('the regex finds codes in running text and ignores other words', () {
    final code = encodeThemeCode('Ember', themePresets.last.spec);
    final matches = themeCodeRe
        .allMatches('try this $code and :emoji: and tct1 alone')
        .map((m) => m.group(0))
        .toList();

    expect(matches, [code]);
  });

  test('a full preset stays well under a thousand characters', () {
    final code = encodeThemeCode('Ember', themePresets.last.spec);

    expect(code.startsWith(themeCodePrefix), isTrue);
    expect(code.length, lessThan(1000));
    expect(decodeThemeCode(code)!.spec, themePresets.last.spec);
  });
}
