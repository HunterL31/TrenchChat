// Invite dialog -- port of invite_dialogs.py's InviteDialog: a searchable
// list of peers from GET /directory plus the manual hex-entry fallback for
// peers whose announces haven't been heard yet. The Qt dialog's 5s auto
// refresh is replaced by re-querying on every keystroke; the directory is a
// local table, so per-keystroke queries are cheap.
import 'package:flutter/material.dart';

import '../../api/client.dart';
import '../../api/models/invite.dart';
import '../../app_state.dart';
import '../../theme/section_theme.dart';
import '../../theme/theme_spec.dart';
import '../../theme/tokens.dart';
import '../../widgets/status_dot.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_dialog.dart';
import '../../widgets/tc_text_field.dart';

const _identityHashHexLength = 32;

Future<void> showInviteDialog(BuildContext context, AppState state,
    {required String channelHashHex, required String channelName}) {
  return _showInviteDialog(
    context,
    state,
    title: 'Invite to #$channelName',
    onSubmit: (peer) => state.inviteToChannel(channelHashHex, peer),
  );
}

/// The same peer-picker, targeting a server instead of a channel.
Future<void> showServerInviteDialog(BuildContext context, AppState state,
    {required String serverHashHex, required String serverName}) {
  return _showInviteDialog(
    context,
    state,
    title: 'Invite to $serverName',
    onSubmit: (peer) => state.inviteToServer(serverHashHex, peer),
  );
}

Future<void> _showInviteDialog(BuildContext context, AppState state,
    {required String title, required Future<bool> Function(String) onSubmit}) {
  return showTcDialog<void>(
    context: context,
    builder: (context) => SectionTheme(
      spec: state.themeSpec,
      section: TCSection.dialogs,
      child: _InviteDialogContent(
        state: state,
        title: title,
        onSubmit: onSubmit,
      ),
    ),
  );
}

class _InviteDialogContent extends StatefulWidget {
  const _InviteDialogContent({
    required this.state,
    required this.title,
    required this.onSubmit,
  });

  final AppState state;
  final String title;
  final Future<bool> Function(String) onSubmit;

  @override
  State<_InviteDialogContent> createState() => _InviteDialogContentState();
}

class _InviteDialogContentState extends State<_InviteDialogContent> {
  final _search = TextEditingController();
  final _manualHash = TextEditingController();

  String? _selectedHash;
  String? _error;
  bool _busy = false;
  String _scope = directoryScopeAll;

  List<DirectoryEntry> get _entries => widget.state.directory;

  @override
  void initState() {
    super.initState();
    _search.addListener(_onSearchChanged);
    _manualHash.addListener(() => setState(() {}));
    _query('');
  }

  @override
  void dispose() {
    _search.dispose();
    _manualHash.dispose();
    super.dispose();
  }

  void _onSearchChanged() => _query(_search.text.trim());

  Future<void> _query(String q) async {
    await widget.state.loadDirectory(q, scope: _scope);
    if (!mounted) return;
    setState(() {
      if (_selectedHash != null &&
          !_entries.any((e) => e.identityHash == _selectedHash)) {
        _selectedHash = null;
      }
    });
  }

  void _setScope(String scope) {
    if (_scope == scope) return;
    setState(() => _scope = scope);
    _query(_search.text.trim());
  }

  String get _emptyMessage {
    switch (_scope) {
      case directoryScopeFriends:
        return 'No accepted friends match. Add a friend to invite them here.';
      case directoryScopeShared:
        return 'No users from your channels match.';
      default:
        return 'No TrenchChat users discovered yet. Users appear here '
            'once their announce has been received.';
    }
  }

  String get _manualNormalized =>
      _manualHash.text.trim().toLowerCase().replaceAll(' ', '');

  bool get _manualValid =>
      _manualNormalized.length == _identityHashHexLength &&
      RegExp(r'^[0-9a-f]+$').hasMatch(_manualNormalized);

  String? get _inviteeHash =>
      _selectedHash ?? (_manualValid ? _manualNormalized : null);

