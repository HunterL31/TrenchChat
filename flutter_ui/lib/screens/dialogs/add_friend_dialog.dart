// Add/Edit Friend dialog -- one dialog serves both flows. Called with no
// identityHash for the manual-add path (hash editable); called with an
// identityHash from a context menu or the friends panel for add-with-known-
// hash or edit (hash pre-filled and read-only either way -- only the
// manual path lets the user type a hash).
//
// Two ways to reach a friendship, and they differ in who else has to act.
// ADD saves the contact here and tells nobody: right for a hash exchanged
// out of band, where the other side will add us too. REQUEST asks them,
// and their answer completes it. Direct messages need both sides either way
// -- each end only ever accepts messages from someone it holds itself.
import 'package:flutter/material.dart';

import '../../app_state.dart';
import '../../theme/section_theme.dart';
import '../../theme/theme_spec.dart';
import '../../theme/tokens.dart';
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
  // Sent to the peer with a request, unlike _note, which never leaves here.
  final _message = TextEditingController();
  String? _error;
  bool _busy = false;

  /// Which kind of hash the field holds. They are the same shape and one
  /// cannot be told from the other, so the user says which -- pasting an
  /// LXMF address as an identity hash addresses nothing at all.
  bool _isLxmfAddress = false;
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
    _message.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    final hash = _hash.text.trim();
    if (hash.isEmpty) {
      setState(() => _error = '${_isLxmfAddress ? "LXMF address" : "Identity "
          "hash"} cannot be empty.');
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    if (_isLxmfAddress && !_isEdit) {
      await _submitLxmfAddress(hash);
      return;
    }
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

  /// An LXMF address has to be resolved to an identity before it is a
  /// contact, which needs the peer's announce. Saying so beats a silent
  /// nothing when the peer has not been heard yet.
  Future<void> _submitLxmfAddress(String hash) async {
    final state = await widget.state.addLxmfAddress(
      hash,
      nickname: _nickname.text.trim(),
      note: _note.text.trim(),
    );
    if (!mounted) return;
    if (state == null) {
      setState(() {
        _busy = false;
        _error = widget.state.takeActionError() ?? 'Could not save contact.';
      });
      return;
    }
    if (state == 'resolving') {
      setState(() {
        _busy = false;
        _error = 'Looking for that address on the mesh. It becomes a contact '
            'once its announce arrives — try again in a moment.';
      });
      return;
    }
    Navigator.pop(context);
  }

  Future<void> _sendRequest() async {
    final hash = _hash.text.trim();
    if (hash.isEmpty) {
      setState(() => _error = 'Identity hash cannot be empty.');
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    final ok = await widget.state.sendFriendRequest(
      hash,
      note: _message.text.trim(),
      nickname: _nickname.text.trim(),
    );
    if (!mounted) return;
    if (!ok) {
      setState(() {
        _busy = false;
        _error = widget.state.takeActionError() ?? 'Could not send the request.';
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
        // A bot has no friend-request handshake to answer, so the ask is
        // meaningless for an address and hidden rather than left to fail.
        if (!_isEdit && !_isLxmfAddress)
          TcGhostButton(
            label: 'REQUEST',
            onPressed: _busy ? null : _sendRequest,
          ),
        TcPrimaryButton(
          label: _busy ? 'SAVING…' : (_isEdit ? 'SAVE' : 'ADD'),
          onPressed: _busy ? null : _submit,
        ),
      ],
      children: [
        TcTextField(
          label: _isLxmfAddress ? 'LXMF address' : 'Identity hash',
          controller: _hash,
          hintText: 'a1b2c3…',
          autofocus: !_hashReadOnly,
          readOnly: _hashReadOnly,
          onSubmitted: (_) => _submit(),
        ),
        if (!_isEdit) ...[
          const SizedBox(height: 6),
          _AddressKindToggle(
            isLxmf: _isLxmfAddress,
            onChanged: _busy
                ? null
                : (value) => setState(() {
                      _isLxmfAddress = value;
                      _error = null;
                    }),
          ),
        ],
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
          hintText: 'optional, stays on this device',
          onSubmitted: (_) => _submit(),
        ),
        if (!_isEdit) ...[
          const SizedBox(height: 12),
          TcTextField(
            label: 'Request message',
            controller: _message,
            hintText: 'optional, sent with the request',
            onSubmitted: (_) => _sendRequest(),
          ),
        ],
      ],
    );
  }
}


/// Which kind of hash was pasted. An LXMF address (what NomadNet, Sideband
/// and bots advertise) and an identity hash are both 32 hex characters, and
/// neither can be derived from the other, so nothing but the user can say
/// which one this is.
class _AddressKindToggle extends StatelessWidget {
  const _AddressKindToggle({required this.isLxmf, required this.onChanged});

  final bool isLxmf;
  final ValueChanged<bool>? onChanged;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    return Row(
      children: [
        for (final option in const [
          (label: 'IDENTITY HASH', lxmf: false),
          (label: 'LXMF ADDRESS', lxmf: true),
        ]) ...[
          GestureDetector(
            onTap: onChanged == null ? null : () => onChanged!(option.lxmf),
            child: Container(
              padding:
                  const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
              margin: const EdgeInsets.only(right: 6),
              decoration: BoxDecoration(
                color: isLxmf == option.lxmf ? tc.bgSelected : null,
                border: Border.all(
                    color: isLxmf == option.lxmf
                        ? tc.borderAccent
                        : tc.borderSubtle),
              ),
              child: Text(
                option.label,
                style: TextStyle(
                  fontSize: TCType.textMicro,
                  color: isLxmf == option.lxmf
                      ? tc.textEmphasis
                      : tc.textTertiary,
                ),
              ),
            ),
          ),
        ],
      ],
    );
  }
}
