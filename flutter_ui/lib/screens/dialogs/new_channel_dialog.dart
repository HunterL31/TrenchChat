// New Channel dialog -- offers an access preset (public/invite-only) for a
// standalone channel; a channel created inside a server inherits the
// server's permissions instead, so the preset picker is omitted there.
// Mirrors trenchchat/gui/main_window.py's NewChannelDialog(in_server=...).
import 'package:flutter/material.dart';

import '../../app_state.dart';
import '../../theme/effects.dart';
import '../../theme/section_theme.dart';
import '../../theme/theme_spec.dart';
import '../../theme/tokens.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_dialog.dart';
import '../../widgets/tc_text_field.dart';

/// [serverHashHex] null creates a standalone channel (with an access preset
/// picker); non-null creates a channel inside that server (no picker --
/// inherits the server's permissions).
Future<void> showNewChannelDialog(BuildContext context, AppState state,
    {String? serverHashHex}) {
  return showTcDialog<void>(
    context: context,
    builder: (context) => SectionTheme(
      spec: state.themeSpec,
      section: TCSection.dialogs,
      child: _NewChannelDialogContent(state: state, serverHashHex: serverHashHex),
    ),
  );
}

class _NewChannelDialogContent extends StatefulWidget {
  const _NewChannelDialogContent({required this.state, required this.serverHashHex});
  final AppState state;
  final String? serverHashHex;

  @override
  State<_NewChannelDialogContent> createState() => _NewChannelDialogContentState();
}

class _NewChannelDialogContentState extends State<_NewChannelDialogContent> {
  final _name = TextEditingController();
  final _desc = TextEditingController();
  String _access = 'public';
  String? _error;
  bool _busy = false;

  @override
  void dispose() {
    _name.dispose();
    _desc.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    final name = _name.text.trim();
    if (name.isEmpty) {
      setState(() => _error = 'Channel name cannot be empty.');
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    final serverHash = widget.serverHashHex;
    final hash = serverHash != null
        ? await widget.state.createChannelInServer(serverHash, name, _desc.text.trim())
        : await widget.state.createStandaloneChannel(name, _desc.text.trim(), _access);
    if (!mounted) return;
    if (hash == null) {
      setState(() {
        _busy = false;
        _error = widget.state.takeActionError() ?? 'Could not create channel.';
      });
      return;
    }
    Navigator.pop(context);
  }

  @override
  Widget build(BuildContext context) {
    final inServer = widget.serverHashHex != null;
    final tc = SectionTheme.of(context);
    return TcDialogShell(
      title: inServer ? 'New Channel in Server' : 'New Channel',
      errorText: _error,
      actions: [
        TcGhostButton(label: 'CANCEL', onPressed: () => Navigator.pop(context)),
        TcPrimaryButton(label: _busy ? 'CREATING…' : 'CREATE', onPressed: _busy ? null : _submit),
      ],
      children: [
        TcTextField(
          label: 'Name',
          controller: _name,
          hintText: 'general',
          autofocus: true,
          onSubmitted: (_) => _submit(),
        ),
        const SizedBox(height: 12),
        TcTextField(
          label: 'Description',
          controller: _desc,
          onSubmitted: (_) => _submit(),
        ),
        if (!inServer) ...[
          const SizedBox(height: 12),
          Text(
            'ACCESS',
            style: TextStyle(
              fontSize: TCType.textCaption,
              color: tc.textSecondary,
              letterSpacing: TCType.letterSpacingFor(TCType.textCaption, TCType.trackingWide),
            ),
          ),
          const SizedBox(height: 6),
          Row(
            children: [
              _AccessOption(
                label: 'PUBLIC',
                value: 'public',
                groupValue: _access,
                onSelected: (v) => setState(() => _access = v),
              ),
              const SizedBox(width: 6),
              _AccessOption(
                label: 'INVITE-ONLY',
                value: 'invite',
                groupValue: _access,
                onSelected: (v) => setState(() => _access = v),
              ),
            ],
          ),
        ],
      ],
    );
  }
}

class _AccessOption extends StatelessWidget {
  const _AccessOption({
    required this.label,
    required this.value,
    required this.groupValue,
    required this.onSelected,
  });

  final String label;
  final String value;
  final String groupValue;
  final ValueChanged<String> onSelected;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final selected = value == groupValue;
    return GestureDetector(
      onTap: () => onSelected(value),
      child: MouseRegion(
        cursor: SystemMouseCursors.click,
        child: AnimatedContainer(
          duration: TCEffects.durationMed,
          curve: TCEffects.easeTerminal,
          padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
          decoration: BoxDecoration(
            color: selected ? tc.bgSelected : Colors.transparent,
            border: Border.all(color: selected ? tc.borderAccent : tc.borderDefault),
          ),
          child: Text(
            label,
            style: TextStyle(
              fontSize: TCType.textCaption,
              letterSpacing: TCType.letterSpacingFor(TCType.textCaption, TCType.trackingWide),
              color: selected ? tc.textEmphasis : tc.textSecondary,
            ),
          ),
        ),
      ),
    );
  }
}
