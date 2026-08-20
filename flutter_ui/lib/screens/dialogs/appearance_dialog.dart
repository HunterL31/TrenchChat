// Appearance editor: the whole per-section theme, one scope at a time --
// the style block (text size, glow, display font) first, then every color.
//
// Scope is either Base -- the overrides every section inherits -- or one of
// the six sections, whose overrides win over base for that region only. The
// editor previews its own draft (the dialog re-themes as you type) and only
// writes it to the backend on APPLY, so a half-finished palette is never
// persisted.
//
// Every edit derives a new spec from the live one with the ThemeSpec with*/
// clear* methods rather than rebuilding a spec from scratch, which is what
// keeps section ids this client does not know from being dropped.
import 'package:flutter/material.dart';

import '../../app_state.dart';
import '../../theme/section_theme.dart';
import '../../theme/theme_presets.dart';
import '../../theme/theme_spec.dart';
import '../../theme/tokens.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_checkbox.dart';
import '../../widgets/tc_color_field.dart';
import '../../widgets/tc_dialog.dart';

/// Human label per scope, in the order the scope picker shows them. The key
/// is `base` or a [TCSection] wire id.
const Map<String, String> appearanceScopeLabels = {
  'base': 'BASE',
  'serverRail': 'SERVER RAIL',
  'channelList': 'CHANNEL LIST',
  'presence': 'PRESENCE',
  'topBar': 'TOP BAR',
  'content': 'CONTENT',
  'dialogs': 'DIALOGS',
};

/// The text scales the editor offers, keyed by the value they store.
const Map<String, String> appearanceTextScaleOptions = {
  '0.9': '90%',
  '1.0': '100%',
  '1.1': '110%',
  '1.25': '125%',
};

/// Label per bundled display family, in the order the editor offers them.
const Map<String, String> appearanceDisplayFontLabels = {
  'VT323': 'VT323',
  'IBM Plex Mono': 'PLEX MONO',
};

/// The option key standing for "this scope sets nothing here".
const String _inheritKey = '';

Future<void> showAppearanceDialog(BuildContext context, AppState state) {
  return showTcDialog<void>(
    context: context,
    builder: (context) => _AppearanceDialogContent(state: state),
  );
}

class _AppearanceDialogContent extends StatefulWidget {
  const _AppearanceDialogContent({required this.state});
  final AppState state;

  @override
  State<_AppearanceDialogContent> createState() => _AppearanceDialogContentState();
}

class _AppearanceDialogContentState extends State<_AppearanceDialogContent> {
  late ThemeSpec _draft = widget.state.themeSpec;
  String _scope = 'base';
  bool _busy = false;
  String? _error;

  /// The section this scope edits, or null when the scope is Base.
  TCSection? get _section => _scope == 'base' ? null : TCSection.fromWireId(_scope);

  /// The palette the scope renders with: stock, then base, then -- for a
  /// section scope -- that section's own overrides.
  TCSectionColors get _resolved {
    final section = _section;
    return section == null ? _draft.resolveBase() : _draft.resolve(section);
  }

  /// The overrides this scope sets itself. Base overrides are inherited by a
  /// section scope, not owned by it, so they are not listed here.
  Map<String, Color> get _ownOverrides {
    final section = _section;
    return section == null ? _draft.base : (_draft.sections[section.wireId] ?? const {});
  }

  /// The style the scope renders with, resolved the same way as [_resolved].
  TCSectionStyle get _resolvedStyle {
    final section = _section;
    return section == null ? _draft.resolveBaseStyle() : _draft.resolveStyle(section);
  }

  /// The style keys this scope sets itself, on the same terms as
  /// [_ownOverrides].
  Map<String, Object> get _ownStyles => _draft.styleOverridesFor(_section);

  /// What a scope that sets nothing calls its inherited value.
  String get _inheritLabel => _scope == 'base' ? 'DEFAULT' : 'INHERIT';

  void _setToken(String tokenKey, Color? color) {
    final section = _section;
    setState(() {
      _draft = section == null
          ? _draft.withBaseOverride(tokenKey, color)
          : _draft.withSectionOverride(section, tokenKey, color);
    });
  }

