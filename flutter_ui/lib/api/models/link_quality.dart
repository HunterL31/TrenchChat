// Mirrors trenchchat/core/link_quality.py's LinkQuality tiers.
enum LinkQualityLevel { excellent, good, fair, poor, unknown }

class ChannelLinkQuality {
  const ChannelLinkQuality({required this.level, this.hops});

  final LinkQualityLevel level;
  final int? hops;

  static const unknown = ChannelLinkQuality(level: LinkQualityLevel.unknown, hops: null);
}
