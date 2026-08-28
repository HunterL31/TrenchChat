// 1b: 206px channel column -- server header, CHANNELS, DIRECT CHANNELS,
// DIRECT MESSAGES, VOICE, and the ADD footer menu. DIRECT CHANNELS are
// channels outside any server; DIRECT MESSAGES are one-to-one conversations,
// which are a different thing entirely. The ONLINE roster lives in
// presence_panel.dart, beside the message list.
import 'package:flutter/material.dart';

import '../../api/models/dm.dart';
import '../../api/models/invite.dart';
import '../../api/models/permissions.dart';
import '../../api/models/server.dart';
import '../../api/models/voice.dart';
import '../../theme/effects.dart';
import '../../theme/section_theme.dart';
import '../../theme/shape.dart';
import '../../theme/tokens.dart';
import '../../widgets/status_dot.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_context_menu.dart';
import '../../widgets/tc_icon.dart';
import '../../widgets/tc_tooltip.dart';

class ChannelColumn extends StatelessWidget {
  const ChannelColumn({
    super.key,
    required this.serverName,
    required this.serverMemberCount,
    required this.channels,
    required this.directChannels,
    required this.selectedChannelHash,
    required this.onSelectChannel,
    this.pendingInvites = const [],
    this.onTapInvite,
    this.onCreateChannel,
    this.onCreateDirectChannel,
    this.onJoinChannel,
    this.dms = const [],
    this.onSelectDm,
    this.onDeleteDm,
    this.onStartDm,
    this.voiceParticipants = const [],
    this.onJoinVoice,
    this.syncStates = const {},
    this.channelPermissions = const {},
    this.onViewMembers,
    this.onInviteToChannel,
    this.onEditPermissions,
    this.onLeaveChannel,
    this.unreadCounts = const {},
  });

  final String? serverName;
  final int? serverMemberCount;
  final List<Channel> channels;
  final List<Channel> directChannels;
  final String? selectedChannelHash;
  final ValueChanged<String> onSelectChannel;
  final List<PendingInvite> pendingInvites;
  final ValueChanged<PendingInvite>? onTapInvite;
  final VoidCallback? onCreateChannel;

  /// Creates a standalone channel regardless of which server is selected;
  /// keeps direct channels reachable while a server occupies the main button.
  final VoidCallback? onCreateDirectChannel;
  final VoidCallback? onJoinChannel;

  /// Direct-message conversations. Distinct from [directChannels], which are
  /// channels outside any server -- a conversation has two people in it and is
  /// never announced or joined.
  final List<DmConversation> dms;
  final ValueChanged<String>? onSelectDm;
  final ValueChanged<DmConversation>? onDeleteDm;

  /// Opens the "message a friend" picker.
  final VoidCallback? onStartDm;

  /// The selected channel's voice roster. Plain data: not a live AppState
  /// read, so this leaf stays testable in isolation.
  final List<VoiceParticipant> voiceParticipants;

  /// Joins the selected channel's voice session; null hides the affordance
  /// (no channel, no voice permission, or already in a call).
  final VoidCallback? onJoinVoice;

  /// Channel hash -> sync state as reported by the backend. Only
  /// `incomplete` draws anything; every other state is the quiet case.
  final Map<String, String> syncStates;

  /// Channel hash -> this reader's permissions there, as far as they are
  /// known. A channel not opened yet has no entry, and the entries its
  /// permissions gate are left out of the row menu rather than guessed at --
  /// the backend enforces either way.
  final Map<String, ChannelPermissions> channelPermissions;

  /// Row context-menu actions. Each is handed the channel the menu was opened
  /// on; a null one leaves that entry out.
  final void Function(Channel channel)? onViewMembers;
  final void Function(Channel channel)? onInviteToChannel;
  final void Function(Channel channel)? onEditPermissions;
  final void Function(Channel channel)? onLeaveChannel;

  /// Channel hash -> unread message count. Zero or missing draws nothing;
  /// anything else brightens the row and adds the same count pill DM rows use.
  final Map<String, int> unreadCounts;