  bool get _isSelf {
    final invitee = _inviteeHash;
    return invitee != null &&
        invitee.toLowerCase() == widget.state.meHashHex.toLowerCase();
  }

  Future<void> _submit() async {
    final invitee = _inviteeHash;
    if (invitee == null) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    final ok = await widget.onSubmit(invitee);
    if (!mounted) return;
    if (!ok) {
      setState(() {
        _busy = false;
        _error = widget.state.takeActionError() ?? 'Could not send the invite.';
      });
      return;
    }
    Navigator.pop(context);
  }

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: widget.state,
      builder: (context, _) => _buildContent(context),
    );
  }

  Widget _buildContent(BuildContext context) {
    final tc = SectionTheme.of(context);
    return TcDialogShell(
      title: widget.title,
      width: 440,
      errorText: _error,
      actions: [
        TcGhostButton(label: 'CANCEL', onPressed: () => Navigator.pop(context)),
        TcPrimaryButton(
          label: _busy ? 'INVITING…' : 'INVITE',
          onPressed: _busy || _inviteeHash == null || _isSelf ? null : _submit,
        ),
      ],
      children: [
        TcTextField(
          label: 'Search discovered users',
          controller: _search,
          hintText: 'Type a name or hash to filter…',
          autofocus: true,
        ),
        const SizedBox(height: 8),
        Row(
          children: [
            _scopeChip('ALL', directoryScopeAll),
            const SizedBox(width: 6),
            _scopeChip('FRIENDS', directoryScopeFriends),
            const SizedBox(width: 6),
            _scopeChip('SHARED', directoryScopeShared),
          ],
        ),
        const SizedBox(height: 8),
        Container(
          height: 150,
          decoration: BoxDecoration(
            color: tc.bgInset,
            border: Border.all(color: tc.borderDefault),
          ),
          child: _entries.isEmpty
              ? Center(
                  child: Padding(
                    padding: const EdgeInsets.all(12),
                    child: Text(
                      _emptyMessage,
                      textAlign: TextAlign.center,
                      style: TextStyle(
                          fontSize: TCType.textCaption, color: tc.textTertiary),
                    ),
                  ),
                )
              : ListView(
                  children: [for (final e in _entries) _entryRow(e)],
                ),
        ),
        const SizedBox(height: 12),
        TcTextField(
          label: 'Or enter identity hash manually',
          controller: _manualHash,
          hintText: 'e.g. a3f1c2d4e5b6a7f8…  (hex, 32 chars)',
        ),
        if (_isSelf) ...[
          const SizedBox(height: 8),
          Text(
            "You can't invite yourself.",
            style: TextStyle(fontSize: TCType.textCaption, color: tc.statusDanger),
          ),
        ],
      ],
    );
  }

  Widget _scopeChip(String label, String scope) {
    final tc = SectionTheme.of(context);
    return TcGhostButton(
      label: label,
      accent: _scope == scope ? tc.accentPrimary : null,
      onPressed: () => _setScope(scope),
    );
  }

  Widget _entryRow(DirectoryEntry e) {
    final tc = SectionTheme.of(context);
    final selected = e.identityHash == _selectedHash;
    final label = e.displayName.isNotEmpty
        ? e.displayName
        : '${e.identityHash.substring(0, 16)}…';
    return GestureDetector(
      onTap: () => setState(() {
        _selectedHash = selected ? null : e.identityHash;
        if (!selected) _manualHash.clear();
      }),
      child: MouseRegion(
        cursor: SystemMouseCursors.click,
        child: Container(
          color: selected ? tc.bgSelected : Colors.transparent,
          padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
          child: Row(
            children: [
              StatusDot(
                status: e.isOnline ? PresenceStatus.online : PresenceStatus.offline,
                size: 10,
              ),
              const SizedBox(width: 8),
              Expanded(
                child: Text(
                  label,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                    fontSize: TCType.textBodySm,
                    color: selected ? tc.textEmphasis : tc.textSecondary,
                  ),
                ),
              ),
              Text(
                '${e.identityHash.substring(0, 8)}…',
                style: TextStyle(fontSize: TCType.textMicro, color: tc.textTertiary),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
