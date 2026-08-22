// Per-section theming: the parsed form of the theme document the backend
// stores verbatim (GET/POST /ui_theme) and interprets not at all.
//
// Wire shape:
//   {
//     "version": 1,
//     "base":     {"<tokenKey>": "#RRGGBB" | "#AARRGGBB"},
//     "sections": {"<sectionId>": {"<tokenKey>": "..."}},
//     "styles":   {"base": {"<styleKey>": value},
//                  "<sectionId>": {"<styleKey>": value}}
//   }
//
// Colors and styles are two parallel layers over the same scopes: `base`
// plus one entry per section, the section winning where both set a key.
//
// Parsing is total: any garbage -- a wrong type, an unknown token or style
// key, a malformed color, an unknown font name, an unknown section id's
// *tokens* -- is dropped rather than thrown, and a text scale out of range
// is clamped, so a document written by a newer client still loads. Unknown
// *section ids* are preserved through parse -> serialize, in both layers,
// so a section added later survives a round trip through this client
// without needing an entry in [TCSection].
import 'package:flutter/widgets.dart';

import 'tokens.dart';

/// The version this client writes. Documents carrying any other version are
/// still parsed field-by-field; there is nothing to migrate yet.
const int themeSpecVersion = 1;

/// What each token key is called in the editor. Display only -- the wire key
/// stays the key everywhere else, so renaming a label changes no document.
/// One entry per [TCSectionColors.tokenKeys].
const Map<String, String> tokenLabels = {
  'bgApp': 'App background',
  'bgSurface': 'Panel background',
  'bgSurfaceRaised': 'Raised panel background',
  'bgInset': 'Inset background',
  'bgHover': 'Hover highlight',
  'bgPressed': 'Pressed highlight',
  'bgSelected': 'Selection background',
  'borderSubtle': 'Subtle border',
  'borderDefault': 'Border',
  'borderStrong': 'Strong border',
  'borderAccent': 'Accent border',
  'textPrimary': 'Text',
  'textEmphasis': 'Emphasized text',
  'textSecondary': 'Secondary text',
  'textTertiary': 'Faint text',
  'textDisabled': 'Disabled text',
  'textOnAccent': 'Text on accent',
  'textInverse': 'Inverted text',
  'accentPrimary': 'Accent',
  'accentPrimaryHover': 'Accent (hover)',
  'accentPrimaryActive': 'Accent (pressed)',
  'accentPrimaryMuted': 'Accent (muted)',
  'accentSecondary': 'Secondary accent',
  'accentSecondaryHover': 'Secondary accent (hover)',
  'accentSecondaryMuted': 'Secondary accent (muted)',
  'statusOnline': 'Online status',
  'statusOffline': 'Offline status',
  'statusDanger': 'Danger',
  'statusDangerMuted': 'Danger (muted)',
  'statusWarn': 'Warning',
  'linkColor': 'Link',
  'linkHoverColor': 'Link (hover)',
};

/// A themeable region of the UI. The enum name is the wire id.
enum TCSection {
  serverRail,
  channelList,
  presence,
  topBar,
  content,
  dialogs;

  /// The id used as a key in the theme document's `sections` map.
  String get wireId => name;

  /// The section with this wire id, or null when it names one this client
  /// does not know.
  static TCSection? fromWireId(String id) {
    for (final s in TCSection.values) {
      if (s.name == id) return s;
    }
    return null;
  }
}

/// Parses `#RRGGBB` / `#AARRGGBB` (the leading `#` optional, case
/// insensitive). Returns null for anything else.
Color? parseThemeColor(Object? raw) {
  if (raw is! String) return null;
  var hex = raw.trim();
  if (hex.startsWith('#')) hex = hex.substring(1);
  if (hex.length != 6 && hex.length != 8) return null;
  final value = int.tryParse(hex, radix: 16);
  if (value == null) return null;
  return Color(hex.length == 6 ? 0xFF000000 | value : value);
}

/// Writes lowercase `#rrggbb`, or `#aarrggbb` when the color is not opaque.
String encodeThemeColor(Color color) {
  final argb = color.toARGB32();
  final rgb = (argb & 0xFFFFFF).toRadixString(16).padLeft(6, '0');
  final alpha = (argb >> 24) & 0xFF;
  if (alpha == 0xFF) return '#$rgb';
  return '#${alpha.toRadixString(16).padLeft(2, '0')}$rgb';
}

