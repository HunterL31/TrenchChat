// 1b: 42px channel header -- #name + topic, link-quality chip, boxed tabs.
import 'package:flutter/material.dart';

import '../../api/models/link_quality.dart';
import '../../theme/section_theme.dart';
import '../../theme/tokens.dart';
import '../../widgets/signal_meter.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_icon.dart';
import '../../widgets/tc_tooltip.dart';

enum ChannelTab { chat, map, iface, friends }

class ChannelHeader extends StatelessWidget {
  const ChannelHeader({
    super.key,
    required this.channelName,
    required this.topic,
    required this.linkQuality,
    required this.activeTab,
    required this.onTabSelected,
    this.onViewMembers,
    this.onOpenNav,
    this.compact = false,
  });

  final String channelName;
  final String topic;
  final ChannelLinkQuality linkQuality;
  final ChannelTab activeTab;
  final ValueChanged<ChannelTab> onTabSelected;
  final VoidCallback? onViewMembers;

  /// Compact layout: shows a menu button that opens the navigation drawer.
  final VoidCallback? onOpenNav;

  /// Narrow-screen mode: menu button, no topic, signal meter without labels.
  final bool compact;

  String get _levelLabel => switch (linkQuality.level) {
        LinkQualityLevel.excellent => 'EXCELLENT',
        LinkQualityLevel.good => 'GOOD',
        LinkQualityLevel.fair => 'FAIR',
        LinkQualityLevel.poor => 'POOR',
        LinkQualityLevel.unknown => 'UNKNOWN',
      };

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final hopsLabel = linkQuality.hops != null
        ? '${linkQuality.hops} HOP${linkQuality.hops == 1 ? '' : 'S'}'
        : 'HOPS UNKNOWN';

    return Container(
      height: 42,
      padding: const EdgeInsets.symmetric(horizontal: 18),
      decoration: BoxDecoration(
        color: tc.bgSurfaceRaised,
        border: Border(bottom: BorderSide(color: tc.borderSubtle)),
      ),
      child: Row(
        children: [
          if (compact && onOpenNav != null) ...[
            TcIconButton(icon: TcIcons.menu, tooltip: 'Channels', size: 26, onPressed: onOpenNav),
            const SizedBox(width: 8),
          ],
          Expanded(
            child: Row(
              children: [
                Text('#', style: TextStyle(color: tc.accentPrimary, fontSize: 15)),
                Flexible(
                  child: Text(
                    channelName,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(color: tc.textEmphasis, fontSize: 15),
                  ),
                ),
                if (topic.isNotEmpty && !compact) ...[
                  const SizedBox(width: 12),
                  Expanded(
                    child: Text(
                      topic,
                      overflow: TextOverflow.ellipsis,
                      style:
                          TextStyle(fontSize: TCType.textCaption, color: tc.textTertiary),
                    ),
                  ),
                ],
              ],
            ),
          ),
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 3),
            decoration: BoxDecoration(
              color: tc.bgInset,
              border: Border.all(color: tc.borderSubtle),
            ),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                SignalMeter(level: linkQuality.level, size: 12),
                if (!compact) ...[
                  const SizedBox(width: 7),
                  Text(
                    '$_levelLabel · $hopsLabel',
                    style: TextStyle(
                      fontSize: TCType.textMicro,
                      color: tc.textSecondary,
                      letterSpacing:
                          TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWide),
                    ),
                  ),
                ],
              ],
            ),
          ),
          if (onViewMembers != null) ...[
            const SizedBox(width: 8),
            TcIconButton(
              icon: TcIcons.users,
              tooltip: 'Members',
              size: 26,
              onPressed: onViewMembers,
            ),
          ],
          const SizedBox(width: 8),
          Row(
            children: [
              _HeaderTab(
                  label: 'CHAT', icon: TcIcons.hash, tab: ChannelTab.chat,
                  active: activeTab, onTap: onTabSelected, compact: compact),
              _HeaderTab(
                  label: 'MAP', icon: TcIcons.map, tab: ChannelTab.map,
                  active: activeTab, onTap: onTabSelected, compact: compact),
              _HeaderTab(
                  label: 'IFACE', icon: TcIcons.iface, tab: ChannelTab.iface,
                  active: activeTab, onTap: onTabSelected, compact: compact),
              _HeaderTab(
                  label: 'FRIENDS', icon: TcIcons.users, tab: ChannelTab.friends,
                  active: activeTab, onTap: onTabSelected, compact: compact),
            ],
          ),
        ],
      ),
    );
  }
}

class _HeaderTab extends StatelessWidget {
  const _HeaderTab({
    required this.label,
    required this.icon,
    required this.tab,
    required this.active,
    required this.onTap,
    this.compact = false,
  });

  final String label;
  final TcIconData icon;
  final ChannelTab tab;
  final ChannelTab active;
  final ValueChanged<ChannelTab> onTap;

  /// Narrow-screen mode: icon instead of label, so four tabs still fit.
  final bool compact;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final selected = tab == active;
    final foreground = selected ? tc.textEmphasis : tc.textTertiary;
    return GestureDetector(
      onTap: () => onTap(tab),
      child: MouseRegion(
        cursor: SystemMouseCursors.click,
        child: TcTooltip(
          message: compact ? label : '',
          child: Container(
            margin: const EdgeInsets.only(left: 2),
            padding: EdgeInsets.symmetric(horizontal: compact ? 7 : 10, vertical: 4),
            decoration: BoxDecoration(
              color: selected ? tc.bgSelected : Colors.transparent,
              border: Border.all(color: selected ? tc.borderAccent : tc.borderSubtle),
            ),
            child: compact
                ? TcIcon(icon, size: 13, color: foreground)
                : Text(
                    label,
                    style: TextStyle(
                      fontSize: TCType.textCaption,
                      letterSpacing:
                          TCType.letterSpacingFor(TCType.textCaption, TCType.trackingWide),
                      color: foreground,
                    ),
                  ),
          ),
        ),
      ),
    );
  }
}
