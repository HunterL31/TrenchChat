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
import 'tokens.dart';

/// Text glow for the section [context] sits in, or null when it has glow
/// off -- assign it straight to `TextStyle.shadows`.
List<Shadow>? tcTextGlow(BuildContext context) {
  if (!SectionTheme.styleOf(context).glow) return null;
  final accent = SectionTheme.of(context).accentPrimary;
  final stock = TCEffects.textGlowGreen;
  return [
    Shadow(
      color: _retint(stock.color, accent),
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
        color: _retint(shadow.color, accent),
        blurRadius: shadow.blurRadius,
        spreadRadius: shadow.spreadRadius,
        offset: shadow.offset,
      ),
  ];
}

/// Moves one stock glow layer the same distance in HSL that [accent] sits
/// from the stock accent, keeping the layer's alpha.
///
/// Recoloring straight to the accent would flatten the relationship the
/// design system encodes -- a glow is brighter and more saturated than the
/// ink it comes off -- so the layers are shifted rather than replaced, and
/// a stock accent leaves them exactly as `effects.dart` defines them.
Color _retint(Color layer, Color accent) {
  final stock = HSLColor.fromColor(TCColors.accentPrimary);
  final tinted = HSLColor.fromColor(accent);
  final dh = tinted.hue - stock.hue;
  final ds = tinted.saturation - stock.saturation;
  final dl = tinted.lightness - stock.lightness;
  if (dh == 0 && ds == 0 && dl == 0) return layer;
  final source = HSLColor.fromColor(layer);
  return HSLColor.fromAHSL(
    source.alpha,
    (source.hue + dh) % 360,
    (source.saturation + ds).clamp(0.0, 1.0),
    (source.lightness + dl).clamp(0.0, 1.0),
  ).toColor();
}