  void _setStyle(String styleKey, Object? value) {
    setState(() => _draft = _draft.withStyleOverride(_section, styleKey, value));
  }

  /// The text-scale option this scope selects: the inherit chip when it sets
  /// none, or a key no chip carries when it stores a scale the editor does
  /// not offer, which leaves the row with nothing selected.
  String get _selectedTextScale {
    final own = _ownStyles[TCSectionStyle.keyTextScale];
    if (own is! num) return _inheritKey;
    for (final key in appearanceTextScaleOptions.keys) {
      if (double.parse(key) == own.toDouble()) return key;
    }
    return 'custom';
  }

  String get _selectedDisplayFont {
    final own = _ownStyles[TCSectionStyle.keyDisplayFont];
    return own is String ? own : _inheritKey;
  }

  /// True when RESET ALL would change something. Overrides under a section
  /// id this client does not know are not resettable and do not count.
  bool get _hasResettableOverrides =>
      _draft.base.isNotEmpty ||
      _draft.styleOverridesFor(null).isNotEmpty ||
      TCSection.values.any((s) =>
          (_draft.sections[s.wireId] ?? const {}).isNotEmpty ||
          _draft.styleOverridesFor(s).isNotEmpty);

  void _resetScope() {
    final section = _section;
    setState(() {
      _draft = section == null ? _draft.clearBase() : _draft.clearSection(section);
    });
  }

  /// Replaces every override this client knows with the preset's, keeping
  /// overrides under unknown section ids (same rule as RESET ALL).
  void _applyPreset(ThemePreset preset) {
    setState(() {
      var next = _draft.clearBase();
      for (final section in TCSection.values) {
        next = next.clearSection(section);
      }
      next = next.withBaseOverrides(preset.spec.base);
      next = next.withStyleOverrides(null, preset.spec.styleOverridesFor(null));
      for (final entry in preset.spec.sections.entries) {
        final section = TCSection.fromWireId(entry.key);
        if (section != null) next = next.withSectionOverrides(section, entry.value);
      }
      for (final section in TCSection.values) {
        next = next.withStyleOverrides(section, preset.spec.styleOverridesFor(section));
      }
      _draft = next;
    });
  }

  void _resetAll() {
    setState(() {
      var next = _draft.clearBase();
      for (final section in TCSection.values) {
        next = next.clearSection(section);
      }
      _draft = next;
    });
  }

  Future<void> _apply() async {
    setState(() {
      _busy = true;
      _error = null;
    });
    final target = _draft;
    await widget.state.saveTheme(target);
    if (!mounted) return;
    if (widget.state.themeSpec != target) {
      setState(() {
        _busy = false;
        _error = widget.state.actionError ?? 'Could not save the theme.';
      });
      return;
    }
    Navigator.pop(context);
  }

  @override
  Widget build(BuildContext context) {
    return SectionTheme(
      spec: _draft,
      section: TCSection.dialogs,
      child: Builder(builder: _buildContent),
    );
  }

  Widget _caption(TCSectionColors tc, String text) => Text(
        text,
        style: TextStyle(
          fontSize: TCType.textCaption,
          color: tc.accentPrimary,
          letterSpacing: TCType.letterSpacingFor(TCType.textCaption, TCType.trackingWider),
        ),
      );

