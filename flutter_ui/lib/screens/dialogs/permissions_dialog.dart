// Channel permissions dialog -- port of invite_dialogs.py's
// ChannelPermissionsDialog role matrix over GET/POST /channels/{h}/permissions.
// The Qt dialog also edits the open-join/discoverable flags; the permissions
// endpoint doesn't expose those, so this port covers the per-role matrix. The
// caller gates on MANAGE_CHANNEL; edit_channel_permissions re-checks it
// server-side regardless.
import 'package:flutter/material.dart';

import '../../app_state.dart';
import '../../theme/tokens.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_checkbox.dart';
import '../../widgets/tc_dialog.dart';

const Map<String, String> _permissionLabels = {
  'send_message': 'Send messages',
  'invite': 'Invite members',
  'kick': 'Remove members',
  'manage_roles': 'Manage roles',
  'manage_channel': 'Manage channel settings',
  'create_channel': 'Create channels in this server',
  'full_sync': 'Full history sync',
  'voice_chat': 'Join voice chat',
};

Future<void> showPermissionsDialog(BuildContext context, AppState state,
    {required String channelHashHex, required String channelName}) {
  return showTcDialog<void>(
    context: context,
    builder: (context) => _PermissionsDialogContent(
      state: state,
      channelHashHex: channelHashHex,
      channelName: channelName,
    ),
  );
}

class _PermissionsDialogContent extends StatefulWidget {
  const _PermissionsDialogContent({
    required this.state,
    required this.channelHashHex,
    required this.channelName,
  });

  final AppState state;
  final String channelHashHex;
  final String channelName;

  @override
  State<_PermissionsDialogContent> createState() => _PermissionsDialogContentState();
}

class _PermissionsDialogContentState extends State<_PermissionsDialogContent> {
  List<String> _allPermissions = [];
  final Set<String> _admin = {};
  final Set<String> _member = {};
  List<String> _adminGrantable = const [];
  List<String> _memberGrantable = const [];

  bool _loading = true;
  bool _busy = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final perms = await widget.state.api.getChannelPermissions(widget.channelHashHex);
      if (!mounted) return;
      setState(() {
        _allPermissions = perms.allPermissions;
        _adminGrantable = perms.grantableFor('admin');
        _memberGrantable = perms.grantableFor('member');
        _admin.addAll(perms.admin);
        _member.addAll(perms.member);
        _loading = false;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _loading = false;
        _error = 'Could not load permissions: $e';
      });
    }
  }

  Future<void> _submit() async {
    setState(() {
      _busy = true;
      _error = null;
    });
    // Send only what each role may hold: the core drops the rest anyway, and
    // submitting a grant it will discard makes the dialog look like it worked.
    final ok = await widget.state.updateChannelPermissions(
        widget.channelHashHex,
        _admin.where(_adminGrantable.contains).toList(),
        _member.where(_memberGrantable.contains).toList());
    if (!mounted) return;
    if (!ok) {
      setState(() {
        _busy = false;
        _error = widget.state.actionError ?? 'The backend rejected the change.';
      });
      return;
    }
    Navigator.pop(context);
  }

  String _labelFor(String perm) => _permissionLabels[perm] ?? perm;

  @override
  Widget build(BuildContext context) {
    return TcDialogShell(
      title: 'Permissions — #${widget.channelName}',
      width: 440,
      errorText: _error,
      actions: [
        TcGhostButton(label: 'CANCEL', onPressed: () => Navigator.pop(context)),
        TcPrimaryButton(
          label: _busy ? 'SAVING…' : 'SAVE',
          onPressed: _busy || _loading ? null : _submit,
        ),
      ],
      children: [
        if (_loading)
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 24),
            child: Center(
              child: Text(
                'LOADING…',
                style: TextStyle(fontSize: TCType.textCaption, color: TCColors.textTertiary),
              ),
            ),
          )
        else
          Container(
            constraints: const BoxConstraints(maxHeight: 420),
            child: ListView(
              shrinkWrap: true,
              children: [
                _roleLabel('OWNER', note: 'always has all permissions'),
                const SizedBox(height: 6),
                for (final perm in _allPermissions)
                  Padding(
                    padding: const EdgeInsets.symmetric(vertical: 3),
                    child: TcCheckbox(value: true, label: _labelFor(perm), onChanged: null),
                  ),
                const SizedBox(height: 12),
                _roleLabel('ADMIN'),
                const SizedBox(height: 6),
                for (final perm in _adminGrantable)
                  Padding(
                    padding: const EdgeInsets.symmetric(vertical: 3),
                    child: TcCheckbox(
                      value: _admin.contains(perm),
                      label: _labelFor(perm),
                      onChanged: (v) =>
                          setState(() => v ? _admin.add(perm) : _admin.remove(perm)),
                    ),
                  ),
                const SizedBox(height: 12),
                _roleLabel('MEMBER'),
                const SizedBox(height: 6),
                for (final perm in _memberGrantable)
                  Padding(
                    padding: const EdgeInsets.symmetric(vertical: 3),
                    child: TcCheckbox(
                      value: _member.contains(perm),
                      label: _labelFor(perm),
                      onChanged: (v) =>
                          setState(() => v ? _member.add(perm) : _member.remove(perm)),
                    ),
                  ),
                const SizedBox(height: 10),
                Text(
                  'Changes take effect immediately for this device and are '
                  'broadcast to other members.',
                  style: TextStyle(fontSize: TCType.textCaption, color: TCColors.textTertiary),
                ),
              ],
            ),
          ),
      ],
    );
  }

  Widget _roleLabel(String role, {String? note}) => Row(
        children: [
          Text(
            role,
            style: TextStyle(
              fontSize: TCType.textCaption,
              color: TCColors.accentPrimary,
              letterSpacing:
                  TCType.letterSpacingFor(TCType.textCaption, TCType.trackingWider),
            ),
          ),
          if (note != null) ...[
            const SizedBox(width: 8),
            Text(
              note,
              style: TextStyle(fontSize: TCType.textMicro, color: TCColors.textTertiary),
            ),
          ],
        ],
      );
}
