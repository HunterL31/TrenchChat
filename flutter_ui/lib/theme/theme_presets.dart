// Built-in theme presets: complete looks a user can apply with one click,
// then refine per section in the appearance editor.
//
// Ember is the stock layout on a different phosphor. The rest are the
// familiar chat clients: each sets its whole base palette, overrides the
// sections that carry a different surface in that client, and picks the
// shape styles -- corner radius, avatar cut, panel edge -- that make the
// layout read as that app rather than as a terminal.
import 'package:flutter/widgets.dart';

import 'theme_spec.dart';
import 'tokens.dart';

Color _hsl(double h, double s, double l) => HSLColor.fromAHSL(1, h, s, l).toColor();

Color _rgb(int value) => Color(0xFF000000 | value);

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
  'bgSelected': TCColors.amber900,
  'borderSubtle': _hsl(30, 0.10, 0.13),
  'borderDefault': _hsl(30, 0.08, 0.19),
  'borderStrong': _hsl(30, 0.06, 0.27),
  'borderAccent': TCColors.amber600,
  'textPrimary': TCColors.amber200,
  'textEmphasis': TCColors.amber100,
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

/// The style block every client-lookalike shares: no phosphor glow, and
/// headings in the body face rather than the pixel display face.
const Map<String, Object> _modernBaseStyle = {
  TCSectionStyle.keyGlow: false,
  TCSectionStyle.keyDisplayFont: TCType.fontMono,
  TCSectionStyle.keyPanelEdge: TCSectionStyle.panelPlain,
};

/// "Discord": the dark blurple look -- near-black server rail, dark grey
/// channel sidebar, lighter chat surface, and round everything.
final ThemeSpec _discord = ThemeSpec(
  base: {
    'bgApp': _rgb(0x313338),
    'bgSurface': _rgb(0x2B2D31),
    'bgSurfaceRaised': _rgb(0x313338),
    'bgInset': _rgb(0x383A40),
    'bgHover': _rgb(0x35373C),
    'bgPressed': _rgb(0x2E3035),
    'bgSelected': _rgb(0x404249),
    'borderSubtle': _rgb(0x2B2D31),
    'borderDefault': _rgb(0x3F4147),
    'borderStrong': _rgb(0x4E5058),
    'borderAccent': _rgb(0x5865F2),
    'textPrimary': _rgb(0xDBDEE1),
    'textEmphasis': _rgb(0xF2F3F5),
    'textSecondary': _rgb(0xB5BAC1),
    'textTertiary': _rgb(0x80848E),
    'textDisabled': _rgb(0x4E5058),
    'textOnAccent': _rgb(0xFFFFFF),
    'textInverse': _rgb(0x313338),
    'accentPrimary': _rgb(0x5865F2),
    'accentPrimaryHover': _rgb(0x6D78F7),
    'accentPrimaryActive': _rgb(0x4752C4),
    'accentPrimaryMuted': _rgb(0x3C438E),
    'accentSecondary': _rgb(0xF0B232),
    'accentSecondaryHover': _rgb(0xF5C063),
    'accentSecondaryMuted': _rgb(0x6B4C10),
    'statusOnline': _rgb(0x23A55A),
    'statusOffline': _rgb(0x80848E),
    'statusDanger': _rgb(0xDA373C),
    'statusDangerMuted': _rgb(0x6E1E22),
    'statusWarn': _rgb(0xF0B232),
    'linkColor': _rgb(0x00A8FC),
    'linkHoverColor': _rgb(0x4EC0FF),
  },
  sections: {
    'serverRail': {
      'bgApp': _rgb(0x1E1F22),
      'bgInset': _rgb(0x313338),
      'bgSelected': _rgb(0x5865F2),
      'borderDefault': _rgb(0x1E1F22),
      'borderStrong': _rgb(0x4E5058),
      'borderSubtle': _rgb(0x2B2D31),
      'accentPrimary': _rgb(0xFFFFFF),
      'textSecondary': _rgb(0xDBDEE1),
    },
    'channelList': {
      'bgSurface': _rgb(0x2B2D31),
      'bgHover': _rgb(0x35373C),
      'bgSelected': _rgb(0x404249),
      'borderSubtle': _rgb(0x232428),
      'accentPrimary': _rgb(0xF2F3F5),
      'textSecondary': _rgb(0x949BA4),
    },
    'presence': {
      'bgSurface': _rgb(0x2B2D31),
      'borderSubtle': _rgb(0x232428),
      'textSecondary': _rgb(0x949BA4),
    },
    'topBar': {
      'bgSurfaceRaised': _rgb(0x313338),
      'borderSubtle': _rgb(0x232428),
      'bgInset': _rgb(0x2B2D31),
    },
    'content': {
      'bgSurface': _rgb(0x313338),
      'bgInset': _rgb(0x383A40),
    },
    'dialogs': {
      'bgSurfaceRaised': _rgb(0x313338),
      'bgInset': _rgb(0x1E1F22),
    },
  },
  styles: {
    'base': {
      ..._modernBaseStyle,
      TCSectionStyle.keyCornerRadius: 8.0,
      TCSectionStyle.keyAvatarShape: TCSectionStyle.avatarCircle,
    },
    'serverRail': {TCSectionStyle.keyCornerRadius: 12.0},
    'channelList': {TCSectionStyle.keyCornerRadius: 6.0},
  },
);

