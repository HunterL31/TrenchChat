// Mirrors trenchchat/core/link_quality.py's LinkQuality tiers.
enum LinkQualityLevel { excellent, good, fair, poor, unknown }

class ChannelLinkQuality {
  const ChannelLinkQuality({required this.level, this.hops});

  final LinkQualityLevel level;
  final int? hops;

  static const unknown = ChannelLinkQuality(level: LinkQualityLevel.unknown, hops: null);

  /// The header's single reading, folded out of the roster
  /// `GET /channels/{hash}/link_quality` returns: one entry per other member,
  /// each carrying the 0-4 score `trenchchat/core/link_quality.py` assigns and
  /// the hop count it was scored from.
  ///
  /// The best-scored peer wins. A channel is as reachable as its best link,
  /// and a peer whose path has not resolved yet scores UNKNOWN -- taking the
  /// worst instead would pin the meter to UNKNOWN whenever one member is
  /// away, which is most of the time.
  factory ChannelLinkQuality.fromRoster(List<dynamic> entries) {
    ChannelLinkQuality best = unknown;
    int bestScore = -1;
    for (final entry in entries) {
      if (entry is! Map) continue;
      final score = entry['quality'];
      if (score is! num || score.toInt() <= bestScore) continue;
      bestScore = score.toInt();
      final hops = entry['hops'];
      best = ChannelLinkQuality(
        level: _levelFromScore(score.toInt()),
        hops: hops is num ? hops.toInt() : null,
      );
    }
    return best;
  }

  static LinkQualityLevel _levelFromScore(int score) => switch (score) {
        4 => LinkQualityLevel.excellent,
        3 => LinkQualityLevel.good,
        2 => LinkQualityLevel.fair,
        1 => LinkQualityLevel.poor,
        _ => LinkQualityLevel.unknown,
      };
}
