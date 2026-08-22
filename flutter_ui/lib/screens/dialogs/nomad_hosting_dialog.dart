// Nomad node hosting dialog -- enable/disable serving our own pages, name
// the node, and see what the pages directory currently serves. The backend
// scans DATA_DIR/nomad_pages/{pages,files}; RESCAN picks up edits without a
// restart. Pages starting with #! are served as plain text, never executed.
import 'package:flutter/material.dart';

import '../../api/client.dart';
import '../../api/models/nomad.dart';
import '../../app_state.dart';
import '../../theme/section_theme.dart';
import '../../theme/theme_spec.dart';
import '../../theme/tokens.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_checkbox.dart';
import '../../widgets/tc_dialog.dart';
import '../../widgets/tc_text_field.dart';

Future<void> showNomadHostingDialog(BuildContext context, AppState state) {
  return showTcDialog<void>(
    context: context,
    builder: (context) => SectionTheme(
      spec: state.themeSpec,
      section: TCSection.dialogs,
      child: _NomadHostingContent(state: state),
    ),
  );
}

class _NomadHostingContent extends StatefulWidget {
  const _NomadHostingContent({required this.state});

  final AppState state;

  @override
  State<_NomadHostingContent> createState() => _NomadHostingContentState();
}

class _NomadHostingContentState extends State<_NomadHostingContent> {
  final _name = TextEditingController();
  NomadHosting? _hosting;
  bool _enabled = false;
  bool _busy = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void dispose() {
    _name.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    try {
      final hosting = await widget.state.api.getNomadHosting();
      if (!mounted) return;
      setState(() {
        _hosting = hosting;
        _enabled = hosting.enabled;
        _name.text = hosting.nodeName;
        _error = null;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() => _error = e is ApiException ? e.message : e.toString());
    }
  }

  Future<void> _apply({required Future<NomadHosting> Function() call}) async {
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final hosting = await call();
      if (!mounted) return;
      setState(() {
        _hosting = hosting;
        _enabled = hosting.enabled;
        _name.text = hosting.nodeName;
        _busy = false;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _busy = false;
        _error = e is ApiException ? e.message : e.toString();
      });
    }
  }

  Future<void> _save() => _apply(
      call: () => widget.state.api.setNomadHosting(
          enabled: _enabled, nodeName: _name.text.trim()));

  Future<void> _rescan() =>
      _apply(call: () => widget.state.api.refreshNomadHosting());

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final hosting = _hosting;
    return TcDialogShell(
      title: 'Host Pages',
      errorText: _error,
      actions: [
        TcGhostButton(label: 'CLOSE', onPressed: () => Navigator.pop(context)),
        TcPrimaryButton(
          label: _busy ? 'SAVING…' : 'APPLY',
          onPressed: _busy ? null : _save,
        ),
      ],
      children: [
        TcCheckbox(
          label: 'Serve pages as a Nomad Network node',
          value: _enabled,
          onChanged: (value) => setState(() => _enabled = value),
        ),
        const SizedBox(height: 12),
        TcTextField(
          label: 'Node name',
          controller: _name,
          hintText: 'shown to browsers on the mesh',
          onSubmitted: (_) => _save(),
        ),
        if (hosting != null) ...[
          const SizedBox(height: 14),
          Text(
            'PAGES DIRECTORY',
            style: TextStyle(
              fontSize: TCType.textMicro,
              color: tc.textTertiary,
              letterSpacing:
                  TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWide),
            ),
          ),
          const SizedBox(height: 4),
          SelectableText(
            hosting.pagesDir,
            style:
                TextStyle(fontSize: TCType.textCaption, color: tc.textSecondary),
          ),
          const SizedBox(height: 10),
          Row(
            children: [
              Expanded(
                child: Text(
                  'SERVING ${hosting.pages.length} PAGE(S), '
                  '${hosting.files.length} FILE(S)',
                  style: TextStyle(
                    fontSize: TCType.textMicro,
                    color: tc.textTertiary,
                    letterSpacing: TCType.letterSpacingFor(
                        TCType.textMicro, TCType.trackingWide),
                  ),
                ),
              ),
              TcGhostButton(
                  label: 'RESCAN', onPressed: _busy ? null : _rescan),
            ],
          ),
          const SizedBox(height: 4),
          ConstrainedBox(
            constraints: const BoxConstraints(maxHeight: 160),
            child: ListView(
              shrinkWrap: true,
              children: [
                for (final entry in [...hosting.pages, ...hosting.files])
                  Padding(
                    padding: const EdgeInsets.symmetric(vertical: 2),
                    child: Row(
                      children: [
                        Expanded(
                          child: Text(
                            entry.path,
                            overflow: TextOverflow.ellipsis,
                            style: TextStyle(
                                fontSize: TCType.textCaption,
                                color: tc.textSecondary),
                          ),
                        ),
                        Text(
                          '${entry.size} B',
                          style: TextStyle(
                              fontSize: TCType.textCaption,
                              color: tc.textTertiary),
                        ),
                      ],
                    ),
                  ),
              ],
            ),
          ),
        ],
      ],
    );
  }
}
