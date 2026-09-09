// Reticulum Relay Chat models, mirroring the /rrc/* endpoints in
// devtools/testenv/api.py. Public chat is RRC now; see docs/rrc.md for what a
// hub can see and why that centre was accepted.

/// Message types from the RRC wire, kept as the spec's numbers.
const int rrcTypeMsg = 20;
const int rrcTypeNotice = 21;
const int rrcTypeAction = 22;

/// An `rrc://<hub hash>[/<room>]` link, as micron pages and other clients
/// write them. Null from [parseRrcLink] when it is not one, or when the hash
/// is not a hash: a link is an invitation to dial, so what it names has to be
/// a destination before anything is opened.
class RRCLink {
  const RRCLink(this.hubHash, this.room);

  final String hubHash;

  /// The room to join once connected, with its '#', or null when the link
  /// names only a hub.
  final String? room;
}

final RegExp _hubHashRe = RegExp(r'^[0-9a-f]{32}$');

RRCLink? parseRrcLink(String url) {
  final lower = url.trim().toLowerCase();
  if (!lower.startsWith('rrc://')) return null;
  final rest = lower.substring('rrc://'.length);
  if (rest.isEmpty) return null;
  final slash = rest.indexOf('/');
  final hub = slash < 0 ? rest : rest.substring(0, slash);
  if (!_hubHashRe.hasMatch(hub)) return null;
  if (slash < 0 || slash == rest.length - 1) return RRCLink(hub, null);
  final room = rest.substring(slash + 1);
  return RRCLink(hub, room.startsWith('#') ? room : '#$room');
}

/// A hub heard on the mesh from its rrc.hub announce.
class RRCHub {
  const RRCHub({
    required this.hash,
    required this.name,
    required this.heardAt,
    required this.bookmarked,
    required this.connected,
  });

  final String hash;

  /// Whatever the hub put in its announce: a label over a verified
  /// destination hash, never an identity. Shown beside the hash, never
  /// instead of it.
  final String name;
  final double heardAt;
  final bool bookmarked;
  final bool connected;

  factory RRCHub.fromJson(Map<String, dynamic> json) => RRCHub(
        hash: json['hash'] as String? ?? '',
        name: json['name'] as String? ?? '',
        heardAt: (json['heard_at'] as num?)?.toDouble() ?? 0,
        bookmarked: json['bookmarked'] as bool? ?? false,
        connected: json['connected'] as bool? ?? false,
      );
}

/// The session with one hub. 'idle' means there is none.
class RRCSession {
  const RRCSession({
    required this.hub,
    required this.state,
    required this.name,
    required this.version,
    required this.rooms,
    required this.limits,
  });

  const RRCSession.idle()
      : hub = null,
        state = 'idle',
        name = '',
        version = '',
        rooms = const {},
        limits = const {};

  final String? hub;

  /// 'idle' | 'dialing' | 'handshaking' | 'active' | 'unreachable'
  final String state;
  final String name;
  final String version;

  /// Room name to its state, as the hub last confirmed it.
  final Map<String, String> rooms;

  /// What the hub said it enforces, so the client can refuse a line the hub
  /// would only reject.
  final Map<String, int> limits;

  bool get isActive => state == 'active';
  bool get isConnecting => state == 'dialing' || state == 'handshaking';

  int get maxMessageBytes => limits['max_msg_body_bytes'] ?? 0;
  int get maxNickBytes => limits['max_nick_bytes'] ?? 0;

  factory RRCSession.fromJson(Map<String, dynamic> json) => RRCSession(
        hub: json['hub'] as String?,
        state: json['state'] as String? ?? 'idle',
        name: json['name'] as String? ?? '',
        version: json['version'] as String? ?? '',
        rooms: {
          for (final entry in (json['rooms'] as Map<dynamic, dynamic>? ?? {}).entries)
            entry.key as String: '${entry.value}'
        },
        limits: {
          for (final entry in (json['limits'] as Map<dynamic, dynamic>? ?? {}).entries)
            if (entry.value is num) entry.key as String: (entry.value as num).toInt()
        },
      );
}

