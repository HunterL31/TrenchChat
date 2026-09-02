// Node-wide Reticulum settings -- the [reticulum] and [logging] sections of
// the RNS config file. Per-interface options live in interface_dialog.dart;
// this one is the daemon-wide half.
//
// The option set, its grouping, its defaults and its tooltips all come from
// the backend (trenchchat/core/reticulum_config.py), so nothing here has to
// be kept in step with what RNS reads. Every field's "DEFAULT"/empty state
// means the key is absent from the config file and RNS uses its own default,
// which is why saving sends only the fields the user actually changed.
import 'package:flutter/material.dart';

import '../../api/client.dart';
import '../../api/models/reticulum_config.dart';
import '../../app_state.dart';
import '../../theme/section_theme.dart';
import '../../theme/theme_spec.dart';
import '../../theme/tokens.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_checkbox.dart';
import '../../widgets/tc_dialog.dart';
import '../../widgets/tc_text_field.dart';
import '../../widgets/tc_tooltip.dart';

/// The choice shown for an option the config file does not set.
const String kUnsetLabel = 'DEFAULT';

/// Pops `true` after a successful save so the caller can refresh.
Future<bool?> showReticulumConfigDialog(BuildContext context, AppState state) {
  return showTcDialog<bool>(
    context: context,
    builder: (context) => SectionTheme(
      spec: state.themeSpec,
      section: TCSection.dialogs,
      child: _ReticulumConfigDialogContent(state: state),
    ),
  );
}

class _ReticulumConfigDialogContent extends StatefulWidget {
  const _ReticulumConfigDialogContent({required this.state});

  final AppState state;

  @override
  State<_ReticulumConfigDialogContent> createState() =>
      _ReticulumConfigDialogContentState();
}

class _ReticulumConfigDialogContentState
    extends State<_ReticulumConfigDialogContent> {
  final Map<String, TextEditingController> _controllers = {};
  final Map<String, String> _initial = {};
  final Map<String, String> _selected = {};

  List<ReticulumOption>? _options;
  String? _error;
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void dispose() {
    for (final c in _controllers.values) {
      c.dispose();
    }
    super.dispose();
  }

  Future<void> _load() async {
    try {
      final options = await widget.state.api.getReticulumConfig();
      if (!mounted) return;
      for (final opt in options) {
        final value = _normalise(opt);
        _initial[opt.key] = value;
        if (opt.isBool || opt.isChoice) {
          _selected[opt.key] = value;
        } else {
          _controllers[opt.key] = TextEditingController(text: value);
        }
      }
      setState(() {
        _options = options;
        _error = null;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _options = const [];
        _error = e is ApiException ? e.message : e.toString();
      });
    }
  }

  /// The config file is hand-editable, so an option's stored value may not be
  /// in the form this editor writes. Map it onto one of the offered choices,
  /// falling back to unset for anything unrecognised.
  static String _normalise(ReticulumOption opt) {
    final raw = opt.value.trim();
    if (raw.isEmpty) return '';
    if (opt.isBool) {
      return ['yes', 'true', 'on', '1'].contains(raw.toLowerCase()) ? 'Yes' : 'No';
    }
    if (opt.isChoice) {
      final lowered = raw.toLowerCase();
      return opt.choices.contains(lowered) ? lowered : '';
    }
    return raw;
  }

  String _valueOf(ReticulumOption opt) => opt.isBool || opt.isChoice
      ? (_selected[opt.key] ?? '')
      : (_controllers[opt.key]?.text.trim() ?? '');

  Map<String, String> _changedValues() {
    final changed = <String, String>{};
    for (final opt in _options ?? const <ReticulumOption>[]) {
      final value = _valueOf(opt);
      if (value != (_initial[opt.key] ?? '')) changed[opt.key] = value;
    }
    return changed;
  }

  Future<void> _submit() async {
    final changed = _changedValues();
    if (changed.isEmpty) {
      Navigator.pop(context, false);
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await widget.state.api.setReticulumConfig(changed);
      if (!mounted) return;
      Navigator.pop(context, true);
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _busy = false;
        _error = e is ApiException ? e.message : e.toString();
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    final options = _options;
    return TcDialogShell(
      title: 'Reticulum Settings',
      width: 560,
      errorText: _error,
      actions: [
        TcGhostButton(label: 'CANCEL', onPressed: () => Navigator.pop(context)),
        if (options != null && options.isNotEmpty)
          TcPrimaryButton(
            label: _busy ? 'SAVING…' : 'SAVE',
            onPressed: _busy ? null : _submit,
          ),
      ],
      children: [
        Container(
          constraints: const BoxConstraints(maxHeight: 440),
          child: options == null
              ? _placeholder('LOADING…')
              : options.isEmpty
                  ? _placeholder('No node-wide settings available.')
                  : ListView(shrinkWrap: true, children: _buildRows(options)),
        ),
      ],
    );
  }

  Widget _placeholder(String text) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 24),
        child: Text(
          text,
          style: TextStyle(
            fontSize: TCType.textCaption,
            color: SectionTheme.of(context).textTertiary,
          ),
        ),
      );

  List<Widget> _buildRows(List<ReticulumOption> options) {
    final rows = <Widget>[];
    String category = '';
    for (final opt in options) {
      if (opt.category != category) {
        category = opt.category;
        if (rows.isNotEmpty) rows.add(const SizedBox(height: 6));
        rows.add(_fieldLabel(category.toUpperCase()));
        rows.add(const SizedBox(height: 8));
      }
      rows.add(_optionRow(opt));
      rows.add(const SizedBox(height: 10));
    }
    return rows;
  }

  /// The whole row sits inside the tooltip, so pointing anywhere at the option
  /// -- its label included -- explains what it does and what it costs.
  Widget _optionRow(ReticulumOption opt) {
    final label = '${opt.label} (?)';
    final Widget editor;
    if (opt.isBool) {
      editor = _choiceEditor(
          opt, label, const {'': kUnsetLabel, 'Yes': 'YES', 'No': 'NO'});
    } else if (opt.isChoice) {
      editor = _choiceEditor(opt, label, {
        '': kUnsetLabel,
        for (final c in opt.choices) c: c.toUpperCase(),
      });
    } else {
      editor = TcTextField(
        label: label,
        controller: _controllers[opt.key]!,
        hintText: 'default: ${opt.defaultValue}',
      );
    }
    return TcTooltip(message: opt.description, child: editor);
  }

  Widget _choiceEditor(
          ReticulumOption opt, String label, Map<String, String> options) =>
      Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _fieldLabel(label.toUpperCase()),
          const SizedBox(height: 6),
          TcChoiceRow(
            options: options,
            value: _selected[opt.key] ?? '',
            onSelected: (v) => setState(() => _selected[opt.key] = v),
          ),
        ],
      );

  Widget _fieldLabel(String label) => Text(
        label,
        style: TextStyle(
          fontSize: TCType.textCaption,
          color: SectionTheme.of(context).textSecondary,
          letterSpacing: TCType.letterSpacingFor(TCType.textCaption, TCType.trackingWide),
        ),
      );
}
