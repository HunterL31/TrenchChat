// Members dialog -- port of invite_dialogs.py's MembersDialog. Kick and
// admin-toggle controls are gated on GET /channels/{h}/my_permissions the
// same way the Qt dialog gates on has_permission(); the backend's
// update_membership re-checks regardless. Unlike the Qt dialog, actions
// apply immediately per row instead of batching until close -- the /roles
// endpoint takes one call per change.
import 'package:flutter/material.dart';

import '../../api/models/member.dart';
import '../../app_state.dart';
import '../../theme/section_theme.dart';
import '../../theme/theme_spec.dart';
import '../../theme/tokens.dart';
import '../../widgets/status_dot.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_dialog.dart';
import 'invite_dialog.dart';
import 'permissions_dialog.dart';

const _roleOwner = 'owner';
const _roleAdmin = 'admin';

Future<void> showMembersDialog(BuildContext context, AppState state,
    {required String channelHashHex, required String channelName}) {
  return showTcDialog<void>(
    context: context,
    builder: (context) => SectionTheme(
      spec: state.themeSpec,
      section: TCSection.dialogs,
      child: _MembersDialogContent(
        state: state,
        channelHashHex: channelHashHex,
        channelName: channelName,
      ),
    ),
  );
}

class _MembersDialogContent extends StatefulWidget {
  const _MembersDialogContent({
    required this.state,
    required this.channelHashHex,
    required this.channelName,
  });

  final AppState state;
  final String channelHashHex;
  final String channelName;

  @override
  State<_MembersDialogContent> createState() => _MembersDialogContentState();
}

