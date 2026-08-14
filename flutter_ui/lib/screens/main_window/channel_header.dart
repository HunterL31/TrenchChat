// 1b: 42px channel header -- #name + topic, link-quality chip, boxed tabs.
import 'package:flutter/material.dart';

import '../../api/models/link_quality.dart';
import '../../theme/tokens.dart';
import '../../widgets/signal_meter.dart';

enum ChannelTab { chat, map, iface }

class ChannelHeader extends StatelessWidget {
  const ChannelHeader({
    super.key,
    required this.channelName,
    required this.topic,
    required this.linkQuality,
    required this.activeTab,
    required this.onTabSelected,
  });

  final String channelName;
  final String topic;
  final ChannelLinkQuality linkQuality;
  final ChannelTab activeTab;
  final ValueChanged<ChannelTab> onTabSelected;

  String get _levelLabel => switch (linkQuality.level) {
        LinkQualityLevel.excellent => 'EXCELLENT',
        LinkQualityLevel.good => 'GOOD',
        LinkQualityLevel.fair => 'FAIR',
        LinkQualityLevel.poor => 'POOR',
        LinkQualityLevel.unknown => 'UNKNOWN',
      };

  @override
  Widget build(BuildContext context) {
    final hopsLabel = linkQuality.hops != null
        ? '${linkQuality.hops} HOP${linkQuality.hops == 1 ? '' : 'S'}'
        : 'HOPS UNKNOWN';

    return Container(
      height: 42,
      padding: const EdgeInsets.symmetric(horizontal: 18),
      decoration: BoxDecoration(
        color: TCColors.bgSurfaceRaised,
        border: Border(bottom: BorderSide(color: TCColors.borderSubtle)),
      ),
      child: Row(
        children: [
          Text('#', style: TextStyle(color: TCColors.accentPrimary, fontSize: 15)),
          Text(channelName, style: TextStyle(color: TCColors.green100, fontSize: 15)),
          if (topic.isNotEmpty) ...[
            const SizedBox(width: 12),
            Expanded(
              child: Text(
                topic,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(fontSize: TCType.textCaption, color: TCColors.textTertiary),
              ),
            ),
          ] else
            const Spacer(),
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 3),
            decoration: BoxDecoration(
              color: TCColors.bgInset,
              border: Border.all(color: TCColors.borderSubtle),
            ),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                SignalMeter(level: linkQuality.level, size: 12),
                const SizedBox(width: 7),
                Text(
                  '$_levelLabel · $hopsLabel',
                  style: TextStyle(
                    fontSize: TCType.textMicro,
                    color: TCColors.textSecondary,
                    letterSpacing: TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWide),
                  ),
                ),
              ],
            ),
          ),
          const SizedBox(width: 8),
          Row(
            children: [
              _HeaderTab(label: 'CHAT', tab: ChannelTab.chat, active: activeTab, onTap: onTabSelected),
              _HeaderTab(label: 'MAP', tab: ChannelTab.map, active: activeTab, onTap: onTabSelected),
              _HeaderTab(label: 'IFACE', tab: ChannelTab.iface, active: activeTab, onTap: onTabSelected),
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
    required this.tab,
    required this.active,
    required this.onTap,
  });

  final String label;
  final ChannelTab tab;
  final ChannelTab active;
  final ValueChanged<ChannelTab> onTap;

  @override
  Widget build(BuildContext context) {
    final selected = tab == active;
    return GestureDetector(
      onTap: () => onTap(tab),
      child: MouseRegion(
        cursor: SystemMouseCursors.click,
        child: Container(
          margin: const EdgeInsets.only(left: 2),
          padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
          decoration: BoxDecoration(
            color: selected ? TCColors.green900 : Colors.transparent,
            border: Border.all(color: selected ? TCColors.borderAccent : TCColors.borderSubtle),
          ),
          child: Text(
            label,
            style: TextStyle(
              fontSize: TCType.textCaption,
              letterSpacing: TCType.letterSpacingFor(TCType.textCaption, TCType.trackingWide),
              color: selected ? TCColors.green100 : TCColors.textTertiary,
            ),
          ),
        ),
      ),
    );
  }
}