/// The full color palette one section renders with: every semantic alias on
/// [TCColors], resolved for that section.
///
/// The unnamed constructor defaults every field to its stock [TCColors]
/// value, so `TCSectionColors()` is the stock palette and passing only the
/// fields a theme overrides is enough.
class TCSectionColors {
  TCSectionColors({
    Color? bgApp,
    Color? bgSurface,
    Color? bgSurfaceRaised,
    Color? bgInset,
    Color? bgHover,
    Color? bgPressed,
    Color? bgSelected,
    Color? borderSubtle,
    Color? borderDefault,
    Color? borderStrong,
    Color? borderAccent,
    Color? textPrimary,
    Color? textEmphasis,
    Color? textSecondary,
    Color? textTertiary,
    Color? textDisabled,
    Color? textOnAccent,
    Color? textInverse,
    Color? accentPrimary,
    Color? accentPrimaryHover,
    Color? accentPrimaryActive,
    Color? accentPrimaryMuted,
    Color? accentSecondary,
    Color? accentSecondaryHover,
    Color? accentSecondaryMuted,
    Color? statusOnline,
    Color? statusOffline,
    Color? statusDanger,
    Color? statusDangerMuted,
    Color? statusWarn,
    Color? linkColor,
    Color? linkHoverColor,
  })  : bgApp = bgApp ?? TCColors.bgApp,
        bgSurface = bgSurface ?? TCColors.bgSurface,
        bgSurfaceRaised = bgSurfaceRaised ?? TCColors.bgSurfaceRaised,
        bgInset = bgInset ?? TCColors.bgInset,
        bgHover = bgHover ?? TCColors.bgHover,
        bgPressed = bgPressed ?? TCColors.bgPressed,
        bgSelected = bgSelected ?? TCColors.green900,
        borderSubtle = borderSubtle ?? TCColors.borderSubtle,
        borderDefault = borderDefault ?? TCColors.borderDefault,
        borderStrong = borderStrong ?? TCColors.borderStrong,
        borderAccent = borderAccent ?? TCColors.borderAccent,
        textPrimary = textPrimary ?? TCColors.textPrimary,
        textEmphasis = textEmphasis ?? TCColors.green100,
        textSecondary = textSecondary ?? TCColors.textSecondary,
        textTertiary = textTertiary ?? TCColors.textTertiary,
        textDisabled = textDisabled ?? TCColors.textDisabled,
        textOnAccent = textOnAccent ?? TCColors.textOnAccent,
        textInverse = textInverse ?? TCColors.textInverse,
        accentPrimary = accentPrimary ?? TCColors.accentPrimary,
        accentPrimaryHover = accentPrimaryHover ?? TCColors.accentPrimaryHover,
        accentPrimaryActive = accentPrimaryActive ?? TCColors.accentPrimaryActive,
        accentPrimaryMuted = accentPrimaryMuted ?? TCColors.accentPrimaryMuted,
        accentSecondary = accentSecondary ?? TCColors.accentSecondary,
        accentSecondaryHover = accentSecondaryHover ?? TCColors.accentSecondaryHover,
        accentSecondaryMuted = accentSecondaryMuted ?? TCColors.accentSecondaryMuted,
        statusOnline = statusOnline ?? TCColors.statusOnline,
        statusOffline = statusOffline ?? TCColors.statusOffline,
        statusDanger = statusDanger ?? TCColors.statusDanger,
        statusDangerMuted = statusDangerMuted ?? TCColors.statusDangerMuted,
        statusWarn = statusWarn ?? TCColors.statusWarn,
        linkColor = linkColor ?? TCColors.linkColor,
        linkHoverColor = linkHoverColor ?? TCColors.linkHoverColor;