class _MembersDialogContentState extends State<_MembersDialogContent> {
  String? _error;
  String? _confirmKickHash;
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    // Refresh from the backend so the list isn't stale; the dialog renders
    // whatever AppState already holds in the meantime.
    widget.state.loadChannel(widget.channelHashHex);
  }

  List<Member> get _members =>
      widget.state.membersByChannel[widget.channelHashHex] ?? [];

  bool _isOnline(String identityHash) {
    final presence = widget.state.presenceByChannel[widget.channelHashHex] ?? [];
    for (final p in presence) {
      if (p.identityHash == identityHash) return p.isOnline;
    }
    return false;
  }

  String _label(Member m) {
    if (m.displayName.isNotEmpty) return m.displayName;
    final resolved = widget.state.resolvePeerName(m.identityHash);
    if (resolved != null && resolved.isNotEmpty) return resolved;
    final h = m.identityHash;
    return h.length > 12 ? '${h.substring(0, 12)}…' : h;
  }

  Future<void> _apply({
    List<String> removeMembers = const [],
    List<String> addAdmins = const [],
    List<String> removeAdmins = const [],
  }) async {
    setState(() {
      _busy = true;
      _error = null;
      _confirmKickHash = null;
    });
    final ok = await widget.state.updateChannelRoles(
      widget.channelHashHex,
      removeMembers: removeMembers,
      addAdmins: addAdmins,
      removeAdmins: removeAdmins,
    );
    if (!mounted) return;
    setState(() {
      _busy = false;
      if (!ok) {
        _error = widget.state.takeActionError() ?? 'The backend rejected the change.';
      }
    });
  }

  @override
  Widget build(BuildContext context) {
    final state = widget.state;
    final tc = SectionTheme.of(context);
    final perms = state.permissionsByChannel[widget.channelHashHex];
    final canKick = perms?.kick ?? false;
    final canManageRoles = perms?.manageRoles ?? false;

    return AnimatedBuilder(
      animation: state,
      builder: (context, _) => TcDialogShell(
        title: 'Members \u00b7 #${widget.channelName}',
        width: 440,
        errorText: _error,
        actions: [
          if (perms?.manageChannel ?? false)
            TcGhostButton(
              label: 'PERMS',
              onPressed: () => showPermissionsDialog(
                context,
                state,
                channelHashHex: widget.channelHashHex,
                channelName: widget.channelName,
              ),
            ),
          if (perms?.invite ?? false)
            TcGhostButton(
              label: 'INVITE',
              onPressed: () => showInviteDialog(
                context,
                state,
                channelHashHex: widget.channelHashHex,
                channelName: widget.channelName,
              ),
            ),
          TcGhostButton(label: 'CLOSE', onPressed: () => Navigator.pop(context)),
        ],
        children: [
          if (_members.isEmpty)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 16),
              child: Text(
                'No members known yet.',
                style: TextStyle(fontSize: TCType.textBodySm, color: tc.textTertiary),
              ),
            )
          else
            Container(
              constraints: const BoxConstraints(maxHeight: 280),
              child: ListView(
                shrinkWrap: true,
                padding: EdgeInsets.only(right: scrollbarInset(context)),
                children: [
                  for (final m in _members)
                    _memberRow(m, canKick: canKick, canManageRoles: canManageRoles),
                ],
              ),
            ),
        ],
      ),
    );
  }

  Widget _memberRow(Member m, {required bool canKick, required bool canManageRoles}) {
    final tc = SectionTheme.of(context);
    final isSelf = m.identityHash == widget.state.meHashHex;
    final isOwner = m.role == _roleOwner;
    final actionable = !isSelf && !isOwner && !_busy;
    final confirming = _confirmKickHash == m.identityHash;

    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: ConstrainedBox(
        constraints: const BoxConstraints(minHeight: tcControlHeight),
        child: Row(
          children: [
            StatusDot(
              status: _isOnline(m.identityHash) ? PresenceStatus.online : PresenceStatus.offline,
              ringColor: tc.bgSurfaceRaised,
            ),
            const SizedBox(width: TCSpace.space2),
            Expanded(
              child: Text(
                '${_label(m)}${isSelf ? '  (you)' : ''}',
                overflow: TextOverflow.ellipsis,
                style: TextStyle(
                  fontSize: TCType.textBodySm,
                  color: isSelf ? tc.textEmphasis : tc.textSecondary,
                ),
              ),
            ),
            if (isOwner || m.role == _roleAdmin) ...[
              const SizedBox(width: 6),
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 5, vertical: 1),
                decoration: BoxDecoration(
                  color: tc.bgInset,
                  border: Border.all(color: tc.borderDefault),
                ),
                child: Text(
                  m.role.toUpperCase(),
                  style: TextStyle(
                    fontSize: TCType.textMicro,
                    color: isOwner ? tc.accentSecondary : tc.accentPrimary,
                    letterSpacing:
                        TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWide),
                  ),
                ),
              ),
            ],
            if (actionable && canManageRoles) ...[
              const SizedBox(width: 6),
              TcGhostButton(
                label: m.role == _roleAdmin ? '-ADMIN' : '+ADMIN',
                onPressed: confirming
                    ? null
                    : () => m.role == _roleAdmin
                        ? _apply(removeAdmins: [m.identityHash])
                        : _apply(addAdmins: [m.identityHash]),
              ),
            ],
            if (actionable && canKick) ...[
              const SizedBox(width: 6),
              if (confirming) ...[
                Padding(
                  padding: const EdgeInsets.symmetric(vertical: 7),
                  child: Text(
                    'KICK?',
                    style: TextStyle(fontSize: TCType.textCaption, color: tc.statusDanger),
                  ),
                ),
                const SizedBox(width: 6),
                TcGhostButton(
                  label: 'YES',
                  onPressed: () => _apply(removeMembers: [m.identityHash]),
                ),
                const SizedBox(width: 6),
                TcGhostButton(
                  label: 'NO',
                  onPressed: () => setState(() => _confirmKickHash = null),
                ),
              ] else
                TcGhostButton(
                  label: 'KICK',
                  onPressed: () => setState(() => _confirmKickHash = m.identityHash),
                ),
            ],
          ],
        ),
      ),
    );
  }
}
