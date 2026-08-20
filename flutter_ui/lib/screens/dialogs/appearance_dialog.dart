// Appearance editor: the whole per-section theme, one scope at a time.
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

  void _setToken(String tokenKey, Color? color) {
    final section = _section;
    setState(() {
      _draft = section == null
          ? _draft.withBaseOverride(tokenKey, color)
          : _draft.withSectionOverride(section, tokenKey, color);
    });
  }

  /// True when RESET ALL would change something. Overrides under a section
  /// id this client does not know are not resettable and do not count.
  bool get _hasResettableOverrides =>
      _draft.base.isNotEmpty ||
      TCSection.values.any((s) => (_draft.sections[s.wireId] ?? const {}).isNotEmpty);

  void _resetScope() {
    final section = _section;
    setState(() {
      _draft = section == null ? _draft.clearBase() : _draft.clearSection(section);
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
        Text(
          'SCOPE',
          style: TextStyle(
            fontSize: TCType.textCaption,
            color: tc.accentPrimary,
            letterSpacing: TCType.letterSpacingFor(TCType.textCaption, TCType.trackingWider),
          ),
        ),
        const SizedBox(height: 8),
        TcChoiceRow(
          options: appearanceScopeLabels,
          value: _scope,
          onSelected: (v) => setState(() => _scope = v),
        ),
        const SizedBox(height: 8),
        Text(
          _scope == 'base'
              ? 'Base colors apply everywhere. A section can override any of them.'
              : 'These colors apply to $scopeLabel only, on top of the base colors.',
          style: TextStyle(fontSize: TCType.textMicro, color: tc.textTertiary),
        ),
        const SizedBox(height: 6),
        Text(
          own.isEmpty
              ? 'No overrides in this scope — every color is inherited.'
              : '${own.length} override${own.length == 1 ? '' : 's'} in this scope.',
          style: TextStyle(fontSize: TCType.textMicro, color: tc.textSecondary),
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
              onPressed: own.isEmpty ? null : _resetScope,
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
