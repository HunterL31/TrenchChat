// Built-in theme presets: complete base-level looks a user can apply with
// one click, then refine per section in the appearance editor.
import 'package:flutter/widgets.dart';

import 'theme_spec.dart';
import 'tokens.dart';

Color _hsl(double h, double s, double l) => HSLColor.fromAHSL(1, h, s, l).toColor();

/// A named, ready-made [ThemeSpec]. Applying one replaces the whole spec.
class ThemePreset {
  const ThemePreset({required this.name, required this.spec});

  final String name;
  final ThemeSpec spec;
}

/// "Ember": the stock layout on a warm amber phosphor palette. The cool
/// green-tinted inks become warm browns and the green/amber accent pair
/// swaps roles; status and danger colors keep their stock meanings.
final ThemeSpec _ember = ThemeSpec(base: {
  'bgApp': _hsl(30, 0.15, 0.05),
  'bgSurface': _hsl(30, 0.12, 0.08),
  'bgSurfaceRaised': _hsl(30, 0.11, 0.10),
  'bgInset': _hsl(30, 0.10, 0.13),
  'bgHover': _hsl(30, 0.10, 0.15),
  'bgPressed': _hsl(30, 0.10, 0.11),
  'borderSubtle': _hsl(30, 0.10, 0.13),
  'borderDefault': _hsl(30, 0.08, 0.19),
  'borderStrong': _hsl(30, 0.06, 0.27),
  'borderAccent': TCColors.amber600,
  'textPrimary': TCColors.amber200,
  'textSecondary': _hsl(30, 0.06, 0.54),
  'textTertiary': _hsl(30, 0.06, 0.32),
  'textDisabled': _hsl(30, 0.08, 0.19),
  'textOnAccent': _hsl(30, 0.15, 0.05),
  'textInverse': _hsl(30, 0.15, 0.05),
  'accentPrimary': TCColors.amber400,
  'accentPrimaryHover': TCColors.amber300,
  'accentPrimaryActive': TCColors.amber500,
  'accentPrimaryMuted': TCColors.amber800,
  'accentSecondary': TCColors.green400,
  'accentSecondaryHover': TCColors.green300,
  'accentSecondaryMuted': TCColors.green800,
  'statusOffline': _hsl(30, 0.05, 0.40),
  'linkColor': TCColors.amber300,
  'linkHoverColor': TCColors.amber200,
});

/// Presets offered by the appearance editor, stock look first.
final List<ThemePreset> themePresets = [
  ThemePreset(name: 'Trench', spec: ThemeSpec.empty),
  ThemePreset(name: 'Ember', spec: _ember),
];
