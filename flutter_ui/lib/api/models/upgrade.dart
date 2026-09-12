// The direct IP path as the client sees it: which path this node reaches a
// peer over, the sessions it holds, and why a pair has none. Mirrors
// trenchchat/network/base.py's path values and core/upgrade.py's failure
// reasons.
//
// Path state is local knowledge: this node's own sessions, never a claim
// about who else is directly connected to whom.

/// Which path this node reaches a peer over. `unknown` is a peer no member
/// row or event has said anything about yet, and reads as no badge.
enum PeerPath { direct, reticulum, offline, unknown }

PeerPath peerPathFrom(String? raw) => switch (raw) {
      'direct' => PeerPath.direct,
      'reticulum' => PeerPath.reticulum,
      'offline' => PeerPath.offline,
      _ => PeerPath.unknown,
    };

/// What the badge on a direct row explains when pointed at.
const String directBadgeTooltip =
    'Connected directly over IP; files and voice with this member take the '
    'fast path';

/// One direct session this node holds, from GET /upgrade/sessions.
class DirectSession {
  const DirectSession({
    required this.peer,
    required this.displayName,
    required this.since,
    required this.roundTripSecs,
    required this.bytesIn,
    required this.bytesOut,
  });

  final String peer;
  final String displayName;

  /// When the session came up, unix seconds.
  final double since;

  /// Round trip measured from acknowledgements; 0 before the first one.
  final double roundTripSecs;
  final int bytesIn;
  final int bytesOut;

  factory DirectSession.fromJson(Map<String, dynamic> json) => DirectSession(
        peer: json['peer'] as String? ?? '',
        displayName: json['display_name'] as String? ?? '',
        since: (json['since'] as num? ?? 0).toDouble(),
        roundTripSecs: (json['round_trip_secs'] as num? ?? 0).toDouble(),
        bytesIn: (json['bytes_in'] as num? ?? 0).toInt(),
        bytesOut: (json['bytes_out'] as num? ?? 0).toInt(),
      );
}

/// Why one eligible peer has no session, from the same endpoint's
/// `last_failure`.
class DirectFailure {
  const DirectFailure({
    required this.peer,
    required this.reason,
    required this.at,
    required this.nextAttempt,
  });

  final String peer;

  /// The backend's own machine-readable cause: disabled, ineligible,
  /// no_answer, punch_failed, handshake_failed, refused or backoff.
  final String reason;
  final double at;

  /// When the next attempt is due, unix seconds.
  final double nextAttempt;

  factory DirectFailure.fromJson(String peer, Map<String, dynamic> json) =>
      DirectFailure(
        peer: peer,
        reason: json['reason'] as String? ?? '',
        at: (json['at'] as num? ?? 0).toDouble(),
        nextAttempt: (json['next_attempt'] as num? ?? 0).toDouble(),
      );
}

/// What GET /upgrade/sessions answers: the sessions, why the rest of the
/// eligible peers have none, and whether this node is listening at all.
/// Not listening is what tells a firewall block apart from a NAT failure.
class DirectSessions {
  const DirectSessions({
    required this.sessions,
    required this.failures,
    required this.listening,
    required this.listenPort,
  });

  final List<DirectSession> sessions;
  final List<DirectFailure> failures;
  final bool listening;
  final int listenPort;

  static const empty = DirectSessions(
      sessions: [], failures: [], listening: false, listenPort: 0);

  factory DirectSessions.fromJson(Map<String, dynamic> json) {
    final failures = json['last_failure'] as Map<String, dynamic>? ?? const {};
    return DirectSessions(
      sessions: [
        for (final s in json['sessions'] as List<dynamic>? ?? const [])
          DirectSession.fromJson(s as Map<String, dynamic>),
      ],
      failures: [
        for (final entry in failures.entries)
          if (entry.value is Map<String, dynamic>)
            DirectFailure.fromJson(
                entry.key, entry.value as Map<String, dynamic>),
      ],
      listening: json['listening'] as bool? ?? false,
      listenPort: (json['listen_port'] as num? ?? 0).toInt(),
    );
  }
}

/// GET /upgrade/enabled: the "Direct connections" switch and the port a
/// session arrives on. The port takes effect on the next launch.
class DirectConnections {
  const DirectConnections({required this.enabled, required this.listenPort});

  final bool enabled;
  final int listenPort;

  static const unknown = DirectConnections(enabled: false, listenPort: 0);

  factory DirectConnections.fromJson(Map<String, dynamic> json) =>
      DirectConnections(
        enabled: json['enabled'] as bool? ?? false,
        listenPort: (json['listen_port'] as num? ?? 0).toInt(),
      );
}

/// Why a pair has no direct session, in plain words. Anything unrecognised
/// says only that there is no session rather than inventing a cause.
String directFailureReason(String reason) => switch (reason) {
      'disabled' => 'Direct connections are off',
      'ineligible' => 'Not eligible: no invite-only channel or server in common',
      'no_answer' => 'No answer to the offer',
      'punch_failed' => 'Could not punch through NAT',
      'handshake_failed' => 'The session handshake failed',
      'refused' => 'The peer refused',
      'backoff' => 'Waiting before the next try',
      _ => 'No direct session',
    };
