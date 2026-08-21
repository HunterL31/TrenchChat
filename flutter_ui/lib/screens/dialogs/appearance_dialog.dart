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
//
// A draft can also be kept under a name (MY THEMES), which is a library
// separate from the theme in force: saving one changes nothing about how the
// app looks, while applying one puts it in force there and then, the same as
// the dialog's own APPLY but without closing. SHARE stages the
// theme's code (theme/theme_code.dart) into the compose box and closes the
// way back to it -- nothing is sent until the user sends it.
import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../../app_state.dart';
import '../../theme/section_theme.dart';
import '../../theme/theme_code.dart';
import '../../theme/theme_presets.dart';
import '../../theme/theme_spec.dart';
import '../../theme/tokens.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_checkbox.dart';
import '../../widgets/tc_color_field.dart';
import '../../widgets/tc_dialog.dart';
import '../../widgets/tc_icon.dart';
import '../../widgets/tc_text_field.dart';

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

/// How long a delete stays armed before the row goes back to offering ×.
const Duration appearanceDeleteConfirmWindow = Duration(seconds: 3);

/// The name field of the SAVE AS… row.
const Key appearanceSaveAsFieldKey = Key('appearance-save-as');

/// The preset chip row; its selected value is the preset the draft matches.
const Key appearancePresetRowKey = Key('appearance-presets');

/// Keys of one saved theme's row controls. The row's APPLY carries the same
/// label as the dialog's own, so these are how a caller tells them apart.
/// The delete key stays on whatever occupies that slot -- × first, then the
/// SURE? confirmation -- so deleting is the same control tapped twice.
Key appearanceApplySavedKey(String name) => Key('theme-apply:$name');
Key appearanceShareSavedKey(String name) => Key('theme-share:$name');
Key appearanceDeleteSavedKey(String name) => Key('theme-delete:$name');