/// "Slack": aubergine rail and channel list against a white workspace, the
/// one preset here that is light where it counts.
final ThemeSpec _slack = ThemeSpec(
  base: {
    'bgApp': _rgb(0xFFFFFF),
    'bgSurface': _rgb(0xFFFFFF),
    'bgSurfaceRaised': _rgb(0xFFFFFF),
    'bgInset': _rgb(0xF1F0F1),
    'bgHover': _rgb(0xF4F3F4),
    'bgPressed': _rgb(0xEDEDED),
    'bgSelected': _rgb(0xE8F5FA),
    'borderSubtle': _rgb(0xE8E8E8),
    'borderDefault': _rgb(0xDDDDDD),
    'borderStrong': _rgb(0xBBBABB),
    'borderAccent': _rgb(0x1264A3),
    'textPrimary': _rgb(0x1D1C1D),
    'textEmphasis': _rgb(0x000000),
    'textSecondary': _rgb(0x616061),
    'textTertiary': _rgb(0x868686),
    'textDisabled': _rgb(0xBBBABB),
    'textOnAccent': _rgb(0xFFFFFF),
    'textInverse': _rgb(0xFFFFFF),
    'accentPrimary': _rgb(0x007A5A),
    'accentPrimaryHover': _rgb(0x148567),
    'accentPrimaryActive': _rgb(0x00614A),
    'accentPrimaryMuted': _rgb(0xD9EFE7),
    'accentSecondary': _rgb(0x1264A3),
    'accentSecondaryHover': _rgb(0x1A73BC),
    'accentSecondaryMuted': _rgb(0xD8E9F7),
    'statusOnline': _rgb(0x2BAC76),
    'statusOffline': _rgb(0xA0A0A0),
    'statusDanger': _rgb(0xE01E5A),
    'statusDangerMuted': _rgb(0xF7D6E1),
    'statusWarn': _rgb(0xECB22E),
    'linkColor': _rgb(0x1264A3),
    'linkHoverColor': _rgb(0x0B4C82),
  },
  sections: {
    'serverRail': {
      'bgApp': _rgb(0x3F0E40),
      'bgInset': _rgb(0x350D36),
      'bgSelected': _rgb(0x1164A3),
      'bgHover': _rgb(0x4A154B),
      'borderSubtle': _rgb(0x522653),
      'borderDefault': _rgb(0x350D36),
      'borderStrong': _rgb(0x6B3D6C),
      'borderAccent': _rgb(0x1164A3),
      'accentPrimary': _rgb(0xFFFFFF),
      'textPrimary': _rgb(0xFFFFFF),
      'textEmphasis': _rgb(0xFFFFFF),
      'textSecondary': _rgb(0xBCABBC),
      'textTertiary': _rgb(0x9B849C),
    },
    'channelList': {
      'bgSurface': _rgb(0x3F0E40),
      'bgHover': _rgb(0x350D36),
      'bgSelected': _rgb(0x1164A3),
      'bgInset': _rgb(0x350D36),
      'borderSubtle': _rgb(0x522653),
      'borderDefault': _rgb(0x6B3D6C),
      'borderStrong': _rgb(0x8A5E8B),
      'borderAccent': _rgb(0x1164A3),
      'accentPrimary': _rgb(0xFFFFFF),
      'accentSecondary': _rgb(0xE8912D),
      'accentSecondaryHover': _rgb(0xF3A94C),
      'textPrimary': _rgb(0xFFFFFF),
      'textEmphasis': _rgb(0xFFFFFF),
      'textSecondary': _rgb(0xBCABBC),
      'textTertiary': _rgb(0x9B849C),
    },
    'presence': {
      'bgSurface': _rgb(0x3F0E40),
      'borderSubtle': _rgb(0x522653),
      'accentPrimary': _rgb(0xFFFFFF),
      'textPrimary': _rgb(0xFFFFFF),
      'textEmphasis': _rgb(0xFFFFFF),
      'textSecondary': _rgb(0xBCABBC),
      'textTertiary': _rgb(0x9B849C),
    },
    'topBar': {
      'bgSurfaceRaised': _rgb(0xFFFFFF),
      'bgInset': _rgb(0xF4F3F4),
      'borderSubtle': _rgb(0xE8E8E8),
    },
  },
  styles: {
    'base': {
      ..._modernBaseStyle,
      TCSectionStyle.keyCornerRadius: 6.0,
      TCSectionStyle.keyAvatarShape: TCSectionStyle.avatarRounded,
    },
  },
);

