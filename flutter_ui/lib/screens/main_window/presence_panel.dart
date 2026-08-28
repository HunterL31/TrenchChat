// Right-hand presence panel: who is on the open channel, online first.
// Lives beside the message list because the roster is channel state, and
// gets the full window height with its own scrolling -- so a busy channel
// can never squeeze the channel/DM list the way the old pinned sidebar
// section did.
import 'package:flutter/material.dart';

import '../../api/models/member.dart';
import '../../theme/section_theme.dart';
import '../../theme/tokens.dart';
import '../../widgets/status_dot.dart';
import '../../widgets/tc_context_menu.dart';

/// Below this window width the panel is hidden; the header's members dialog
/// covers the same ground there.
const double presencePanelBreakpoint = 950;

const double _panelWidth = 184;

class PresencePanel extends StatelessWidget {
  const PresencePanel({
    super.key,
    required this.presence,
    this.meHashHex = '',
    this.friendHashes = const {},
    this.onAddFriend,
  });

  /// The open channel's roster, online and offline alike.
  final List<PresenceEntry> presence;

  /// The local user's identity hash, so the panel never lists the reader
  /// as one of their own peers.
  final String meHashHex;

  /// Identity hashes already saved as a friend -- drives the "Add friend…"
  /// vs "Edit friend…" context menu label.
  final Set<String> friendHashes;

  /// Fired with a row's identity hash when "Add/Edit friend…" is chosen.
  final void Function(String identityHashHex)? onAddFriend;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final peers = presence.where((p) => p.identityHash != meHashHex).toList();
    final online = peers.where((p) => p.isOnline).toList();
    final offline = peers.where((p) => !p.isOnline).toList();
    return Container(
      width: _panelWidth,
      decoration: BoxDecoration(
        color: tc.bgSurface,
        border: Border(left: BorderSide(color: tc.borderSubtle)),
      ),
      child: ListView(
        padding: const EdgeInsets.symmetric(vertical: 10),
        children: [
          _SectionLabel('ONLINE — ${online.length}'),
          for (final p in online) _PeerRow(entry: p, panel: this),
          if (offline.isNotEmpty) ...[
            _SectionLabel('OFFLINE — ${offline.length}'),
            for (final p in offline) _PeerRow(entry: p, panel: this),
          ],
        ],
      ),
    );
  }
}

class _SectionLabel extends StatelessWidget {
  const _SectionLabel(this.label);
  final String label;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    return Padding(
      padding: const EdgeInsets.fromLTRB(14, 8, 14, 6),
      child: Text(
        label,
        style: TextStyle(
          fontSize: TCType.textMicro,
          color: tc.textSecondary,
          letterSpacing: TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWider),
        ),
      ),
    );
  }
}

class _PeerRow extends StatelessWidget {
  const _PeerRow({required this.entry, required this.panel});

  final PresenceEntry entry;
  final PresencePanel panel;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    return TcContextMenuRegion(
      items: [
        if (panel.onAddFriend != null)
          TcContextMenuItem(
            label: panel.friendHashes.contains(entry.identityHash)
                ? 'Edit friend…'
                : 'Add friend…',
            onTap: () => panel.onAddFriend!(entry.identityHash),
          ),
      ],
      child: Opacity(
        opacity: entry.isOnline ? 1.0 : 0.5,
        child: Padding(
          padding: const EdgeInsets.fromLTRB(14, 3, 14, 3),
          child: Row(
            children: [
              StatusDot(
                status: entry.isOnline ? PresenceStatus.online : PresenceStatus.offline,
                size: 10,
              ),
              const SizedBox(width: 9),
              Expanded(
                child: Text(
                  entry.displayName?.isNotEmpty == true
                      ? entry.displayName!
                      : _shortHash(entry.identityHash),
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(fontSize: 12, color: tc.textSecondary),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

String _shortHash(String hex) {
  if (hex.length <= 8) return hex;
  return '${hex.substring(0, 4)}…${hex.substring(hex.length - 4)}';
}