  /// Builds a palette from a token-key -> color map, ignoring unknown keys.
  factory TCSectionColors.fromOverrides(Map<String, Color> overrides) {
    return TCSectionColors(
      bgApp: overrides['bgApp'],
      bgSurface: overrides['bgSurface'],
      bgSurfaceRaised: overrides['bgSurfaceRaised'],
      bgInset: overrides['bgInset'],
      bgHover: overrides['bgHover'],
      bgPressed: overrides['bgPressed'],
      bgSelected: overrides['bgSelected'],
      borderSubtle: overrides['borderSubtle'],
      borderDefault: overrides['borderDefault'],
      borderStrong: overrides['borderStrong'],
      borderAccent: overrides['borderAccent'],
      textPrimary: overrides['textPrimary'],
      textEmphasis: overrides['textEmphasis'],
      textSecondary: overrides['textSecondary'],
      textTertiary: overrides['textTertiary'],
      textDisabled: overrides['textDisabled'],
      textOnAccent: overrides['textOnAccent'],
      textInverse: overrides['textInverse'],
      accentPrimary: overrides['accentPrimary'],
      accentPrimaryHover: overrides['accentPrimaryHover'],
      accentPrimaryActive: overrides['accentPrimaryActive'],
      accentPrimaryMuted: overrides['accentPrimaryMuted'],
      accentSecondary: overrides['accentSecondary'],
      accentSecondaryHover: overrides['accentSecondaryHover'],
      accentSecondaryMuted: overrides['accentSecondaryMuted'],
      statusOnline: overrides['statusOnline'],
      statusOffline: overrides['statusOffline'],
      statusDanger: overrides['statusDanger'],
      statusDangerMuted: overrides['statusDangerMuted'],
      statusWarn: overrides['statusWarn'],
      linkColor: overrides['linkColor'],
      linkHoverColor: overrides['linkHoverColor'],
    );
  }

  final Color bgApp;
  final Color bgSurface;
  final Color bgSurfaceRaised;
  final Color bgInset;
  final Color bgHover;
  final Color bgPressed;
  final Color bgSelected;

  final Color borderSubtle;
  final Color borderDefault;
  final Color borderStrong;
  final Color borderAccent;

  final Color textPrimary;
  final Color textEmphasis;
  final Color textSecondary;
  final Color textTertiary;
  final Color textDisabled;
  final Color textOnAccent;
  final Color textInverse;

  final Color accentPrimary;
  final Color accentPrimaryHover;
  final Color accentPrimaryActive;
  final Color accentPrimaryMuted;

  final Color accentSecondary;
  final Color accentSecondaryHover;
  final Color accentSecondaryMuted;

  final Color statusOnline;
  final Color statusOffline;
  final Color statusDanger;
  final Color statusDangerMuted;
  final Color statusWarn;

  final Color linkColor;
  final Color linkHoverColor;

  /// The stock palette: every token at its [TCColors] value.
  static final TCSectionColors stock = TCSectionColors();

  /// Every token key a theme document may carry, in declaration order.
  static const List<String> tokenKeys = [
    'bgApp',
    'bgSurface',
    'bgSurfaceRaised',
    'bgInset',
    'bgHover',
    'bgPressed',
    'bgSelected',
    'borderSubtle',
    'borderDefault',
    'borderStrong',
    'borderAccent',
    'textPrimary',
    'textEmphasis',
    'textSecondary',
    'textTertiary',
    'textDisabled',
    'textOnAccent',
    'textInverse',
    'accentPrimary',
    'accentPrimaryHover',
    'accentPrimaryActive',
    'accentPrimaryMuted',
    'accentSecondary',
    'accentSecondaryHover',
    'accentSecondaryMuted',
    'statusOnline',
    'statusOffline',
    'statusDanger',
    'statusDangerMuted',
    'statusWarn',
    'linkColor',
    'linkHoverColor',
  ];

  /// The color for [tokenKey], or null when the key is not a known token.
  Color? byKey(String tokenKey) => asMap()[tokenKey];

