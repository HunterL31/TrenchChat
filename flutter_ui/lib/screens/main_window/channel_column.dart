// 1b: 206px channel column -- server header, CHANNELS, DIRECT CHANNELS,
// ONLINE roster footer, +CHANNEL / JOIN ghost buttons.
import 'package:flutter/material.dart';

import '../../api/models/member.dart';
import '../../api/models/server.dart';
import '../../theme/effects.dart';
import '../../theme/tokens.dart';
import '../../widgets/status_dot.dart';
import '../../widgets/tc_button.dart';

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
  });

  final String? serverName;
  final int? serverMemberCount;
  final List<Channel> channels;
  final List<Channel> directChannels;
  final String? selectedChannelHash;
  final ValueChanged<String> onSelectChannel;
  final List<PresenceEntry> onlinePresence;

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
                if (channels.isNotEmpty) ...[
                  const _SectionLabel('CHANNELS'),
                  for (final c in channels)
                    _ChannelRow(
                      channel: c,
                      selected: c.hash == selectedChannelHash,
                      onTap: () => onSelectChannel(c.hash),
                    ),
                ],
                if (directChannels.isNotEmpty) ...[
                  const _SectionLabel('DIRECT CHANNELS'),
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
              ],
            ),
          ),
          Container(
            decoration: BoxDecoration(
              border: Border(top: BorderSide(color: TCColors.borderSubtle)),
            ),
            padding: const EdgeInsets.all(10),
            child: const Row(
              children: [
                Expanded(child: TcGhostButton(label: '＋ CHANNEL', onPressed: null)),
                SizedBox(width: 6),
                Expanded(child: TcGhostButton(label: '⤵ JOIN', onPressed: null)),
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
  const _SectionLabel(this.label);
  final String label;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(14, 12, 14, 6),
      child: Text(
        label,
        style: TextStyle(
          fontSize: TCType.textMicro,
          color: TCColors.textSecondary,
          letterSpacing: TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWider),
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
                Text('🔒', style: TextStyle(fontSize: TCType.textMicro, color: TCColors.textTertiary)),
            ],
          ),
        ),
      ),
    );
  }
}