  /// The right-click menu for one channel row, mirroring the Qt client's
  /// channel menu (main_window.py's _on_channel_context_menu). Leaving is
  /// offered for standalone channels only: membership of a server's channel
  /// belongs to the server, so there is no such thing as leaving one of them.
  List<TcContextMenuItem> _menuFor(Channel channel) {
    final perms = channelPermissions[channel.hash];
    return [
      if (onInviteToChannel != null && (perms?.invite ?? false))
        TcContextMenuItem(
          label: 'Invite…',
          onTap: () => onInviteToChannel!(channel),
        ),
      if (onViewMembers != null)
        TcContextMenuItem(label: 'Members…', onTap: () => onViewMembers!(channel)),
      if (onEditPermissions != null && (perms?.manageChannel ?? false))
        TcContextMenuItem(
          label: 'Edit permissions…',
          onTap: () => onEditPermissions!(channel),
        ),
      if (onLeaveChannel != null && channel.serverHash == null)
        TcContextMenuItem(label: 'Leave channel', onTap: () => onLeaveChannel!(channel)),
    ];
  }

  /// Every way to add a conversation, consolidated behind the footer's one
  /// ADD button. With a server selected its channel button lives here too;
  /// the per-section + shortcuts stay as quicker paths to the same actions.
  List<TcContextMenuItem> _addMenuItems() {
    return [
      if (onCreateChannel != null)
        TcContextMenuItem(
          label: serverName != null ? 'New channel in $serverName' : 'New channel',
          onTap: onCreateChannel!,
        ),
      if (serverName != null && onCreateDirectChannel != null)
        TcContextMenuItem(label: 'New direct channel', onTap: onCreateDirectChannel!),
      if (onJoinChannel != null)
        TcContextMenuItem(label: 'Join channel…', onTap: onJoinChannel!),
      if (onStartDm != null)
        TcContextMenuItem(label: 'Message a friend…', onTap: onStartDm!),
    ];
  }

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    return Container(
      width: 206,
      color: tc.bgSurface,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          if (serverName != null)
            Container(
              padding: const EdgeInsets.all(14),
              decoration: BoxDecoration(
                border: Border(bottom: BorderSide(color: tc.borderSubtle)),
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    serverName!,
                    style: TextStyle(
                      fontFamily: SectionTheme.styleOf(context).displayFont,
                      fontSize: 21,
                      height: 1.1,
                      color: tc.textEmphasis,
                    ),
                  ),
                  const SizedBox(height: 2),
                  Text(
                    serverMemberCount != null ? '$serverMemberCount MEMBERS' : 'MEMBERS UNKNOWN',
                    style: TextStyle(
                      fontSize: TCType.textMicro,
                      color: tc.textSecondary,
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
                  _SectionLabel('CHANNELS',
                      onAdd: onCreateChannel, addTooltip: 'New channel'),
                  for (final c in channels)
                    _ChannelRow(
                      channel: c,
                      selected: c.hash == selectedChannelHash,
                      onTap: () => onSelectChannel(c.hash),
                      incomplete: syncStates[c.hash] == 'incomplete',
                      menuItems: _menuFor(c),
                      unread: unreadCounts[c.hash] ?? 0,
                    ),
                ],
                if (directChannels.isNotEmpty || onCreateDirectChannel != null) ...[
                  _SectionLabel('DIRECT CHANNELS',
                      onAdd: onCreateDirectChannel, addTooltip: 'New direct channel'),
                  for (final c in directChannels)
                    _ChannelRow(
                      channel: c,
                      selected: c.hash == selectedChannelHash,
                      onTap: () => onSelectChannel(c.hash),
                      incomplete: syncStates[c.hash] == 'incomplete',
                      menuItems: _menuFor(c),
                      unread: unreadCounts[c.hash] ?? 0,
                    ),
                ],
                if (dms.isNotEmpty || onStartDm != null) ...[
                  _SectionLabel('DIRECT MESSAGES',
                      onAdd: onStartDm, addTooltip: 'Message a friend'),
                  for (final d in dms)
                    _DmRow(
                      conversation: d,
                      selected: d.hash == selectedChannelHash,
                      onTap: () => onSelectDm?.call(d.hash),
                      onDelete: onDeleteDm == null ? null : () => onDeleteDm!(d),
                    ),
                ],
                if (voiceParticipants.isNotEmpty || onJoinVoice != null) ...[
                  Padding(
                    padding: const EdgeInsets.fromLTRB(14, 12, 14, 6),
                    child: Text(
                      '▾ VOICE — ${voiceParticipants.length}',
                      style: TextStyle(
                        fontSize: TCType.textMicro,
                        color: tc.textSecondary,
                        letterSpacing:
                            TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWider),
                      ),
                    ),
                  ),
                  for (final p in voiceParticipants) _VoiceRow(participant: p),
                  if (onJoinVoice != null)
                    Padding(
                      padding: const EdgeInsets.fromLTRB(14, 4, 14, 6),
                      child: TcGhostButton(
                        icon: TcIcons.headset,
                        label: 'JOIN VOICE',
                        onPressed: onJoinVoice,
                      ),
                    ),
                ],
              ],
            ),
          ),
          if (_addMenuItems().isNotEmpty)
            Container(
              decoration: BoxDecoration(
                border: Border(top: BorderSide(color: tc.borderSubtle)),
              ),
              padding: const EdgeInsets.all(10),
              child: _AddMenuButton(items: _addMenuItems()),
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
  const _SectionLabel(this.label, {this.onAdd, this.addTooltip = ''});
  final String label;
  final VoidCallback? onAdd;
  final String addTooltip;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final text = Text(
      label,
      style: TextStyle(
        fontSize: TCType.textMicro,
        color: tc.textSecondary,
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
            tooltip: addTooltip,
            size: 22,
            onPressed: onAdd,
          ),
        ],
      ),
    );
  }
}

