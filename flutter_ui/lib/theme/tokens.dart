// 1:1 port of trenchchat-design-system tokens/{colors,typography,spacing}.css.
// Keep the CSS variable name as the Dart identifier: --green-400 -> green400.
// Do not invent values here; add a token only by porting one from the CSS.
import 'package:flutter/widgets.dart';

Color _hsl(double h, double s, double l, [double a = 1.0]) {
  return HSLColor.fromAHSL(a, h, s, l).toColor();
}

// ---- colors.css ----

class TCColors {
  TCColors._();

  // Greens
  static final Color green950 = _hsl(108, 0.40, 0.05);
  static final Color green900 = _hsl(108, 0.45, 0.09);
  static final Color green800 = _hsl(108, 0.50, 0.15);
  static final Color green700 = _hsl(108, 0.55, 0.21);
  static final Color green600 = _hsl(108, 0.58, 0.26);
  static final Color green500 = _hsl(108, 0.60, 0.33);
  static final Color green400 = _hsl(108, 0.58, 0.40);
  static final Color green300 = _hsl(108, 0.55, 0.50);
  static final Color green200 = _hsl(108, 0.42, 0.64);
  static final Color green100 = _hsl(108, 0.35, 0.84);

  // Ambers
  static final Color amber950 = _hsl(28, 0.60, 0.06);
  static final Color amber900 = _hsl(28, 0.65, 0.10);
  static final Color amber800 = _hsl(28, 0.70, 0.15);
  static final Color amber700 = _hsl(28, 0.80, 0.22);
  static final Color amber600 = _hsl(28, 0.90, 0.34);
  static final Color amber500 = _hsl(28, 1.00, 0.46);
  static final Color amber400 = _hsl(28, 1.00, 0.56);
  static final Color amber300 = _hsl(28, 1.00, 0.68);
  static final Color amber200 = _hsl(28, 1.00, 0.80);
  static final Color amber100 = _hsl(28, 1.00, 0.91);

  // Reds
  static final Color red700 = _hsl(4, 0.45, 0.20);
  static final Color red600 = _hsl(4, 0.50, 0.28);
  static final Color red500 = _hsl(4, 0.55, 0.36);
  static final Color red400 = _hsl(4, 0.55, 0.42);

  // Inks
  static final Color ink950 = _hsl(140, 0.15, 0.05);
  static final Color ink900 = _hsl(140, 0.12, 0.08);
  static final Color ink850 = _hsl(140, 0.11, 0.10);
  static final Color ink800 = _hsl(140, 0.10, 0.13);
  static final Color ink700 = _hsl(140, 0.08, 0.19);
  static final Color ink600 = _hsl(140, 0.06, 0.27);
  static final Color ink500 = _hsl(140, 0.05, 0.40);
  static final Color ink400 = _hsl(140, 0.06, 0.54);
  static final Color ink300 = _hsl(138, 0.06, 0.68);
  static final Color ink200 = _hsl(120, 0.08, 0.83);
  static final Color ink100 = _hsl(110, 0.10, 0.94);

  // Aliases
  static Color get bgApp => ink950;
  static Color get bgSurface => ink900;
  static Color get bgSurfaceRaised => ink850;
  static Color get bgInset => ink800;
  static final Color bgHover = _hsl(140, 0.10, 0.15);
  static final Color bgPressed = _hsl(140, 0.10, 0.11);

  static Color get borderSubtle => ink800;
  static Color get borderDefault => ink700;
  static Color get borderStrong => ink600;
  static Color get borderAccent => green600;

  static Color get textPrimary => green200;
  static Color get textSecondary => ink400;
  static Color get textTertiary => ink600;
  static Color get textDisabled => ink700;
  static Color get textOnAccent => ink950;
  static Color get textInverse => ink950;

  static Color get accentPrimary => green400;
  static Color get accentPrimaryHover => green300;
  static Color get accentPrimaryActive => green500;
  static Color get accentPrimaryMuted => green800;

  static Color get accentSecondary => amber400;
  static Color get accentSecondaryHover => amber300;
  static Color get accentSecondaryMuted => amber800;

  static Color get statusOnline => green400;
  static Color get statusOffline => ink500;
  static Color get statusDanger => red400;
  static Color get statusDangerMuted => red700;
  static Color get statusWarn => amber400;

  static Color get linkCurrentDomainColor => green300;
  static Color get linkColor => green300;
  static Color get linkHoverColor => green200;
}

// ---- typography.css ----

class TCType {
  TCType._();

  static const String fontDisplay = 'VT323';
  static const String fontMono = 'IBM Plex Mono';

  static const double textDisplay2xl = 72;
  static const double textDisplayXl = 56;
  static const double textDisplayLg = 40;
  static const double textDisplayMd = 28;
  static const double textDisplaySm = 22;

  static const double textBodyLg = 17;
  static const double textBodyMd = 14;
  static const double textBodySm = 13;
  static const double textCaption = 11;
  static const double textMicro = 10;

  static const double leadingTight = 1.15;
  static const double leadingDisplay = 1.05;
  static const double leadingBody = 1.55;
  static const double leadingRelaxed = 1.7;

  static const double trackingWide = 0.06;
  static const double trackingWider = 0.12;
  static const double trackingNormal = 0.0;

  static const FontWeight weightRegular = FontWeight.w400;
  static const FontWeight weightMedium = FontWeight.w500;
  static const FontWeight weightSemibold = FontWeight.w600;
  static const FontWeight weightBold = FontWeight.w700;

  /// --tracking-wide / --tracking-wider are `em` units: multiply by the
  /// style's own font size to get a letterSpacing value in logical pixels.
  static double letterSpacingFor(double fontSize, double trackingEm) => fontSize * trackingEm;
}

// ---- spacing.css ----

class TCSpace {
  TCSpace._();

  static const double space0 = 0;
  static const double space1 = 4;
  static const double space2 = 8;
  static const double space3 = 12;
  static const double space4 = 16;
  static const double space5 = 20;
  static const double space6 = 24;
  static const double space8 = 32;
  static const double space10 = 40;
  static const double space12 = 48;
  static const double space16 = 64;
  static const double space20 = 80;
  static const double space24 = 96;

  static const double radiusNone = 0;
  static const double radiusSm = 2;
  static const double radiusMd = 4;
  static const double radiusLg = 6;
  static const double radiusPill = 999;

  static const double notch = 10;
  static const double notchSm = 6;

  static const double borderHair = 1;
  static const double borderThick = 2;
}