  /// This palette as a token-key -> color map, one entry per [tokenKeys].
  Map<String, Color> asMap() => {
        'bgApp': bgApp,
        'bgSurface': bgSurface,
        'bgSurfaceRaised': bgSurfaceRaised,
        'bgInset': bgInset,
        'bgHover': bgHover,
        'bgPressed': bgPressed,
        'bgSelected': bgSelected,
        'borderSubtle': borderSubtle,
        'borderDefault': borderDefault,
        'borderStrong': borderStrong,
        'borderAccent': borderAccent,
        'textPrimary': textPrimary,
        'textEmphasis': textEmphasis,
        'textSecondary': textSecondary,
        'textTertiary': textTertiary,
        'textDisabled': textDisabled,
        'textOnAccent': textOnAccent,
        'textInverse': textInverse,
        'accentPrimary': accentPrimary,
        'accentPrimaryHover': accentPrimaryHover,
        'accentPrimaryActive': accentPrimaryActive,
        'accentPrimaryMuted': accentPrimaryMuted,
        'accentSecondary': accentSecondary,
        'accentSecondaryHover': accentSecondaryHover,
        'accentSecondaryMuted': accentSecondaryMuted,
        'statusOnline': statusOnline,
        'statusOffline': statusOffline,
        'statusDanger': statusDanger,
        'statusDangerMuted': statusDangerMuted,
        'statusWarn': statusWarn,
        'linkColor': linkColor,
        'linkHoverColor': linkHoverColor,
      };

  /// A copy with the given tokens replaced.
  TCSectionColors copyWithTokens(Map<String, Color> overrides) =>
      TCSectionColors.fromOverrides({...asMap(), ...overrides});

  @override
  bool operator ==(Object other) {
    if (identical(this, other)) return true;
    if (other is! TCSectionColors) return false;
    final mine = asMap();
    final theirs = other.asMap();
    for (final key in tokenKeys) {
      if (mine[key] != theirs[key]) return false;
    }
    return true;
  }

  @override
  int get hashCode => Object.hashAll([for (final key in tokenKeys) asMap()[key]]);
}

/// The non-color half of one section's look: how large its text renders,
/// whether it glows, which display face its headings use, and what shape
/// its panels, controls and avatars are cut to.
///
/// The unnamed constructor defaults every field, so `TCSectionStyle()` is
/// the stock style and passing only the fields a theme overrides is enough.
/// A text scale or corner radius out of range is clamped; any other unusable
/// value -- an unknown font name, a scale that is not a number -- falls back
/// to the default.
class TCSectionStyle {
  TCSectionStyle({
    double? textScale,
    bool? glow,
    String? displayFont,
    double? cornerRadius,
    String? avatarShape,
    String? panelEdge,
  })  : textScale = (normalizeStyleValue(keyTextScale, textScale) as double?) ?? defaultTextScale,
        glow = (normalizeStyleValue(keyGlow, glow) as bool?) ?? defaultGlow,
        displayFont =
            (normalizeStyleValue(keyDisplayFont, displayFont) as String?) ?? defaultDisplayFont,
        cornerRadius =
            (normalizeStyleValue(keyCornerRadius, cornerRadius) as double?) ?? defaultCornerRadius,
        avatarShape =
            (normalizeStyleValue(keyAvatarShape, avatarShape) as String?) ?? defaultAvatarShape,
        panelEdge =
            (normalizeStyleValue(keyPanelEdge, panelEdge) as String?) ?? defaultPanelEdge;

  /// Builds a style from a style-key -> value map, ignoring unknown keys.
  factory TCSectionStyle.fromOverrides(Map<String, Object> overrides) {
    final scale = overrides[keyTextScale];
    final glow = overrides[keyGlow];
    final font = overrides[keyDisplayFont];
    final radius = overrides[keyCornerRadius];
    final avatar = overrides[keyAvatarShape];
    final edge = overrides[keyPanelEdge];
    return TCSectionStyle(
      textScale: scale is num ? scale.toDouble() : null,
      glow: glow is bool ? glow : null,
      displayFont: font is String ? font : null,
      cornerRadius: radius is num ? radius.toDouble() : null,
      avatarShape: avatar is String ? avatar : null,
      panelEdge: edge is String ? edge : null,
    );
  }

  /// Multiplies the text scale the platform already asks for.
  final double textScale;

  /// Whether accent glow renders at all in this section.
  final bool glow;

  /// The family headings render in, one of [displayFonts].
  final String displayFont;

  /// How far panels, controls and rows round their corners, in logical
  /// pixels. Zero is the stock hard corner, and every widget reading this
  /// treats zero as "leave my stock shape alone" -- see theme/shape.dart.
  final double cornerRadius;

