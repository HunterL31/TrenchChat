import 'package:flutter/material.dart';

import 'tokens.dart';

ThemeData buildAppTheme() {
  final base = ThemeData.dark(useMaterial3: true);
  return base.copyWith(
    scaffoldBackgroundColor: TCColors.bgApp,
    colorScheme: base.colorScheme.copyWith(
      surface: TCColors.bgSurface,
      primary: TCColors.accentPrimary,
      secondary: TCColors.accentSecondary,
      error: TCColors.statusDanger,
    ),
    textTheme: base.textTheme.apply(
      fontFamily: TCType.fontMono,
      bodyColor: TCColors.textPrimary,
      displayColor: TCColors.textPrimary,
    ),
    splashFactory: NoSplash.splashFactory,
    highlightColor: Colors.transparent,
    hoverColor: Colors.transparent,
    dividerColor: TCColors.borderSubtle,
    scrollbarTheme: ScrollbarThemeData(
      thumbColor: WidgetStatePropertyAll(TCColors.borderStrong),
      trackColor: WidgetStatePropertyAll(TCColors.bgSurface),
    ),
  );
}
