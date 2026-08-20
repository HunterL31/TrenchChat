// Hands a resolved per-section palette and style down the tree. Wrap a
// layout region once, near the top; every widget under it reads its colors
// with `SectionTheme.of(context)` and never touches [TCColors] directly.
//
// The style half needs no widget changes for text size: a section whose
// text scale is not 1.0 wraps its child in a [MediaQuery] whose scaler
// multiplies the one already in force, so the platform's accessibility
// scaling still applies on top.
//
// With no SectionTheme ancestor -- a widget test pumping a single widget,
// say -- `of` returns the stock palette and `styleOf` the stock style, so
// an unwrapped widget renders exactly as it did before theming existed.
import 'package:flutter/widgets.dart';

import 'theme_spec.dart';

class SectionTheme extends InheritedWidget {
  /// Resolves [spec] for [section]. This is what call sites use: the live
  /// spec goes in, the section decides what comes out.
  SectionTheme({
    Key? key,
    required ThemeSpec spec,
    required TCSection section,
    required Widget child,
  }) : this._(
          key: key,
          spec: spec,
          section: section,
          colors: spec.resolve(section),
          style: spec.resolveStyle(section),
          child: child,
        );

  /// For a palette that is already resolved (a theme editor previewing one
  /// section, a test pinning colors directly).
  SectionTheme.resolved({
    Key? key,
    required TCSection section,
    required TCSectionColors colors,
    TCSectionStyle? style,
    required Widget child,
  }) : this._(
          key: key,
          section: section,
          colors: colors,
          style: style ?? TCSectionStyle.stock,
          child: child,
        );

  SectionTheme._({
    super.key,
    this.spec,
    required this.section,
    required this.colors,
    required this.style,
    required Widget child,
  }) : super(child: _scaleText(style, child));

  final TCSection section;
  final TCSectionColors colors;

  /// The section's non-color look: text scale, glow, display font.
  final TCSectionStyle style;

  /// The spec this section was resolved from, or null when it was built from
  /// already-resolved colors.
  final ThemeSpec? spec;

  /// The nearest enclosing section's palette, or the stock palette when
  /// there is no [SectionTheme] above [context].
  static TCSectionColors of(BuildContext context) => maybeOf(context) ?? TCSectionColors.stock;

  /// The nearest enclosing section's palette, or null when there is none.
  static TCSectionColors? maybeOf(BuildContext context) =>
      context.dependOnInheritedWidgetOfExactType<SectionTheme>()?.colors;

  /// The nearest enclosing section's style, or the stock style when there is
  /// no [SectionTheme] above [context].
  static TCSectionStyle styleOf(BuildContext context) =>
      context.dependOnInheritedWidgetOfExactType<SectionTheme>()?.style ?? TCSectionStyle.stock;

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

  /// Retunes the inherited text scaler so this section renders at its own
  /// scale times whatever the platform asks for. Sections nest, so an
  /// enclosing section's factor is divided back out before this one's is
  /// applied -- otherwise a top bar inside a scaled content region would
  /// compound the two.
  static Widget _scaleText(TCSectionStyle style, Widget child) {
    return Builder(builder: (context) {
      final applied = _SectionTextScale.maybeOf(context) ?? TCSectionStyle.defaultTextScale;
      if (style.textScale == applied) return child;
      final inherited = MediaQuery.maybeOf(context);
      final current = (inherited?.textScaler ?? TextScaler.noScaling).scale(1);
      final scaler = TextScaler.linear(current * style.textScale / applied);
      return _SectionTextScale(
        scale: style.textScale,
        child: MediaQuery(
          data: (inherited ?? const MediaQueryData()).copyWith(textScaler: scaler),
          child: child,
        ),
      );
    });
  }

  @override
  bool updateShouldNotify(SectionTheme oldWidget) =>
      colors != oldWidget.colors ||
      style != oldWidget.style ||
      section != oldWidget.section ||
      spec != oldWidget.spec;
}

/// The text scale the enclosing section already folded into the MediaQuery,
/// so a nested section can undo it before applying its own.
class _SectionTextScale extends InheritedWidget {
  const _SectionTextScale({required this.scale, required super.child});

  final double scale;

  static double? maybeOf(BuildContext context) =>
      context.dependOnInheritedWidgetOfExactType<_SectionTextScale>()?.scale;

  @override
  bool updateShouldNotify(_SectionTextScale oldWidget) => scale != oldWidget.scale;
}