  /// The shape avatars and server tiles are cut to, one of [avatarShapes].
  final String avatarShape;

  /// How an emphasis panel's outline is cut, one of [panelEdges]: the
  /// angular notch, or a plain rectangle rounded by [cornerRadius].
  final String panelEdge;

  static const String keyTextScale = 'textScale';
  static const String keyGlow = 'glow';
  static const String keyDisplayFont = 'displayFont';
  static const String keyCornerRadius = 'cornerRadius';
  static const String keyAvatarShape = 'avatarShape';
  static const String keyPanelEdge = 'panelEdge';

  /// Every style key a theme document may carry, in declaration order.
  static const List<String> styleKeys = [
    keyTextScale,
    keyGlow,
    keyDisplayFont,
    keyCornerRadius,
    keyAvatarShape,
    keyPanelEdge,
  ];

  static const double minTextScale = 0.7;
  static const double maxTextScale = 1.5;
  static const double defaultTextScale = 1.0;
  static const bool defaultGlow = true;
  static const String defaultDisplayFont = TCType.fontDisplay;

  static const double minCornerRadius = 0.0;
  static const double maxCornerRadius = 24.0;
  static const double defaultCornerRadius = 0.0;

  static const String avatarSquare = 'square';
  static const String avatarRounded = 'rounded';
  static const String avatarCircle = 'circle';
  static const String defaultAvatarShape = avatarSquare;

  static const String panelNotch = 'notch';
  static const String panelPlain = 'plain';
  static const String defaultPanelEdge = panelNotch;

  /// The families bundled with the app, in the order the editor offers them.
  static const List<String> displayFonts = [TCType.fontDisplay, TCType.fontMono];

  /// The avatar shapes, in the order the editor offers them.
  static const List<String> avatarShapes = [avatarSquare, avatarRounded, avatarCircle];

  /// The panel edges, in the order the editor offers them.
  static const List<String> panelEdges = [panelNotch, panelPlain];

  /// The stock style: every key at its default.
  static final TCSectionStyle stock = TCSectionStyle();

  /// The storable form of [raw] for [styleKey] -- a clamped double, a bool,
  /// a known font name -- or null when the key is unknown or the value is
  /// not usable. This is the one place a style value is validated.
  static Object? normalizeStyleValue(String styleKey, Object? raw) {
    switch (styleKey) {
      case keyTextScale:
        if (raw is! num) return null;
        final scale = raw.toDouble();
        if (!scale.isFinite) return null;
        return scale.clamp(minTextScale, maxTextScale).toDouble();
      case keyGlow:
        return raw is bool ? raw : null;
      case keyDisplayFont:
        return raw is String && displayFonts.contains(raw) ? raw : null;
      case keyCornerRadius:
        if (raw is! num) return null;
        final radius = raw.toDouble();
        if (!radius.isFinite) return null;
        return radius.clamp(minCornerRadius, maxCornerRadius).toDouble();
      case keyAvatarShape:
        return raw is String && avatarShapes.contains(raw) ? raw : null;
      case keyPanelEdge:
        return raw is String && panelEdges.contains(raw) ? raw : null;
    }
    return null;
  }

  /// The value for [styleKey], or null when the key is not a known style.
  Object? byKey(String styleKey) => asMap()[styleKey];

  /// This style as a style-key -> value map, one entry per [styleKeys].
  Map<String, Object> asMap() => {
        keyTextScale: textScale,
        keyGlow: glow,
        keyDisplayFont: displayFont,
        keyCornerRadius: cornerRadius,
        keyAvatarShape: avatarShape,
        keyPanelEdge: panelEdge,
      };

  @override
  bool operator ==(Object other) =>
      identical(this, other) ||
      other is TCSectionStyle &&
          other.textScale == textScale &&
          other.glow == glow &&
          other.displayFont == displayFont &&
          other.cornerRadius == cornerRadius &&
          other.avatarShape == avatarShape &&
          other.panelEdge == panelEdge;

  @override
  int get hashCode =>
      Object.hash(textScale, glow, displayFont, cornerRadius, avatarShape, panelEdge);

