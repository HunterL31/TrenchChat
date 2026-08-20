// Per-section theming: the parsed form of the theme document the backend
// stores verbatim (GET/POST /ui_theme) and interprets not at all.
//
// Wire shape:
//   {
//     "version": 1,
//     "base":     {"<tokenKey>": "#RRGGBB" | "#AARRGGBB"},
//     "sections": {"<sectionId>": {"<tokenKey>": "..."}}
//   }
//
// Parsing is total: any garbage -- a wrong type, an unknown token key, a
// malformed color, an unknown section id's *tokens* -- is dropped rather
// than thrown, so a document written by a newer client still loads.
// Unknown *section ids* are preserved through parse -> serialize, so a
// section added later survives a round trip through this client without
// needing an entry in [TCSection].
import 'package:flutter/widgets.dart';

import 'tokens.dart';

/// The version this client writes. Documents carrying any other version are
/// still parsed field-by-field; there is nothing to migrate yet.
const int themeSpecVersion = 1;

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
    Color? borderSubtle,
    Color? borderDefault,
    Color? borderStrong,
    Color? borderAccent,
    Color? textPrimary,
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
        borderSubtle = borderSubtle ?? TCColors.borderSubtle,
        borderDefault = borderDefault ?? TCColors.borderDefault,
        borderStrong = borderStrong ?? TCColors.borderStrong,
        borderAccent = borderAccent ?? TCColors.borderAccent,
        textPrimary = textPrimary ?? TCColors.textPrimary,
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
      borderSubtle: overrides['borderSubtle'],
      borderDefault: overrides['borderDefault'],
      borderStrong: overrides['borderStrong'],
      borderAccent: overrides['borderAccent'],
      textPrimary: overrides['textPrimary'],
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

  final Color borderSubtle;
  final Color borderDefault;
  final Color borderStrong;
  final Color borderAccent;

  final Color textPrimary;
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
    'borderSubtle',
    'borderDefault',
    'borderStrong',
    'borderAccent',
    'textPrimary',
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
        'borderSubtle': borderSubtle,
        'borderDefault': borderDefault,
        'borderStrong': borderStrong,
        'borderAccent': borderAccent,
        'textPrimary': textPrimary,
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

/// A theme document: base overrides plus per-section overrides.
///
/// Immutable. Build a modified copy with [withBaseOverride],
/// [withSectionOverride], [withBaseOverrides], [withSectionOverrides],
/// [clearSection] or [clearBase] -- each returns a new spec and leaves this
/// one untouched:
///
/// ```dart
/// final next = state.themeSpec
///     .withBaseOverride('accentPrimary', const Color(0xFF00FF88))
///     .withSectionOverride(TCSection.topBar, 'bgSurface', null); // clears it
/// await state.saveTheme(next);
/// ```
class ThemeSpec {
  ThemeSpec({
    Map<String, Color>? base,
    Map<String, Map<String, Color>>? sections,
  })  : base = Map.unmodifiable(_pruneTokens(base ?? const {})),
        sections = Map.unmodifiable({
          for (final entry in (sections ?? const {}).entries)
            if (entry.value.isNotEmpty)
              entry.key: Map<String, Color>.unmodifiable(_pruneTokens(entry.value)),
        });

  /// Base overrides, applied to every section. Token keys only.
  final Map<String, Color> base;

  /// Per-section overrides keyed by section wire id. Ids this client does
  /// not know are kept so they survive a round trip.
  final Map<String, Map<String, Color>> sections;

  /// A spec with no overrides at all: every section renders stock.
  static final ThemeSpec empty = ThemeSpec();

  static Map<String, Color> _pruneTokens(Map<String, Color> input) => {
        for (final entry in input.entries)
          if (TCSectionColors.tokenKeys.contains(entry.key)) entry.key: entry.value,
      };

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
    return ThemeSpec(base: base, sections: sections);
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
      };

  /// True when nothing is overridden, so every section renders stock.
  bool get isEmpty => base.isEmpty && sections.values.every((m) => m.isEmpty);

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

  /// A copy with one base token set, or cleared when [color] is null.
  ThemeSpec withBaseOverride(String tokenKey, Color? color) =>
      withBaseOverrides({tokenKey: color});

  /// A copy with several base tokens set; a null value clears that token.
  ThemeSpec withBaseOverrides(Map<String, Color?> overrides) =>
      ThemeSpec(base: _merge(base, overrides), sections: sections);

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
    return ThemeSpec(base: base, sections: next);
  }

  /// A copy with every override for [section] removed.
  ThemeSpec clearSection(TCSection section) {
    final next = Map<String, Map<String, Color>>.from(sections)..remove(section.wireId);
    return ThemeSpec(base: base, sections: next);
  }

  /// A copy with every base override removed.
  ThemeSpec clearBase() => ThemeSpec(sections: sections);

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
    return true;
  }

  static bool _sameTokens(Map<String, Color> a, Map<String, Color> b) {
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
      );

  @override
  String toString() => 'ThemeSpec(base: ${base.length} tokens, '
      'sections: ${sections.keys.join(', ')})';
}