/// "Telegram": a light client with a blue accent, a blue-selected chat row
/// and the roundest corners of the set.
final ThemeSpec _telegram = ThemeSpec(
  base: {
    'bgApp': _rgb(0xEDF1F5),
    'bgSurface': _rgb(0xFFFFFF),
    'bgSurfaceRaised': _rgb(0xFFFFFF),
    'bgInset': _rgb(0xF1F3F5),
    'bgHover': _rgb(0xF1F5F9),
    'bgPressed': _rgb(0xE3EAF1),
    'bgSelected': _rgb(0x3390EC),
    'borderSubtle': _rgb(0xE9EDF1),
    'borderDefault': _rgb(0xDFE4E9),
    'borderStrong': _rgb(0xC3CBD3),
    'borderAccent': _rgb(0x3390EC),
    'textPrimary': _rgb(0x0F0F0F),
    'textEmphasis': _rgb(0x000000),
    'textSecondary': _rgb(0x707579),
    'textTertiary': _rgb(0xA2ACB4),
    'textDisabled': _rgb(0xC3CBD3),
    'textOnAccent': _rgb(0xFFFFFF),
    'textInverse': _rgb(0xFFFFFF),
    'accentPrimary': _rgb(0x3390EC),
    'accentPrimaryHover': _rgb(0x4EA0F0),
    'accentPrimaryActive': _rgb(0x2B7CD3),
    'accentPrimaryMuted': _rgb(0xD6E9FB),
    'accentSecondary': _rgb(0x00A884),
    'accentSecondaryHover': _rgb(0x1FBF9C),
    'accentSecondaryMuted': _rgb(0xD3F0E8),
    'statusOnline': _rgb(0x4DCD5E),
    'statusOffline': _rgb(0xA2ACB4),
    'statusDanger': _rgb(0xE53935),
    'statusDangerMuted': _rgb(0xF9D7D6),
    'statusWarn': _rgb(0xF5A623),
    'linkColor': _rgb(0x168ACD),
    'linkHoverColor': _rgb(0x0F6FA8),
  },
  sections: {
    'serverRail': {
      'bgApp': _rgb(0xF4F6F8),
      'bgInset': _rgb(0xFFFFFF),
      'bgSelected': _rgb(0xD6E9FB),
      'borderDefault': _rgb(0xE2E7EC),
      'borderSubtle': _rgb(0xE2E7EC),
    },
    'channelList': {
      'bgSurface': _rgb(0xFFFFFF),
      'bgHover': _rgb(0xF1F5F9),
      'bgSelected': _rgb(0xE7F3FE),
    },
    'content': {
      'bgSurface': _rgb(0xFFFFFF),
      'bgInset': _rgb(0xF1F3F5),
    },
  },
  styles: {
    'base': {
      ..._modernBaseStyle,
      TCSectionStyle.keyCornerRadius: 14.0,
      TCSectionStyle.keyAvatarShape: TCSectionStyle.avatarCircle,
    },
  },
);

