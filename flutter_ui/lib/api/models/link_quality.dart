// Mirrors trenchchat/core/link_quality.py's LinkQuality tiers.
enum LinkQualityLevel { excellent, good, fair, poor, unknown }

class ChannelLinkQuality {
  const ChannelLinkQuality({required this.level, this.hops});

  final LinkQualityLevel level;
  final int? hops;

  static const unknown = ChannelLinkQuality(level: LinkQualityLevel.unknown, hops: null);

  factory ChannelLinkQuality.fromJson(Map<String, dynamic> json) => ChannelLinkQuality(
        level: _levelFromString(json['level'] as String?),
        hops: json['hops'] as int?,
      );

  static LinkQualityLevel _levelFromString(String? value) => switch (value) {
        'excellent' => LinkQualityLevel.excellent,
        'good' => LinkQualityLevel.good,
        'fair' => LinkQualityLevel.fair,
        'poor' => LinkQualityLevel.poor,
        _ => LinkQualityLevel.unknown,
      };
}
