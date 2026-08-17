// New Server dialog -- name + description, no access choice (servers are
// always invite-only). Mirrors trenchchat/gui/main_window.py's
// NewServerDialog.
import 'package:flutter/material.dart';

import '../../app_state.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_dialog.dart';
import '../../widgets/tc_text_field.dart';

Future<void> showNewServerDialog(BuildContext context, AppState state) {
  return showTcDialog<void>(
    context: context,
    builder: (context) => _NewServerDialogContent(state: state),
  );
}

class _NewServerDialogContent extends StatefulWidget {
  const _NewServerDialogContent({required this.state});
  final AppState state;

  @override
  State<_NewServerDialogContent> createState() => _NewServerDialogContentState();
}

class _NewServerDialogContentState extends State<_NewServerDialogContent> {
  final _name = TextEditingController();
  final _desc = TextEditingController();
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
      setState(() => _error = 'Server name cannot be empty.');
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    final hash = await widget.state.createServer(name, _desc.text.trim());
    if (!mounted) return;
    if (hash == null) {
      setState(() {
        _busy = false;
        _error = widget.state.actionError ?? 'Could not create server.';
      });
      return;
    }
    Navigator.pop(context);
  }

  @override
  Widget build(BuildContext context) {
    return TcDialogShell(
      title: 'New Server',
      errorText: _error,
      actions: [
        TcGhostButton(label: 'CANCEL', onPressed: () => Navigator.pop(context)),
        TcPrimaryButton(label: _busy ? 'CREATING…' : 'CREATE', onPressed: _busy ? null : _submit),
      ],
      children: [
        TcTextField(
          label: 'Name',
          controller: _name,
          hintText: 'my-server',
          autofocus: true,
          onSubmitted: (_) => _submit(),
        ),
        const SizedBox(height: 12),
        TcTextField(label: 'Description', controller: _desc, onSubmitted: (_) => _submit()),
      ],
    );
  }
}