/// "WhatsApp": the dark teal-green look -- deep ink surfaces, a green
/// accent, round avatars and a pill composer.
final ThemeSpec _whatsapp = ThemeSpec(
  base: {
    'bgApp': _rgb(0x0B141A),
    'bgSurface': _rgb(0x111B21),
    'bgSurfaceRaised': _rgb(0x202C33),
    'bgInset': _rgb(0x2A3942),
    'bgHover': _rgb(0x202C33),
    'bgPressed': _rgb(0x18242B),
    'bgSelected': _rgb(0x2A3942),
    'borderSubtle': _rgb(0x222D34),
    'borderDefault': _rgb(0x2A3942),
    'borderStrong': _rgb(0x3B4A54),
    'borderAccent': _rgb(0x00A884),
    'textPrimary': _rgb(0xE9EDEF),
    'textEmphasis': _rgb(0xFFFFFF),
    'textSecondary': _rgb(0x8696A0),
    'textTertiary': _rgb(0x667781),
    'textDisabled': _rgb(0x3B4A54),
    'textOnAccent': _rgb(0x0B141A),
    'textInverse': _rgb(0x0B141A),
    'accentPrimary': _rgb(0x00A884),
    'accentPrimaryHover': _rgb(0x06CF9C),
    'accentPrimaryActive': _rgb(0x008069),
    'accentPrimaryMuted': _rgb(0x0B3D33),
    'accentSecondary': _rgb(0x53BDEB),
    'accentSecondaryHover': _rgb(0x7ED0F2),
    'accentSecondaryMuted': _rgb(0x10394B),
    'statusOnline': _rgb(0x00A884),
    'statusOffline': _rgb(0x667781),
    'statusDanger': _rgb(0xF15C6D),
    'statusDangerMuted': _rgb(0x50242B),
    'statusWarn': _rgb(0xFFD279),
    'linkColor': _rgb(0x53BDEB),
    'linkHoverColor': _rgb(0x7ED0F2),
  },
  sections: {
    'serverRail': {
      'bgApp': _rgb(0x0B141A),
      'bgInset': _rgb(0x202C33),
      'bgSelected': _rgb(0x2A3942),
      'borderDefault': _rgb(0x202C33),
    },
    'channelList': {
      'bgSurface': _rgb(0x111B21),
      'bgHover': _rgb(0x202C33),
      'bgSelected': _rgb(0x2A3942),
      'borderSubtle': _rgb(0x222D34),
    },
    'presence': {
      'bgSurface': _rgb(0x111B21),
      'borderSubtle': _rgb(0x222D34),
    },
    'topBar': {
      'bgSurfaceRaised': _rgb(0x202C33),
      'bgInset': _rgb(0x111B21),
      'borderSubtle': _rgb(0x222D34),
    },
    'content': {
      'bgSurface': _rgb(0x111B21),
      'bgInset': _rgb(0x2A3942),
    },
    'dialogs': {
      'bgSurfaceRaised': _rgb(0x202C33),
      'bgInset': _rgb(0x111B21),
    },
  },
  styles: {
    'base': {
      ..._modernBaseStyle,
      TCSectionStyle.keyCornerRadius: 10.0,
      TCSectionStyle.keyAvatarShape: TCSectionStyle.avatarCircle,
    },
  },
);

/// Presets offered by the appearance editor, stock look first.
final List<ThemePreset> themePresets = [
  ThemePreset(name: 'Trench', spec: ThemeSpec.empty),
  ThemePreset(name: 'Ember', spec: _ember),
  ThemePreset(name: 'Discord', spec: _discord),
  ThemePreset(name: 'Slack', spec: _slack),
  ThemePreset(name: 'Telegram', spec: _telegram),
  ThemePreset(name: 'WhatsApp', spec: _whatsapp),
];
