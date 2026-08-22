// Theme-aware glow. The stock glow shape -- the blur radii and alphas the
// design system defines -- carried onto the enclosing section's accent, and
// absent entirely when that section turns glow off.
//
// Call these instead of reaching for TCEffects.textGlowGreen /
// TCEffects.glowGreenSm at a call site: the effect tokens stay the literal
// port of the CSS, and the theme decides which color wears them.
import 'package:flutter/widgets.dart';

import 'effects.dart';
import 'section_theme.dart';
import 'tint.dart';
import 'tokens.dart';

/// Text glow for the section [context] sits in, or null when it has glow
/// off -- assign it straight to `TextStyle.shadows`.
List<Shadow>? tcTextGlow(BuildContext context) {
  if (!SectionTheme.styleOf(context).glow) return null;
  final accent = SectionTheme.of(context).accentPrimary;
  final stock = TCEffects.textGlowGreen;
  return [
    Shadow(
      color: _accented(stock.color, accent),
      blurRadius: stock.blurRadius,
      offset: stock.offset,
    ),
  ];
}

/// The small box glow for the section [context] sits in, or null when it has
/// glow off -- assign it straight to `BoxDecoration.boxShadow`.
List<BoxShadow>? tcBoxGlowSm(BuildContext context) {
  if (!SectionTheme.styleOf(context).glow) return null;
  final accent = SectionTheme.of(context).accentPrimary;
  return [
    for (final shadow in TCEffects.glowGreenSm)
      BoxShadow(
        color: _accented(shadow.color, accent),
        blurRadius: shadow.blurRadius,
        spreadRadius: shadow.spreadRadius,
        offset: shadow.offset,
      ),
  ];
}

/// One stock glow layer carried onto [accent] -- see tint.dart for why the
/// layer is moved rather than replaced.
Color _accented(Color layer, Color accent) =>
    tcShiftLike(layer, from: TCColors.accentPrimary, to: accent);