/// One line of a room transcript. Never stored: a session holds these in
/// memory and they go when it closes.
class RRCLine {
  const RRCLine({
    required this.room,
    required this.type,
    required this.source,
    required this.nick,
    required this.text,
    required this.at,
    required this.id,
    required this.own,
  });

  final String room;
  final int type;

  /// The identity hash the hub authenticated. A nick is advisory; this is
  /// the only part of a line that is proof of anything.
  final String source;
  final String nick;
  final String text;
  final double at;
  final String id;
  final bool own;

  bool get isAction => type == rrcTypeAction;
  bool get isNotice => type == rrcTypeNotice;

  /// What to show when the sender set no nick: the hash is the identity, so
  /// a prefix of it is the honest fallback.
  String get label => nick.isNotEmpty
      ? nick
      : (source.isEmpty ? 'unknown' : source.substring(0, source.length.clamp(0, 12)));

  factory RRCLine.fromJson(Map<String, dynamic> json) => RRCLine(
        room: json['room'] as String? ?? '',
        type: (json['type'] as num?)?.toInt() ?? rrcTypeMsg,
        source: json['source'] as String? ?? '',
        nick: json['nick'] as String? ?? '',
        text: json['text'] as String? ?? '',
        at: (json['at'] as num?)?.toDouble() ?? 0,
        id: json['id'] as String? ?? '',
        own: json['own'] as bool? ?? false,
      );
}

/// Our own hub, when hosting is switched on.
class RRCHosting {
  const RRCHosting({
    required this.enabled,
    required this.hubHash,
    required this.name,
    required this.clients,
    required this.rooms,
  });

  const RRCHosting.off()
      : enabled = false,
        hubHash = '',
        name = '',
        clients = 0,
        rooms = const {};

  final bool enabled;

  /// The destination clients dial. Empty while hosting is off.
  final String hubHash;
  final String name;
  final int clients;

  /// Room name to how many clients are in it.
  final Map<String, int> rooms;

  factory RRCHosting.fromJson(Map<String, dynamic> json) => RRCHosting(
        enabled: json['enabled'] as bool? ?? false,
        hubHash: json['hub_hash'] as String? ?? '',
        name: json['name'] as String? ?? '',
        clients: (json['clients'] as num?)?.toInt() ?? 0,
        rooms: {
          for (final entry in (json['rooms'] as Map<dynamic, dynamic>? ?? {}).entries)
            entry.key as String: (entry.value as num?)?.toInt() ?? 0
        },
      );
}

/// Everything the RRC surface needs in one read, from GET /rrc.
class RRCState {
  const RRCState({
    required this.session,
    required this.hubs,
    required this.nickname,
    required this.bookmarks,
    required this.rosters,
  });

  const RRCState.empty()
      : session = const RRCSession.idle(),
        hubs = const [],
        nickname = '',
        bookmarks = const [],
        rosters = const {};

  final RRCSession session;
  final List<RRCHub> hubs;
  final String nickname;
  final List<String> bookmarks;

  /// Room name to the identity hashes the hub last said were in it. A roster
  /// is optional in RRC and never authoritative.
  final Map<String, List<String>> rosters;

  factory RRCState.fromJson(Map<String, dynamic> json) => RRCState(
        session: RRCSession.fromJson(
            (json['session'] as Map<String, dynamic>?) ?? const {}),
        hubs: [
          for (final entry in (json['hubs'] as List<dynamic>? ?? []))
            RRCHub.fromJson(entry as Map<String, dynamic>)
        ],
        nickname: json['nickname'] as String? ?? '',
        bookmarks: [
          for (final entry in (json['bookmarks'] as List<dynamic>? ?? []))
            '$entry'
        ],
        rosters: {
          for (final entry in (json['rosters'] as Map<dynamic, dynamic>? ?? {}).entries)
            entry.key as String: [
              for (final member in (entry.value as List<dynamic>? ?? [])) '$member'
            ]
        },
      );
}