  @override
  String toString() => 'TCSectionStyle(textScale: $textScale, glow: $glow, '
      'displayFont: $displayFont, cornerRadius: $cornerRadius, '
      'avatarShape: $avatarShape, panelEdge: $panelEdge)';
}

/// A theme document: base overrides plus per-section overrides, for colors
/// and styles alike.
///
/// Immutable. Build a modified copy with [withBaseOverride],
/// [withSectionOverride], [withBaseOverrides], [withSectionOverrides],
/// [withStyleOverride], [withStyleOverrides], [clearSection] or [clearBase]
/// -- each returns a new spec and leaves this one untouched:
///
/// ```dart
/// final next = state.themeSpec
///     .withBaseOverride('accentPrimary', const Color(0xFF00FF88))
///     .withSectionOverride(TCSection.topBar, 'bgSurface', null) // clears it
///     .withStyleOverride(null, TCSectionStyle.keyTextScale, 1.1) // base scope
///     .withStyleOverride(TCSection.content, TCSectionStyle.keyGlow, false);
/// await state.saveTheme(next);
/// ```
///
/// The style editors take a nullable [TCSection]: null is the base scope,
/// and a null value clears that key. [clearSection] and [clearBase] drop a
/// scope's styles along with its colors.
class ThemeSpec {
  ThemeSpec({
    Map<String, Color>? base,
    Map<String, Map<String, Color>>? sections,
    Map<String, Map<String, Object>>? styles,
  })  : base = Map.unmodifiable(_pruneTokens(base ?? const {})),
        sections = Map.unmodifiable({
          for (final entry in (sections ?? const {}).entries)
            if (entry.value.isNotEmpty)
              entry.key: Map<String, Color>.unmodifiable(_pruneTokens(entry.value)),
        }),
        styles = Map.unmodifiable(_pruneStyleScopes(styles ?? const {}));

  /// Base overrides, applied to every section. Token keys only.
  final Map<String, Color> base;

  /// Per-section overrides keyed by section wire id. Ids this client does
  /// not know are kept so they survive a round trip.
  final Map<String, Map<String, Color>> sections;

  /// Style overrides keyed by [baseStyleScope] or a section wire id, the
  /// same preservation rule as [sections].
  final Map<String, Map<String, Object>> styles;

  /// The key the base scope's style overrides live under in [styles].
  static const String baseStyleScope = 'base';

  /// A spec with no overrides at all: every section renders stock.
  static final ThemeSpec empty = ThemeSpec();

  static Map<String, Color> _pruneTokens(Map<String, Color> input) => {
        for (final entry in input.entries)
          if (TCSectionColors.tokenKeys.contains(entry.key)) entry.key: entry.value,
      };

  static Map<String, Object> _pruneStyles(Map<String, Object> input) {
    final out = <String, Object>{};
    for (final entry in input.entries) {
      final value = TCSectionStyle.normalizeStyleValue(entry.key, entry.value);
      if (value != null) out[entry.key] = value;
    }
    return out;
  }

  static Map<String, Map<String, Object>> _pruneStyleScopes(
      Map<String, Map<String, Object>> input) {
    final out = <String, Map<String, Object>>{};
    for (final entry in input.entries) {
      final pruned = _pruneStyles(entry.value);
      if (pruned.isNotEmpty) out[entry.key] = Map<String, Object>.unmodifiable(pruned);
    }
    return out;
  }

  static String _styleScopeKey(TCSection? section) => section?.wireId ?? baseStyleScope;

  /// Parses a theme document. Never throws: anything unrecognized is dropped.
  factory ThemeSpec.fromJson(Map<String, dynamic> json) {
    final base = _parseTokenMap(json['base']);
    final sections = <String, Map<String, Color>>{};
    final rawSections = json['sections'];
    if (rawSections is Map) {
      for (final entry in rawSections.entries) {
        final id = entry.key;
        if (id is! String) continue;
        final tokens = _parseTokenMap(entry.value);
        if (tokens.isNotEmpty) sections[id] = tokens;
      }
    }
    final styles = <String, Map<String, Object>>{};
    final rawStyles = json['styles'];
    if (rawStyles is Map) {
      for (final entry in rawStyles.entries) {
        final id = entry.key;
        if (id is! String) continue;
        final scope = _parseStyleMap(entry.value);
        if (scope.isNotEmpty) styles[id] = scope;
      }
    }
    return ThemeSpec(base: base, sections: sections, styles: styles);
  }

