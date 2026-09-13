// Mirrors trenchchat/core/link_quality.py's LinkQuality tiers.

/// How often the open channel's reading is re-read on a timer, on top of the
/// topology events that drive it. Path entries expire with no event of their
/// own, so without this the pill would keep showing a path that has lapsed.
const Duration linkQualityRefreshInterval = Duration(seconds: 60);

enum LinkQualityLevel { excellent, good, fair, poor, unknown }

LinkQualityLevel _levelFromScore(int score) => switch (score) {
      4 => LinkQualityLevel.excellent,
      3 => LinkQualityLevel.good,
      2 => LinkQualityLevel.fair,
      1 => LinkQualityLevel.poor,
      _ => LinkQualityLevel.unknown,
    };

int? _asInt(dynamic v) => v is num ? v.toInt() : null;

double? _asDouble(dynamic v) => v is num ? v.toDouble() : null;

/// One channel member's link, as the backend reads it out of the RNS path
/// table. [hops] is null when this node holds no path to them at all, which is
/// what "unreachable" means here: a message to them would be queued for retry
/// rather than sent now.
class PeerLinkQuality {
  const PeerLinkQuality({
    required this.identityHash,
    required this.displayName,
    required this.level,
    this.hops,
    this.via,
    this.rttMs,
    this.pathExpiresIn,
    this.isOnline = false,
    this.lastSeen = 0,
  });

  final String identityHash;
  final String displayName;
  final LinkQualityLevel level;
  final int? hops;

  /// Next hop on the path, or null when the peer is reached directly.
  final String? via;

  /// Round-trip time on a link this node already has open, null otherwise.
  final double? rttMs;

  /// Seconds before the path entry expires, null when there is no path.
  final double? pathExpiresIn;

  final bool isOnline;
  final double lastSeen;

  bool get isReachable => hops != null;

  factory PeerLinkQuality.fromJson(Map<String, dynamic> json) => PeerLinkQuality(
        identityHash: json['identity_hash'] as String? ?? '',
        displayName: json['display_name'] as String? ?? '',
        level: _levelFromScore(_asInt(json['quality']) ?? 0),
        hops: _asInt(json['hops']),
        via: json['via'] as String?,
        rttMs: _asDouble(json['rtt_ms']),
        pathExpiresIn: _asDouble(json['path_expires_in']),
        isOnline: json['is_online'] == true,
        lastSeen: _asDouble(json['last_seen']) ?? 0,
      );
}

/// The header's reading of how well this node reaches a whole channel, from
/// `GET /channels/{hash}/link_quality`.
///
/// The headline is [reachable] of [total] members: a channel send is unicast
/// to every member, so a member with no path is not reached now however good
/// the other links are. [medianHops] and [level] are medians over the
/// reachable members, taken instead of a mean because hops are ordinal and one
/// member eleven hops out would otherwise drag the whole reading with it.
///
/// This replaced a best-peer reading, which showed full bars whenever any one
/// member happened to be close by, even with the rest of the channel
/// unreachable. The best link is kept as [bestIdentityHash] / [bestHops] and
/// shown as a detail in the popover instead.
class ChannelLinkQuality {
  const ChannelLinkQuality({
    required this.level,
    this.medianHops,
    this.reachable = 0,
    this.total = 0,
    this.bestIdentityHash,
    this.bestHops,
    this.peers = const [],
  });

  final LinkQualityLevel level;
  final int? medianHops;
  final int reachable;
  final int total;
  final String? bestIdentityHash;
  final int? bestHops;

  /// Every other member, best link first; see actions.channel_link_quality.
  final List<PeerLinkQuality> peers;

  static const unknown = ChannelLinkQuality(level: LinkQualityLevel.unknown);

  Iterable<PeerLinkQuality> get reachablePeers => peers.where((p) => p.isReachable);

  Iterable<PeerLinkQuality> get unreachablePeers => peers.where((p) => !p.isReachable);

  /// Display name of the closest peer, null when nothing is reachable or the
  /// summary names a peer the roster no longer carries.
  String? get bestName {
    final hash = bestIdentityHash;
    if (hash == null) return null;
    for (final p in peers) {
      if (p.identityHash == hash) return p.displayName;
    }
    return null;
  }

  factory ChannelLinkQuality.fromJson(dynamic body) {
    if (body is! Map) return unknown;
    final summary = body['summary'];
    final rawPeers = body['peers'];
    final peers = <PeerLinkQuality>[
      if (rawPeers is List)
        for (final entry in rawPeers)
          if (entry is Map) PeerLinkQuality.fromJson(entry.cast<String, dynamic>()),
    ];
    if (summary is! Map) {
      return ChannelLinkQuality(level: LinkQualityLevel.unknown, peers: peers);
    }
    return ChannelLinkQuality(
      level: _levelFromScore(_asInt(summary['level']) ?? 0),
      medianHops: _asInt(summary['median_hops']),
      reachable: _asInt(summary['reachable']) ?? 0,
      total: _asInt(summary['total']) ?? peers.length,
      bestIdentityHash: summary['best_identity_hash'] as String?,
      bestHops: _asInt(summary['best_hops']),
      peers: peers,
    );
  }
}