/// The footer's single entry point for adding anything: opens the
/// consolidated add menu anchored on itself.
class _AddMenuButton extends StatelessWidget {
  const _AddMenuButton({required this.items});
  final List<TcContextMenuItem> items;

  @override
  Widget build(BuildContext context) {
    return Builder(
      builder: (buttonContext) => TcGhostButton(
        icon: TcIcons.plus,
        label: 'ADD',
        onPressed: () {
          final box = buttonContext.findRenderObject() as RenderBox?;
          final position = box?.localToGlobal(Offset.zero) ?? Offset.zero;
          showTcContextMenu(
            context: buttonContext,
            position: position,
            items: items,
          );
        },
      ),
    );
  }
}

/// The unread-count pill shared by channel and conversation rows.
class _UnreadPill extends StatelessWidget {
  const _UnreadPill({required this.count});
  final int count;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 1),
      decoration: BoxDecoration(
        color: tc.accentPrimary,
        borderRadius: tcCorners(context, scale: 0.5),
      ),
      child: Text(
        '$count',
        style: TextStyle(fontSize: TCType.textMicro, color: tc.bgApp),
      ),
    );
  }
}

class _VoiceRow extends StatelessWidget {
  const _VoiceRow({required this.participant});

  final VoiceParticipant participant;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    // The online dot already carries the green glow, giving the "lit while
    // speaking" read without a new widget.
    final degraded = switch (participant.linkState) {
      VoiceLinkState.connecting ||
      VoiceLinkState.unreachable ||
      VoiceLinkState.signalled ||
      VoiceLinkState.unknown =>
        true,
      VoiceLinkState.self || VoiceLinkState.streaming => false,
    };
    final name = participant.displayName.isNotEmpty
        ? participant.displayName
        : _shortHash(participant.identityHash);
    return Opacity(
      opacity: degraded ? 0.45 : 1.0,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(14, 3, 14, 3),
        child: Row(
          children: [
            StatusDot(
              status: participant.speaking ? PresenceStatus.online : PresenceStatus.offline,
              size: 10,
            ),
            const SizedBox(width: 9),
            Expanded(
              child: Text(
                name,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(fontSize: 12, color: tc.textSecondary),
              ),
            ),
            if (participant.muted)
              TcIcon(TcIcons.micMuted, size: 12, color: tc.textTertiary),
          ],
        ),
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
    final tc = SectionTheme.of(context);
    return MouseRegion(
      cursor: SystemMouseCursors.click,
      onEnter: (_) => setState(() => _hover = true),
      onExit: (_) => setState(() => _hover = false),
      child: GestureDetector(
        onTap: widget.onTap == null ? null : () => widget.onTap!(widget.invite),
        child: AnimatedContainer(
          duration: TCEffects.durationMed,
          curve: TCEffects.easeTerminal,
          margin: EdgeInsets.symmetric(horizontal: tcRadius(context, scale: 0.75)),
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 6),
          decoration: BoxDecoration(
            color: _hover ? tc.bgHover : Colors.transparent,
            borderRadius: tcCorners(context, scale: 0.75),
          ),
          child: Row(
            children: [
              TcIcon(TcIcons.join,
                  size: 12, color: _hover ? tc.accentSecondaryHover : tc.accentSecondary),
              const SizedBox(width: 6),
              Expanded(
                child: Text(
                  widget.invite.channelName,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(fontSize: 13, color: tc.accentSecondaryHover),
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
  const _ChannelRow({
    required this.channel,
    required this.selected,
    required this.onTap,
    this.incomplete = false,
    this.menuItems = const [],
    this.unread = 0,
  });

  final Channel channel;
  final bool selected;
  final VoidCallback onTap;
  final bool incomplete;
  final List<TcContextMenuItem> menuItems;
  final int unread;

  @override
  State<_ChannelRow> createState() => _ChannelRowState();
}

class _ChannelRowState extends State<_ChannelRow> {
  bool _hover = false;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final selected = widget.selected;
    return TcContextMenuRegion(
      items: widget.menuItems,
      child: MouseRegion(
        cursor: SystemMouseCursors.click,
        onEnter: (_) => setState(() => _hover = true),
        onExit: (_) => setState(() => _hover = false),
        child: GestureDetector(
          onTap: widget.onTap,
          child: AnimatedContainer(
            duration: TCEffects.durationMed,
            curve: TCEffects.easeTerminal,
            // A rounded row cannot run edge to edge and still read as
            // rounded, so the radius pays for its own margin.
            margin: EdgeInsets.symmetric(horizontal: tcRadius(context, scale: 0.75)),
            padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 6),
            decoration: BoxDecoration(
              color: selected ? tc.bgSelected : (_hover ? tc.bgHover : Colors.transparent),
              border: Border(
                left: BorderSide(
                  color: selected && !tcIsRounded(context)
                      ? tc.accentPrimary
                      : Colors.transparent,
                  width: 2,
                ),
              ),
              borderRadius: tcCorners(context, scale: 0.75),
            ),
            child: Row(
              children: [
                Text('#',
                    style: TextStyle(color: selected ? tc.accentPrimary : tc.textTertiary)),
                const SizedBox(width: 4),
                Expanded(
                  child: Text(
                    widget.channel.name,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(
                      fontSize: 13,
                      color: selected || widget.unread > 0
                          ? tc.textEmphasis
                          : tc.textSecondary,
                      fontWeight: widget.unread > 0 && !selected
                          ? FontWeight.w600
                          : FontWeight.normal,
                    ),
                  ),
                ),
                if (widget.unread > 0 && !selected) ...[
                  _UnreadPill(count: widget.unread),
                  const SizedBox(width: 4),
                ],
                if (widget.incomplete) ...[
                  Tooltip(
                    decoration: tcTooltipDecoration(context),
                    textStyle: tcTooltipTextStyle(context),
                    message: 'History incomplete \u2014 some messages could not be synced',
                    child: Text(
                      'INCOMPLETE',
                      style: TextStyle(
                        fontSize: TCType.textMicro,
                        color: tc.accentSecondary,
                        letterSpacing:
                            TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWide),
                      ),
                    ),
                  ),
                  const SizedBox(width: 4),
                ],
                if (widget.channel.isInviteOnly)
                  TcIcon(TcIcons.lock, size: TCType.textMicro, color: tc.textTertiary),
              ],
            ),
          ),
        ),
      ),
    );
  }
}


/// One conversation in the DIRECT MESSAGES section. Shows the peer's presence
/// and an unread count; a conversation with a peer who is no longer a friend
/// stays readable but is marked, because nothing more can pass either way. A
/// peer on another LXMF client is marked too: messages reach them, reactions
/// and the rest of TrenchChat's extras do not.
class _DmRow extends StatefulWidget {
  const _DmRow({
    required this.conversation,
    required this.selected,
    required this.onTap,
    this.onDelete,
  });

  final DmConversation conversation;
  final bool selected;
  final VoidCallback onTap;
  final VoidCallback? onDelete;

  @override
  State<_DmRow> createState() => _DmRowState();
}

class _DmRowState extends State<_DmRow> {
  bool _hover = false;

  String get _label {
    final d = widget.conversation;
    if (d.displayName.isNotEmpty) return d.displayName;
    final hex = d.peerHash;
    if (hex.length <= 8) return hex;
    return '${hex.substring(0, 4)}…${hex.substring(hex.length - 4)}';
  }

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final d = widget.conversation;
    return MouseRegion(
      cursor: SystemMouseCursors.click,
      onEnter: (_) => setState(() => _hover = true),
      onExit: (_) => setState(() => _hover = false),
      child: TcContextMenuRegion(
        items: [
          if (widget.onDelete != null)
            TcContextMenuItem(label: 'Delete conversation', onTap: widget.onDelete!),
        ],
        child: GestureDetector(
          onTap: widget.onTap,
          child: AnimatedContainer(
            duration: TCEffects.durationMed,
            curve: TCEffects.easeTerminal,
            margin: EdgeInsets.symmetric(horizontal: tcRadius(context, scale: 0.75)),
            padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 6),
            decoration: BoxDecoration(
              color: widget.selected
                  ? tc.bgSelected
                  : (_hover ? tc.bgHover : Colors.transparent),
              border: Border(
                left: BorderSide(
                  color: widget.selected && !tcIsRounded(context)
                      ? tc.accentPrimary
                      : Colors.transparent,
                  width: 2,
                ),
              ),
              borderRadius: tcCorners(context, scale: 0.75),
            ),
            child: Row(
              children: [
                StatusDot(
                  status: d.isOnline ? PresenceStatus.online : PresenceStatus.offline,
                  size: 8,
                ),
                const SizedBox(width: 9),
                Expanded(
                  child: Text(
                    _label,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(
                      fontSize: 13,
                      color: widget.selected || d.unread > 0
                          ? tc.textEmphasis
                          : tc.textSecondary,
                      fontWeight: d.unread > 0 && !widget.selected
                          ? FontWeight.w600
                          : FontWeight.normal,
                    ),
                  ),
                ),
                if (!d.isFriend)
                  Padding(
                    padding: const EdgeInsets.only(left: 6),
                    child: Text(
                      'NOT A FRIEND',
                      style: TextStyle(fontSize: TCType.textMicro, color: tc.textTertiary),
                    ),
                  )
                else ...[
                  if (!d.peerIsTrenchchat)
                    Padding(
                      padding: const EdgeInsets.only(left: 6),
                      child: TcTooltip(
                        message: 'On another LXMF client — messages work, '
                            'reactions and other TrenchChat extras do not',
                        child: Text(
                          'LXMF',
                          style: TextStyle(
                              fontSize: TCType.textMicro, color: tc.textTertiary),
                        ),
                      ),
                    ),
                  if (d.unread > 0)
                    Padding(
                      padding: const EdgeInsets.only(left: 6),
                      child: _UnreadPill(count: d.unread),
                    ),
                ],
              ],
            ),
          ),
        ),
      ),
    );
  }
}