  static Map<String, Object> _parseStyleMap(Object? raw) {
    final out = <String, Object>{};
    if (raw is! Map) return out;
    for (final entry in raw.entries) {
      final key = entry.key;
      if (key is! String) continue;
      final value = TCSectionStyle.normalizeStyleValue(key, entry.value);
      if (value != null) out[key] = value;
    }
    return out;
  }

  static Map<String, Color> _parseTokenMap(Object? raw) {
    final out = <String, Color>{};
    if (raw is! Map) return out;
    for (final entry in raw.entries) {
      final key = entry.key;
      if (key is! String || !TCSectionColors.tokenKeys.contains(key)) continue;
      final color = parseThemeColor(entry.value);
      if (color != null) out[key] = color;
    }
    return out;
  }

  /// The wire form, ready for POST /ui_theme.
  Map<String, dynamic> toJson() => {
        'version': themeSpecVersion,
        'base': {
          for (final entry in base.entries) entry.key: encodeThemeColor(entry.value),
        },
        'sections': {
          for (final entry in sections.entries)
            entry.key: {
              for (final token in entry.value.entries)
                token.key: encodeThemeColor(token.value),
            },
        },
        'styles': {
          for (final entry in styles.entries) entry.key: {...entry.value},
        },
      };

  /// True when nothing is overridden, so every section renders stock.
  bool get isEmpty =>
      base.isEmpty &&
      sections.values.every((m) => m.isEmpty) &&
      styles.values.every((m) => m.isEmpty);

  bool get isNotEmpty => !isEmpty;

  /// The overrides in force for [section]: base, then the section's own.
  Map<String, Color> overridesFor(TCSection section) =>
      {...base, ...?sections[section.wireId]};

  /// The palette [section] renders with: stock defaults, then base
  /// overrides, then the section's own overrides.
  TCSectionColors resolve(TCSection section) =>
      TCSectionColors.fromOverrides(overridesFor(section));

  /// The palette with base overrides only -- for chrome that belongs to no
  /// single section (the scaffold and drawer backgrounds, a fatal error).
  TCSectionColors resolveBase() => TCSectionColors.fromOverrides(base);

  /// The style overrides [section] sets itself, or the base ones when
  /// [section] is null. Empty when that scope overrides nothing.
  Map<String, Object> styleOverridesFor(TCSection? section) =>
      styles[_styleScopeKey(section)] ?? const {};

  /// The style overrides in force for [section]: base, then its own.
  Map<String, Object> resolvedStyleOverridesFor(TCSection section) =>
      {...?styles[baseStyleScope], ...?styles[section.wireId]};

  /// The style [section] renders with: defaults, then base overrides, then
  /// the section's own.
  TCSectionStyle resolveStyle(TCSection section) =>
      TCSectionStyle.fromOverrides(resolvedStyleOverridesFor(section));

  /// The style with base overrides only, the [resolveBase] of the style
  /// layer.
  TCSectionStyle resolveBaseStyle() =>
      TCSectionStyle.fromOverrides(styles[baseStyleScope] ?? const {});

  /// A copy with one base token set, or cleared when [color] is null.
  ThemeSpec withBaseOverride(String tokenKey, Color? color) =>
      withBaseOverrides({tokenKey: color});

  /// A copy with several base tokens set; a null value clears that token.
  ThemeSpec withBaseOverrides(Map<String, Color?> overrides) =>
      ThemeSpec(base: _merge(base, overrides), sections: sections, styles: styles);

  /// A copy with one style key of [section] set -- base scope when [section]
  /// is null, cleared when [value] is null. A value the style layer cannot
  /// store (an unknown font, a non-number scale) clears the key too; one out
  /// of range is clamped.
  ThemeSpec withStyleOverride(TCSection? section, String styleKey, Object? value) =>
      withStyleOverrides(section, {styleKey: value});