  Widget _styleLabel(TCSectionColors tc, String text) => Text(
        text,
        style: TextStyle(
          fontSize: TCType.textMicro,
          color: tc.textSecondary,
          letterSpacing: TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWide),
        ),
      );

  Widget _buildContent(BuildContext context) {
    final tc = SectionTheme.of(context);
    final resolved = _resolved.asMap();
    final own = _ownOverrides;
    final scopeLabel = appearanceScopeLabels[_scope] ?? _scope;

    return TcDialogShell(
      title: 'Appearance',
      width: 520,
      errorText: _error,
      actions: [
        TcGhostButton(label: 'CLOSE', onPressed: () => Navigator.pop(context)),
        TcPrimaryButton(
          label: _busy ? 'SAVING…' : 'APPLY',
          onPressed: _busy ? null : _apply,
        ),
      ],
      children: [
        _caption(tc, 'PRESETS'),
        const SizedBox(height: 8),
        Row(
          children: [
            for (final preset in themePresets) ...[
              TcGhostButton(
                label: preset.name.toUpperCase(),
                onPressed: () => _applyPreset(preset),
              ),
              const SizedBox(width: 6),
            ],
          ],
        ),
        const SizedBox(height: 12),
        Container(height: 1, color: tc.borderSubtle),
        const SizedBox(height: 12),
        _caption(tc, 'SCOPE'),
        const SizedBox(height: 8),
        TcChoiceRow(
          options: appearanceScopeLabels,
          value: _scope,
          onSelected: (v) => setState(() => _scope = v),
        ),
        const SizedBox(height: 8),
        Text(
          _scope == 'base'
              ? 'Base colors and styles apply everywhere. A section can override any of them.'
              : 'These apply to $scopeLabel only, on top of the base colors and styles.',
          style: TextStyle(fontSize: TCType.textMicro, color: tc.textTertiary),
        ),
        const SizedBox(height: 6),
        Text(
          own.isEmpty && _ownStyles.isEmpty
              ? 'No overrides in this scope — every color is inherited.'
              : '${own.length + _ownStyles.length} '
                  'override${own.length + _ownStyles.length == 1 ? '' : 's'} in this scope.',
          style: TextStyle(fontSize: TCType.textMicro, color: tc.textSecondary),
        ),
        const SizedBox(height: 12),
        Container(height: 1, color: tc.borderSubtle),
        const SizedBox(height: 12),
        _caption(tc, 'STYLE'),
        const SizedBox(height: 8),
        _styleLabel(tc, 'TEXT SIZE'),
        const SizedBox(height: 4),
        TcChoiceRow(
          options: {_inheritKey: _inheritLabel, ...appearanceTextScaleOptions},
          value: _selectedTextScale,
          onSelected: (v) => _setStyle(
            TCSectionStyle.keyTextScale,
            v == _inheritKey ? null : double.parse(v),
          ),
        ),
        const SizedBox(height: 10),
        _styleLabel(tc, 'GLOW'),
        const SizedBox(height: 4),
        Row(
          children: [
            TcCheckbox(
              value: _resolvedStyle.glow,
              label: 'Accent glow',
              onChanged: (v) => _setStyle(TCSectionStyle.keyGlow, v),
            ),
            if (_ownStyles.containsKey(TCSectionStyle.keyGlow)) ...[
              const SizedBox(width: 10),
              TcGhostButton(
                label: 'CLEAR',
                onPressed: () => _setStyle(TCSectionStyle.keyGlow, null),
              ),
            ],
          ],
        ),
        const SizedBox(height: 10),
        _styleLabel(tc, 'DISPLAY FONT'),
        const SizedBox(height: 4),
        TcChoiceRow(
          options: {_inheritKey: _inheritLabel, ...appearanceDisplayFontLabels},
          value: _selectedDisplayFont,
          onSelected: (v) => _setStyle(
            TCSectionStyle.keyDisplayFont,
            v == _inheritKey ? null : v,
          ),
        ),
        const SizedBox(height: 12),
        Container(height: 1, color: tc.borderSubtle),
        const SizedBox(height: 12),
        Container(
          constraints: const BoxConstraints(maxHeight: 300),
          child: SingleChildScrollView(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                for (final key in TCSectionColors.tokenKeys)
                  TcColorField(
                    key: ValueKey('$_scope/$key'),
                    label: key,
                    color: resolved[key]!,
                    overridden: own.containsKey(key),
                    onChanged: (color) => _setToken(key, color),
                    onClear: () => _setToken(key, null),
                  ),
              ],
            ),
          ),
        ),
        const SizedBox(height: 12),
        Container(height: 1, color: tc.borderSubtle),
        const SizedBox(height: 12),
        Row(
          children: [
            TcGhostButton(
              label: _scope == 'base' ? 'RESET BASE' : 'RESET SECTION',
              onPressed: own.isEmpty && _ownStyles.isEmpty ? null : _resetScope,
            ),
            const SizedBox(width: 6),
            TcGhostButton(
              label: 'RESET ALL',
              onPressed: _hasResettableOverrides ? _resetAll : null,
            ),
          ],
        ),
      ],
    );
  }
}
