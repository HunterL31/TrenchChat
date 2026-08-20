// Incoming invite dialog -- port of invite_dialogs.py's IncomingInviteDialog:
// who invited you, to what, until when, with Accept/Decline. Reached from the
// INVITES section in the channel column rather than popping up on arrival.
import 'package:flutter/material.dart';

import '../../api/models/invite.dart';
import '../../app_state.dart';
import '../../theme/section_theme.dart';
import '../../theme/theme_spec.dart';
import '../../theme/tokens.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_dialog.dart';

Future<void> showIncomingInviteDialog(
    BuildContext context, AppState state, PendingInvite invite) {
  return showTcDialog<void>(
    context: context,
    builder: (context) => SectionTheme(
      spec: state.themeSpec,
      section: TCSection.dialogs,
      child: _IncomingInviteDialogContent(state: state, invite: invite),
    ),
  );
}

class _IncomingInviteDialogContent extends StatefulWidget {
  const _IncomingInviteDialogContent({required this.state, required this.invite});

  final AppState state;
  final PendingInvite invite;

  @override
  State<_IncomingInviteDialogContent> createState() =>
      _IncomingInviteDialogContentState();
}

class _IncomingInviteDialogContentState extends State<_IncomingInviteDialogContent> {
  String? _error;
  bool _busy = false;

  String get _expiryLabel {
    final remaining = widget.invite.expiry -
        DateTime.now().millisecondsSinceEpoch / 1000.0;
    if (remaining <= 0) return 'expired';
    final hours = remaining ~/ 3600;
    if (hours >= 48) return 'expires in ${hours ~/ 24} days';
    if (hours >= 1) return 'expires in $hours h';
    return 'expires in ${(remaining / 60).ceil()} min';
  }

  Future<void> _accept() async {
    setState(() {
      _busy = true;
      _error = null;
    });
    final ok = await widget.state.acceptInvite(widget.invite.channelHashHex);
    if (!mounted) return;
    if (!ok) {
      setState(() {
        _busy = false;
        _error = widget.state.actionError ?? 'Could not accept the invite.';
      });
      return;
    }
    Navigator.pop(context);
  }

  Future<void> _decline() async {
    setState(() => _busy = true);
    await widget.state.declineInvite(widget.invite.channelHashHex);
    if (!mounted) return;
    Navigator.pop(context);
  }

  @override
  Widget build(BuildContext context) {
    final invite = widget.invite;
    final tc = SectionTheme.of(context);
    final scopeLabel = invite.scopeKind == 'server' ? 'server' : 'channel';
    return TcDialogShell(
      title: 'Invite — ${invite.scopeKind == 'server' ? '' : '#'}${invite.channelName}',
      width: 400,
      errorText: _error,
      actions: [
        TcGhostButton(label: 'DECLINE', onPressed: _busy ? null : _decline),
        TcPrimaryButton(label: _busy ? 'JOINING…' : 'ACCEPT', onPressed: _busy ? null : _accept),
      ],
      children: [
        Text(
          'You have been invited to the $scopeLabel '
          '${invite.scopeKind == 'server' ? '' : '#'}${invite.channelName}.',
          style: TextStyle(fontSize: TCType.textBodySm, color: tc.textSecondary),
        ),
        const SizedBox(height: 10),
        Text(
          'INVITED BY',
          style: TextStyle(
            fontSize: TCType.textMicro,
            color: tc.textSecondary,
            letterSpacing: TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWide),
          ),
        ),
        const SizedBox(height: 4),
        Text(
          invite.adminHex,
          style: TextStyle(fontSize: TCType.textBodySm, color: tc.textTertiary),
        ),
        const SizedBox(height: 8),
        Text(
          _expiryLabel.toUpperCase(),
          style: TextStyle(
            fontSize: TCType.textMicro,
            color: tc.accentSecondary,
            letterSpacing: TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWide),
          ),
        ),
      ],
    );
  }
}