  /// A copy with several style keys of one scope set; a null value clears
  /// one. [section] null means the base scope.
  ThemeSpec withStyleOverrides(TCSection? section, Map<String, Object?> overrides) {
    final scopeKey = _styleScopeKey(section);
    final merged = Map<String, Object>.from(styles[scopeKey] ?? const {});
    for (final entry in overrides.entries) {
      final value = entry.value;
      if (value == null) {
        merged.remove(entry.key);
      } else {
        merged[entry.key] = value;
      }
    }
    final next = Map<String, Map<String, Object>>.from(styles);
    if (_pruneStyles(merged).isEmpty) {
      next.remove(scopeKey);
    } else {
      next[scopeKey] = merged;
    }
    return ThemeSpec(base: base, sections: sections, styles: next);
  }

  /// A copy with one token of [section] set, or cleared when [color] is null.
  ThemeSpec withSectionOverride(TCSection section, String tokenKey, Color? color) =>
      withSectionOverrides(section, {tokenKey: color});

  /// A copy with several tokens of [section] set; a null value clears one.
  ThemeSpec withSectionOverrides(TCSection section, Map<String, Color?> overrides) {
    final next = Map<String, Map<String, Color>>.from(sections);
    final merged = _merge(sections[section.wireId] ?? const {}, overrides);
    if (merged.isEmpty) {
      next.remove(section.wireId);
    } else {
      next[section.wireId] = merged;
    }
    return ThemeSpec(base: base, sections: next, styles: styles);
  }

  /// A copy with every override for [section] removed, colors and styles.
  ThemeSpec clearSection(TCSection section) {
    final next = Map<String, Map<String, Color>>.from(sections)..remove(section.wireId);
    final nextStyles = Map<String, Map<String, Object>>.from(styles)..remove(section.wireId);
    return ThemeSpec(base: base, sections: next, styles: nextStyles);
  }

  /// A copy with every base override removed, colors and styles.
  ThemeSpec clearBase() {
    final nextStyles = Map<String, Map<String, Object>>.from(styles)..remove(baseStyleScope);
    return ThemeSpec(sections: sections, styles: nextStyles);
  }

  static Map<String, Color> _merge(Map<String, Color> current, Map<String, Color?> changes) {
    final next = Map<String, Color>.from(current);
    for (final entry in changes.entries) {
      final color = entry.value;
      if (color == null) {
        next.remove(entry.key);
      } else {
        next[entry.key] = color;
      }
    }
    return next;
  }

  @override
  bool operator ==(Object other) {
    if (identical(this, other)) return true;
    if (other is! ThemeSpec) return false;
    if (!_sameTokens(base, other.base)) return false;
    if (sections.length != other.sections.length) return false;
    for (final entry in sections.entries) {
      final theirs = other.sections[entry.key];
      if (theirs == null || !_sameTokens(entry.value, theirs)) return false;
    }
    if (styles.length != other.styles.length) return false;
    for (final entry in styles.entries) {
      final theirs = other.styles[entry.key];
      if (theirs == null || !_sameStyles(entry.value, theirs)) return false;
    }
    return true;
  }

  static bool _sameTokens(Map<String, Color> a, Map<String, Color> b) {
    if (a.length != b.length) return false;
    for (final entry in a.entries) {
      if (b[entry.key] != entry.value) return false;
    }
    return true;
  }

  static bool _sameStyles(Map<String, Object> a, Map<String, Object> b) {
    if (a.length != b.length) return false;
    for (final entry in a.entries) {
      if (b[entry.key] != entry.value) return false;
    }
    return true;
  }

  @override
  int get hashCode => Object.hash(
        Object.hashAllUnordered([for (final e in base.entries) Object.hash(e.key, e.value)]),
        Object.hashAllUnordered([
          for (final section in sections.entries)
            Object.hash(
              section.key,
              Object.hashAllUnordered(
                  [for (final e in section.value.entries) Object.hash(e.key, e.value)]),
            ),
        ]),
        Object.hashAllUnordered([
          for (final scope in styles.entries)
            Object.hash(
              scope.key,
              Object.hashAllUnordered(
                  [for (final e in scope.value.entries) Object.hash(e.key, e.value)]),
            ),
        ]),
      );

  @override
  String toString() => 'ThemeSpec(base: ${base.length} tokens, '
      'sections: ${sections.keys.join(', ')}, styles: ${styles.keys.join(', ')})';
}
