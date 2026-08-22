// Carrying a fixed design-system color onto a theme.
//
// A few colors are drawn by the design system rather than named by a token:
// the glow layers in effects.dart, the signal meter's "good" tier. Recoloring
// them straight to a theme token would flatten the relationship the design
// encodes -- a glow is brighter than the ink it comes off, and the good tier
// sits between online and warning -- so instead they are moved the same
// distance in HSL that the theme moved the token they belong beside.
//
// A section that leaves that token alone leaves these colors exactly as the
// design system drew them.
import 'package:flutter/widgets.dart';

/// [source] moved by the HSL distance from [from] to [to].
Color tcShiftLike(Color source, {required Color from, required Color to}) {
  final anchor = HSLColor.fromColor(from);
  final moved = HSLColor.fromColor(to);
  final dh = moved.hue - anchor.hue;
  final ds = moved.saturation - anchor.saturation;
  final dl = moved.lightness - anchor.lightness;
  if (dh == 0 && ds == 0 && dl == 0) return source;
  final base = HSLColor.fromColor(source);
  return HSLColor.fromAHSL(
    base.alpha,
    (base.hue + dh) % 360,
    (base.saturation + ds).clamp(0.0, 1.0),
    (base.lightness + dl).clamp(0.0, 1.0),
  ).toColor();
}
