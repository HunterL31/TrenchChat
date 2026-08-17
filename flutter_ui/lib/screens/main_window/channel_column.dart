// 1b: 206px channel column -- server header, CHANNELS, DIRECT CHANNELS,
// ONLINE roster footer, +CHANNEL / JOIN ghost buttons.
import 'package:flutter/material.dart';

import '../../api/models/invite.dart';
import '../../api/models/member.dart';
import '../../api/models/server.dart';
import '../../theme/effects.dart';
import '../../theme/tokens.dart';
import '../../widgets/status_dot.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_context_menu.dart';
import '../../widgets/tc_icon.dart';

class ChannelColumn extends StatelessWidget {
  const ChannelColumn({
    super.key,
    required this.serverName,
    required this.serverMemberCount,
    required this.channels,
    required this.directChannels,
    required this.selectedChannelHash,
    required this.onSelectChannel,
    required this.onlinePresence,
    this.pendingInvites = const [],
    this.onTapInvite,
    this.onCreateChannel,
    this.onCreateDirectChannel,
    this.onJoinChannel,
    this.friendHashes = const {},
    this.onAddFriend,
  });

  final String? serverName;
  final int? serverMemberCount;
  final List<Channel> channels;
  final List<Channel> directChannels;
  final String? selectedChannelHash;
  final ValueChanged<String> onSelectChannel;
  final List<PresenceEntry> onlinePresence;
  final List<PendingInvite> pendingInvites;
  final ValueChanged<PendingInvite>? onTapInvite;
  final VoidCallback? onCreateChannel;

  /// Creates a standalone channel regardless of which server is selected;
  /// keeps direct channels reachable while a server occupies the main button.
  final VoidCallback? onCreateDirectChannel;
  final VoidCallback? onJoinChannel;

  /// Identity hashes already saved as a friend -- drives the "Add friend…"
  /// vs "Edit friend…" context menu label. Plain data, not a live AppState
  /// read, so this leaf stays testable in isolation.
  final Set<String> friendHashes;

  /// Fired with an online peer's identity hash when "Add/Edit friend…" is
  /// chosen from the roster row's right-click menu.
  final void Function(String identityHashHex)? onAddFriend;