/// Opens the editor. Resolves to true when it closed because a theme was
/// staged into the compose box, which is the caller's cue to get out of the
/// way too (see settings_dialog.dart).
Future<bool?> showAppearanceDialog(BuildContext context, AppState state) {
  return showTcDialog<bool>(
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
  final TextEditingController _saveAsName = TextEditingController();
  String _scope = 'base';
  bool _busy = false;
  String? _error;

  /// One line of feedback for a library action -- saved, shared, deleted.
  String? _notice;

  /// The saved theme the draft is understood to be wearing, so duplicates of
  /// one spec do not all claim to be the active one. Null when the draft
  /// belongs to no saved theme.
  String? _activeName;

  /// The row whose delete is armed, waiting for the confirming second click.
  String? _armedDelete;
  Timer? _armedDeleteTimer;

  @override
  void initState() {
    super.initState();
    _saveAsName.addListener(() => setState(() {}));
    final applied = widget.state.themeSpec;
    for (final name in _savedNames) {
      if (widget.state.themeLibrary[name] == applied) {
        _activeName = name;
        break;
      }
    }
  }

  @override
  void dispose() {
    _armedDeleteTimer?.cancel();
    _saveAsName.dispose();
    super.dispose();
  }

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

  /// The preset the draft currently matches, or '' when it matches none.
  String get _activePresetName {
    for (final p in themePresets) {
      if (p.spec == _draft) return p.name;
    }
    return '';
  }

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

  /// Replaces every override this client knows with [spec]'s, keeping
  /// overrides under unknown section ids (same rule as RESET ALL). This is
  /// what a preset and a saved theme both apply through.
  void _applySpec(ThemeSpec spec) {
    setState(() {
      var next = _draft.clearBase();
      for (final section in TCSection.values) {
        next = next.clearSection(section);
      }
      next = next.withBaseOverrides(spec.base);
      next = next.withStyleOverrides(null, spec.styleOverridesFor(null));
      for (final entry in spec.sections.entries) {
        final section = TCSection.fromWireId(entry.key);
        if (section != null) next = next.withSectionOverrides(section, entry.value);
      }
      for (final section in TCSection.values) {
        next = next.withStyleOverrides(section, spec.styleOverridesFor(section));
      }
      _draft = next;
      _notice = null;
    });
  }

  /// The saved themes, newest names last -- ordered by name so the list does
  /// not reshuffle when one is replaced.
  List<String> get _savedNames => widget.state.themeLibrary.keys.toList()..sort();

  /// The trimmed name SAVE AS… would write to, empty when the field is blank.
  String get _saveAsTarget => _saveAsName.text.trim();

  /// True when saving would replace a theme already in the library with a
  /// different one. Re-saving a name that already holds this exact draft
  /// changes nothing, so it is not an overwrite worth warning about.
  bool get _saveAsOverwrites {
    if (_saveAsTarget.isEmpty) return false;
    final stored = widget.state.themeLibrary[_saveAsTarget];
    return stored != null && stored != _draft;
  }

  Future<void> _runLibraryAction(Future<bool> Function() action, String success) async {
    setState(() {
      _busy = true;
      _error = null;
      _notice = null;
    });
    final ok = await action();
    if (!mounted) return;
    setState(() {
      _busy = false;
      _notice = ok ? success : null;
      _error = ok ? null : (widget.state.takeActionError() ?? 'Could not reach the backend.');
    });
  }

  Future<void> _saveDraftAs() async {
    final name = _saveAsTarget;
    if (name.isEmpty) return;
    final replaced = _saveAsOverwrites;
    await _runLibraryAction(
      () => widget.state.saveThemeAs(name, _draft),
      replaced ? 'Replaced $name.' : 'Saved as $name.',
    );
    if (!mounted || _error != null) return;
    setState(() => _activeName = name);
    _saveAsName.clear();
  }

  /// Puts a saved theme in force: it is persisted at once, the draft becomes
  /// it, and the dialog stays open. The row's APPLY means the same thing the
  /// shared-theme card's does -- not "load this for editing".
  Future<void> _applySaved(String name, ThemeSpec spec) async {
    setState(() {
      _busy = true;
      _error = null;
      _notice = null;
    });
    await widget.state.saveTheme(spec);
    if (!mounted) return;
    if (widget.state.themeSpec != spec) {
      setState(() {
        _busy = false;
        _error = widget.state.takeActionError() ?? 'Could not save the theme.';
      });
      return;
    }
    setState(() {
      _busy = false;
      _draft = spec;
      _activeName = name;
      _notice = 'Applied $name.';
    });
  }

  /// First click on × arms the row; the second, within
  /// [appearanceDeleteConfirmWindow], deletes. Anything else clicked in the
  /// dialog disarms it (see [_disarmDelete]).
  void _armDelete(String name) {
    _armedDeleteTimer?.cancel();
    setState(() => _armedDelete = name);
    _armedDeleteTimer = Timer(appearanceDeleteConfirmWindow, () {
      if (mounted) setState(() => _armedDelete = null);
    });
  }

  void _disarmDelete() {
    _armedDeleteTimer?.cancel();
    _armedDeleteTimer = null;
    if (_armedDelete != null) setState(() => _armedDelete = null);
  }

  /// Hands the theme's code to the compose box and leaves, closing the
  /// settings dialog underneath so the user lands on the draft.
  void _shareSaved(String name, ThemeSpec spec) {
    widget.state.stageThemeShare(name, encodeThemeCode(name, spec));
    Navigator.pop(context, true);
  }

  Future<void> _deleteSaved(String name) {
    _disarmDelete();
    if (_activeName == name) _activeName = null;
    return _runLibraryAction(
      () => widget.state.deleteSavedTheme(name),
      'Deleted $name.',
    );
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
        _error = widget.state.takeActionError() ?? 'Could not save the theme.';
      });
      return;
    }
    Navigator.pop(context);
  }

  @override
  Widget build(BuildContext context) {
    // The pointer-up runs before any tap callback, so a click landing on a
    // delete control still arms or confirms it after this disarms the row.
    return SectionTheme(
      spec: _draft,
      section: TCSection.dialogs,
      child: Listener(
        onPointerUp: _armedDelete == null ? null : (_) => _disarmDelete(),
        child: Builder(builder: _buildContent),
      ),
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

  Widget _savedThemeRow(TCSectionColors tc, String name) {
    final spec = widget.state.themeLibrary[name] ?? ThemeSpec.empty;
    final active = name == _activeName && spec == _draft;
    // The name wears its own theme's colors, so each row previews itself.
    final own = spec.resolveBase();
    return Padding(
      padding: const EdgeInsets.only(bottom: 4),
      child: Row(
        children: [
          Expanded(
            child: Align(
              alignment: Alignment.centerLeft,
              child: Container(
                padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                decoration: BoxDecoration(
                  color: own.bgApp,
                  border: Border.all(color: tc.borderSubtle),
                ),
                child: Text(
                  name,
                  overflow: TextOverflow.ellipsis,
                  softWrap: false,
                  style: TextStyle(fontSize: TCType.textBodySm, color: own.textPrimary),
                ),
              ),
            ),
          ),
          if (active) ...[
            const SizedBox(width: 6),
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 5, vertical: 1),
              decoration: BoxDecoration(
                color: tc.bgSelected,
                border: Border.all(color: tc.borderAccent),
              ),
              child: Text(
                'ACTIVE',
                style: TextStyle(
                  fontSize: TCType.textMicro,
                  color: tc.textEmphasis,
                  letterSpacing:
                      TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWide),
                ),
              ),
            ),
          ],
          const SizedBox(width: 6),
          TcGhostButton(
            key: appearanceApplySavedKey(name),
            label: 'APPLY',
            onPressed: _busy ? null : () => _applySaved(name, spec),
          ),
          const SizedBox(width: 6),
          TcGhostButton(
            key: appearanceShareSavedKey(name),
            label: 'SHARE',
            onPressed: _busy ? null : () => _shareSaved(name, spec),
          ),
          const SizedBox(width: 6),
          if (_armedDelete == name)
            TcGhostButton(
              key: appearanceDeleteSavedKey(name),
              label: 'SURE?',
              accent: tc.statusDanger,
              onPressed: _busy ? null : () => _deleteSaved(name),
            )
          else
            SizedBox(
              width: 22,
              height: 22,
              child: TcIconButton(
                key: appearanceDeleteSavedKey(name),
                icon: TcIcons.close,
                tooltip: 'Delete theme',
                size: 22,
                onPressed: _busy ? null : () => _armDelete(name),
              ),
            ),
        ],
      ),
    );
  }

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
        TcChoiceRow(
          key: appearancePresetRowKey,
          options: {for (final p in themePresets) p.name: p.name.toUpperCase()},
          value: _activePresetName,
          onSelected: _busy
              ? null
              : (name) =>
                  _applySpec(themePresets.firstWhere((p) => p.name == name).spec),
        ),
        const SizedBox(height: 12),
        _caption(tc, 'MY THEMES'),
        const SizedBox(height: 8),
        if (_savedNames.isEmpty)
          Text(
            'Nothing saved yet — name the current draft below to keep it.',
            style: TextStyle(fontSize: TCType.textMicro, color: tc.textTertiary),
          )
        else
          for (final name in _savedNames) _savedThemeRow(tc, name),
        const SizedBox(height: 8),
        Row(
          crossAxisAlignment: CrossAxisAlignment.end,
          children: [
            Expanded(
              child: TcTextField(
                key: appearanceSaveAsFieldKey,
                label: 'Save the current draft as',
                controller: _saveAsName,
                hintText: 'Theme name',
                inputFormatters: [
                  LengthLimitingTextInputFormatter(maxThemeNameLength),
                ],
                onSubmitted: (_) => _saveDraftAs(),
              ),
            ),
            const SizedBox(width: 6),
            TcGhostButton(
              label: _saveAsOverwrites ? 'OVERWRITE' : 'SAVE AS…',
              onPressed: _busy || _saveAsTarget.isEmpty ? null : _saveDraftAs,
            ),
          ],
        ),
        if (_saveAsOverwrites) ...[
          const SizedBox(height: 6),
          Text(
            'Overwrites existing "$_saveAsTarget".',
            style: TextStyle(fontSize: TCType.textMicro, color: tc.statusWarn),
          ),
        ],
        if (_notice != null) ...[
          const SizedBox(height: 6),
          Text(
            _notice!,
            style: TextStyle(fontSize: TCType.textMicro, color: tc.accentSecondary),
          ),
        ],
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
                    displayLabel: tokenLabels[key],
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
