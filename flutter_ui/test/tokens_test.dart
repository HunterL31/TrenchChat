// Verifies the ported tokens (lib/theme/tokens.dart) reproduce the exact
// values in trenchchat-design-system's tokens/{colors,typography,spacing}.css.
// HSL triples below are copied independently from colors.css so this test
// catches a transcription error in tokens.dart, not just a self-consistency
// check against the same formula.
import 'package:flutter/painting.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/theme/tokens.dart';

Color _hsl(double h, double s, double l) => HSLColor.fromAHSL(1, h, s, l).toColor();

void main() {
  group('color tokens match tokens/colors.css', () {
    test('green scale', () {
      expect(TCColors.green950, _hsl(108, 0.40, 0.05));
      expect(TCColors.green900, _hsl(108, 0.45, 0.09));
      expect(TCColors.green800, _hsl(108, 0.50, 0.15));
      expect(TCColors.green700, _hsl(108, 0.55, 0.21));
      expect(TCColors.green600, _hsl(108, 0.58, 0.26));
      expect(TCColors.green500, _hsl(108, 0.60, 0.33));
      expect(TCColors.green400, _hsl(108, 0.58, 0.40));
      expect(TCColors.green300, _hsl(108, 0.55, 0.50));
      expect(TCColors.green200, _hsl(108, 0.42, 0.64));
      expect(TCColors.green100, _hsl(108, 0.35, 0.84));
    });

    test('amber scale', () {
      expect(TCColors.amber900, _hsl(28, 0.65, 0.10));
      expect(TCColors.amber400, _hsl(28, 1.00, 0.56));
      expect(TCColors.amber100, _hsl(28, 1.00, 0.91));
    });

    test('red scale', () {
      expect(TCColors.red700, _hsl(4, 0.45, 0.20));
      expect(TCColors.red400, _hsl(4, 0.55, 0.42));
    });

    test('ink scale', () {
      expect(TCColors.ink950, _hsl(140, 0.15, 0.05));
      expect(TCColors.ink900, _hsl(140, 0.12, 0.08));
      expect(TCColors.ink850, _hsl(140, 0.11, 0.10));
      expect(TCColors.ink700, _hsl(140, 0.08, 0.19));
      expect(TCColors.ink500, _hsl(140, 0.05, 0.40));
      expect(TCColors.ink300, _hsl(138, 0.06, 0.68));
      expect(TCColors.ink100, _hsl(110, 0.10, 0.94));
    });

    test('semantic aliases resolve to the right scale step', () {
      expect(TCColors.bgApp, TCColors.ink950);
      expect(TCColors.bgSurface, TCColors.ink900);
      expect(TCColors.bgSurfaceRaised, TCColors.ink850);
      expect(TCColors.bgInset, TCColors.ink800);
      expect(TCColors.bgHover, _hsl(140, 0.10, 0.15));
      expect(TCColors.bgPressed, _hsl(140, 0.10, 0.11));
      expect(TCColors.borderSubtle, TCColors.ink800);
      expect(TCColors.borderDefault, TCColors.ink700);
      expect(TCColors.borderStrong, TCColors.ink600);
      expect(TCColors.borderAccent, TCColors.green600);
      expect(TCColors.textPrimary, TCColors.green200);
      expect(TCColors.textSecondary, TCColors.ink400);
      expect(TCColors.textTertiary, TCColors.ink600);
      expect(TCColors.accentPrimary, TCColors.green400);
      expect(TCColors.accentPrimaryHover, TCColors.green300);
      expect(TCColors.accentPrimaryActive, TCColors.green500);
      expect(TCColors.accentPrimaryMuted, TCColors.green800);
      expect(TCColors.accentSecondary, TCColors.amber400);
      expect(TCColors.statusOnline, TCColors.green400);
      expect(TCColors.statusOffline, TCColors.ink500);
      expect(TCColors.statusDanger, TCColors.red400);
      expect(TCColors.statusWarn, TCColors.amber400);
    });
  });

  group('typography tokens match tokens/typography.css', () {
    test('font families', () {
      expect(TCType.fontDisplay, 'VT323');
      expect(TCType.fontMono, 'IBM Plex Mono');
    });

    test('type scale', () {
      expect(TCType.textDisplay2xl, 72);
      expect(TCType.textDisplayXl, 56);
      expect(TCType.textDisplayLg, 40);
      expect(TCType.textDisplayMd, 28);
      expect(TCType.textDisplaySm, 22);
      expect(TCType.textBodyLg, 17);
      expect(TCType.textBodyMd, 14);
      expect(TCType.textBodySm, 13);
      expect(TCType.textCaption, 11);
      expect(TCType.textMicro, 10);
    });

    test('leading and tracking', () {
      expect(TCType.leadingTight, 1.15);
      expect(TCType.leadingDisplay, 1.05);
      expect(TCType.leadingBody, 1.55);
      expect(TCType.leadingRelaxed, 1.7);
      expect(TCType.trackingWide, 0.06);
      expect(TCType.trackingWider, 0.12);
      expect(TCType.trackingNormal, 0.0);
    });

    test('weights', () {
      expect(TCType.weightRegular.value, 400);
      expect(TCType.weightMedium.value, 500);
      expect(TCType.weightSemibold.value, 600);
      expect(TCType.weightBold.value, 700);
    });

    test('letterSpacingFor converts em tracking to logical pixels', () {
      expect(TCType.letterSpacingFor(10, 0.06), closeTo(0.6, 1e-9));
      expect(TCType.letterSpacingFor(11, 0.12), closeTo(1.32, 1e-9));
    });
  });

  group('spacing tokens match tokens/spacing.css', () {
    test('space scale', () {
      expect(TCSpace.space1, 4);
      expect(TCSpace.space4, 16);
      expect(TCSpace.space8, 32);
      expect(TCSpace.space24, 96);
    });

    test('radius, notch, border widths', () {
      expect(TCSpace.radiusSm, 2);
      expect(TCSpace.radiusMd, 4);
      expect(TCSpace.radiusLg, 6);
      expect(TCSpace.radiusPill, 999);
      expect(TCSpace.notch, 10);
      expect(TCSpace.notchSm, 6);
      expect(TCSpace.borderHair, 1);
      expect(TCSpace.borderThick, 2);
    });
  });
}
