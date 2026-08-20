// Hands a resolved per-section palette down the tree. Wrap a layout region
// once, near the top; every widget under it reads its colors with
// `SectionTheme.of(context)` and never touches [TCColors] directly.
//
// With no SectionTheme ancestor -- a widget test pumping a single widget,
// say -- `of` returns the stock palette, so an unwrapped widget renders
// exactly as it did before theming existed.
import 'package:flutter/widgets.dart';

import 'theme_spec.dart';

class SectionTheme extends InheritedWidget {
  /// Resolves [spec] for [section]. This is what call sites use: the live
  /// spec goes in, the section decides what comes out.
  SectionTheme({
    super.key,
    required ThemeSpec spec,
    required this.section,
    required super.child,
  })  : spec = spec,
        colors = spec.resolve(section);

  /// For a palette that is already resolved (a theme editor previewing one
  /// section, a test pinning colors directly).
  const SectionTheme.resolved({
    super.key,
    required this.section,
    required this.colors,
    required super.child,
  }) : spec = null;

  final TCSection section;
  final TCSectionColors colors;

  /// The spec this section was resolved from, or null when it was built from
  /// already-resolved colors.
  final ThemeSpec? spec;

  /// The nearest enclosing section's palette, or the stock palette when
  /// there is no [SectionTheme] above [context].
  static TCSectionColors of(BuildContext context) => maybeOf(context) ?? TCSectionColors.stock;

  /// The nearest enclosing section's palette, or null when there is none.
  static TCSectionColors? maybeOf(BuildContext context) =>
      context.dependOnInheritedWidgetOfExactType<SectionTheme>()?.colors;

  /// Which section [context] sits in, or null when it sits in none.
  static TCSection? sectionOf(BuildContext context) =>
      context.dependOnInheritedWidgetOfExactType<SectionTheme>()?.section;

  /// The live spec behind the nearest section, for a widget that needs to
  /// open a nested section of its own. Null when there is no [SectionTheme]
  /// above [context], or when the one above was given resolved colors --
  /// such a caller falls back to [SectionTheme.resolved] with the palette
  /// [of] already returns.
  static ThemeSpec? specOf(BuildContext context) =>
      context.dependOnInheritedWidgetOfExactType<SectionTheme>()?.spec;

  @override
  bool updateShouldNotify(SectionTheme oldWidget) =>
      colors != oldWidget.colors || section != oldWidget.section || spec != oldWidget.spec;
}