  @override
  Widget build(BuildContext context) {
    final online = onlinePresence.where((p) => p.isOnline).toList();
    return Container(
      width: 206,
      color: TCColors.bgSurface,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          if (serverName != null)
            Container(
              padding: const EdgeInsets.all(14),
              decoration: BoxDecoration(
                border: Border(bottom: BorderSide(color: TCColors.borderSubtle)),
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    serverName!,
                    style: TextStyle(
                      fontFamily: TCType.fontDisplay,
                      fontSize: 21,
                      height: 1.1,
                      color: TCColors.green100,
                    ),
                  ),
                  const SizedBox(height: 2),
                  Text(
                    serverMemberCount != null ? '$serverMemberCount MEMBERS' : 'MEMBERS UNKNOWN',
                    style: TextStyle(
                      fontSize: TCType.textMicro,
                      color: TCColors.textSecondary,
                      letterSpacing: TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWide),
                    ),
                  ),
                ],
              ),
            ),
          Expanded(
            child: ListView(
              padding: EdgeInsets.zero,
              children: [
                if (pendingInvites.isNotEmpty) ...[
                  const _SectionLabel('INVITES'),
                  for (final invite in pendingInvites)
                    _InviteRow(invite: invite, onTap: onTapInvite),
                ],
                if (channels.isNotEmpty) ...[
                  const _SectionLabel('CHANNELS'),
                  for (final c in channels)
                    _ChannelRow(
                      channel: c,
                      selected: c.hash == selectedChannelHash,
                      onTap: () => onSelectChannel(c.hash),
                    ),
                ],
                if (directChannels.isNotEmpty || onCreateDirectChannel != null) ...[
                  _SectionLabel('DIRECT CHANNELS', onAdd: onCreateDirectChannel),
                  for (final c in directChannels)
                    _ChannelRow(
                      channel: c,
                      selected: c.hash == selectedChannelHash,
                      onTap: () => onSelectChannel(c.hash),
                    ),
                ],
              ],
            ),
          ),
          Container(
            decoration: BoxDecoration(
              border: Border(top: BorderSide(color: TCColors.borderSubtle)),
            ),
            padding: const EdgeInsets.fromLTRB(14, 10, 14, 4),
            child: Text(
              '▾ ONLINE — ${online.length}',
              style: TextStyle(
                fontSize: TCType.textMicro,
                color: TCColors.textSecondary,
                letterSpacing: TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWider),
              ),
            ),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(14, 2, 14, 12),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                for (final p in online)
                  Padding(
                    padding: const EdgeInsets.symmetric(vertical: 3),
                    child: GestureDetector(
                      onSecondaryTapDown: onAddFriend == null
                          ? null
                          : (details) => showTcContextMenu(
                                context: context,
                                position: details.globalPosition,
                                items: [
                                  TcContextMenuItem(
                                    label: friendHashes.contains(p.identityHash)
                                        ? 'Edit friend…'
                                        : 'Add friend…',
                                    onTap: () => onAddFriend!(p.identityHash),
                                  ),
                                ],
                              ),
                      child: Row(
                        children: [
                          const StatusDot(status: PresenceStatus.online, size: 10),
                          const SizedBox(width: 9),
                          Expanded(
                            child: Text(
                              _shortHash(p.identityHash),
                              overflow: TextOverflow.ellipsis,
                              style: TextStyle(fontSize: 12, color: TCColors.textSecondary),
                            ),
                          ),
                        ],
                      ),
                    ),
                  ),
              ],
            ),
          ),
          Container(
            decoration: BoxDecoration(
              border: Border(top: BorderSide(color: TCColors.borderSubtle)),
            ),
            padding: const EdgeInsets.all(10),
            child: Row(
              children: [
                Expanded(
                  child: TcGhostButton(icon: TcIcons.plus, label: 'CHANNEL', onPressed: onCreateChannel),
                ),
                const SizedBox(width: 6),
                Expanded(
                  child: TcGhostButton(icon: TcIcons.join, label: 'JOIN', onPressed: onJoinChannel),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

String _shortHash(String hex) {
  if (hex.length <= 8) return hex;
  return '${hex.substring(0, 4)}…${hex.substring(hex.length - 4)}';
}

class _SectionLabel extends StatelessWidget {
  const _SectionLabel(this.label, {this.onAdd});
  final String label;
  final VoidCallback? onAdd;

  @override
  Widget build(BuildContext context) {
    final text = Text(
      label,
      style: TextStyle(
        fontSize: TCType.textMicro,
        color: TCColors.textSecondary,
        letterSpacing: TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWider),
      ),
    );
    if (onAdd == null) {
      return Padding(padding: const EdgeInsets.fromLTRB(14, 12, 14, 6), child: text);
    }
    return Padding(
      padding: const EdgeInsets.fromLTRB(14, 6, 8, 0),
      child: Row(
        children: [
          Expanded(child: text),
          TcIconButton(
            icon: TcIcons.plus,
            tooltip: 'New direct channel',
            size: 22,
            onPressed: onAdd,
          ),
        ],
      ),
    );
  }
}

class _InviteRow extends StatefulWidget {
  const _InviteRow({required this.invite, required this.onTap});

  final PendingInvite invite;
  final ValueChanged<PendingInvite>? onTap;

  @override
  State<_InviteRow> createState() => _InviteRowState();
}

class _InviteRowState extends State<_InviteRow> {
  bool _hover = false;

  @override
  Widget build(BuildContext context) {
    return MouseRegion(
      cursor: SystemMouseCursors.click,
      onEnter: (_) => setState(() => _hover = true),
      onExit: (_) => setState(() => _hover = false),
      child: GestureDetector(
        onTap: widget.onTap == null ? null : () => widget.onTap!(widget.invite),
        child: AnimatedContainer(
          duration: TCEffects.durationMed,
          curve: TCEffects.easeTerminal,
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 6),
          color: _hover ? TCColors.bgHover : Colors.transparent,
          child: Row(
            children: [
              TcIcon(TcIcons.join, size: 12, color: TCColors.accentSecondary),
              const SizedBox(width: 6),
              Expanded(
                child: Text(
                  widget.invite.channelName,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(fontSize: 13, color: TCColors.amber300),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _ChannelRow extends StatefulWidget {
  const _ChannelRow({required this.channel, required this.selected, required this.onTap});

  final Channel channel;
  final bool selected;
  final VoidCallback onTap;

  @override
  State<_ChannelRow> createState() => _ChannelRowState();
}

class _ChannelRowState extends State<_ChannelRow> {
  bool _hover = false;

  @override
  Widget build(BuildContext context) {
    final selected = widget.selected;
    return MouseRegion(
      cursor: SystemMouseCursors.click,
      onEnter: (_) => setState(() => _hover = true),
      onExit: (_) => setState(() => _hover = false),
      child: GestureDetector(
        onTap: widget.onTap,
        child: AnimatedContainer(
          duration: TCEffects.durationMed,
          curve: TCEffects.easeTerminal,
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 6),
          decoration: BoxDecoration(
            color: selected ? TCColors.green900 : (_hover ? TCColors.bgHover : Colors.transparent),
            border: Border(
              left: BorderSide(
                color: selected ? TCColors.accentPrimary : Colors.transparent,
                width: 2,
              ),
            ),
          ),
          child: Row(
            children: [
              Text('#', style: TextStyle(color: selected ? TCColors.accentPrimary : TCColors.textTertiary)),
              const SizedBox(width: 4),
              Expanded(
                child: Text(
                  widget.channel.name,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                    fontSize: 13,
                    color: selected ? TCColors.green100 : TCColors.textSecondary,
                  ),
                ),
              ),
              if (widget.channel.isInviteOnly)
                TcIcon(TcIcons.lock, size: TCType.textMicro, color: TCColors.textTertiary),
            ],
          ),
        ),
      ),
    );
  }
}
