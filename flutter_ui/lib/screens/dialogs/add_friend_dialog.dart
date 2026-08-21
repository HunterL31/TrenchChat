// Add/Edit Friend dialog -- one dialog serves both flows. Called with no
// identityHash for the manual-add path (hash editable); called with an
// identityHash from a context menu or the friends panel for add-with-known-
// hash or edit (hash pre-filled and read-only either way -- only the
// manual path lets the user type a hash).
import 'package:flutter/material.dart';

import '../../app_state.dart';
import '../../theme/section_theme.dart';
import '../../theme/theme_spec.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_dialog.dart';
import '../../widgets/tc_text_field.dart';

Future<void> showAddFriendDialog(
  BuildContext context,
  AppState state, {
  String? identityHash,
}) {
  return showTcDialog<void>(
    context: context,
    builder: (context) => SectionTheme(
      spec: state.themeSpec,
      section: TCSection.dialogs,
      child: _AddFriendDialogContent(state: state, identityHash: identityHash),
    ),
  );
}

class _AddFriendDialogContent extends StatefulWidget {
  const _AddFriendDialogContent({required this.state, this.identityHash});

  final AppState state;
  final String? identityHash;

  @override
  State<_AddFriendDialogContent> createState() => _AddFriendDialogContentState();
}

class _AddFriendDialogContentState extends State<_AddFriendDialogContent> {
  late final TextEditingController _hash;
  final _nickname = TextEditingController();
  final _note = TextEditingController();
  String? _error;
  bool _busy = false;
  late final bool _isEdit;

  bool get _hashReadOnly => widget.identityHash != null;

  @override
  void initState() {
    super.initState();
    _hash = TextEditingController(text: widget.identityHash ?? '');
    _isEdit = widget.identityHash != null &&
        widget.state.friends.any((f) => f.identityHash == widget.identityHash);
    if (_isEdit) {
      final existing =
          widget.state.friends.firstWhere((f) => f.identityHash == widget.identityHash);
      _nickname.text = existing.nickname;
      _note.text = existing.note;
    }
  }

  @override
  void dispose() {
    _hash.dispose();
    _nickname.dispose();
    _note.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    final hash = _hash.text.trim();
    if (hash.isEmpty) {
      setState(() => _error = 'Identity hash cannot be empty.');
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    final ok = _isEdit
        ? await widget.state
            .updateFriend(hash, nickname: _nickname.text.trim(), note: _note.text.trim())
        : await widget.state.addFriend(hash, _nickname.text.trim(), _note.text.trim());
    if (!mounted) return;
    if (!ok) {
      setState(() {
        _busy = false;
        _error = widget.state.takeActionError() ?? 'Could not save friend.';
      });
      return;
    }
    Navigator.pop(context);
  }

  @override
  Widget build(BuildContext context) {
    return TcDialogShell(
      title: _isEdit ? 'Edit Friend' : 'Add Friend',
      errorText: _error,
      actions: [
        TcGhostButton(label: 'CANCEL', onPressed: () => Navigator.pop(context)),
        TcPrimaryButton(
          label: _busy ? 'SAVING…' : (_isEdit ? 'SAVE' : 'ADD'),
          onPressed: _busy ? null : _submit,
        ),
      ],
      children: [
        TcTextField(
          label: 'Identity hash',
          controller: _hash,
          hintText: 'a1b2c3…',
          autofocus: !_hashReadOnly,
          readOnly: _hashReadOnly,
          onSubmitted: (_) => _submit(),
        ),
        const SizedBox(height: 12),
        TcTextField(
          label: 'Nickname',
          controller: _nickname,
          hintText: 'optional',
          autofocus: _hashReadOnly,
          onSubmitted: (_) => _submit(),
        ),
        const SizedBox(height: 12),
        TcTextField(
          label: 'Note',
          controller: _note,
          hintText: 'optional',
          onSubmitted: (_) => _submit(),
        ),
      ],
    );
  }
}
