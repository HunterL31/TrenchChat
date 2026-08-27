class Friend {
  const Friend({
    required this.identityHash,
    required this.nickname,
    required this.note,
    required this.displayName,
    required this.addedAt,
    required this.lastSeenAt,
    required this.isOnline,
    this.state = 'accepted',
    this.nomadNodeHash,
  });

  final String identityHash;
  final String nickname;
  final String note;

  /// The peer's self-asserted name -- distinct from [nickname], which is
  /// local and panel-only.
  final String displayName;
  final double addedAt;

  /// Unix seconds; 0 means never seen.
  final double lastSeenAt;
  final bool isOnline;

  /// 'accepted', 'pending_in' or 'pending_out'. Only an accepted friend can
  /// exchange direct messages; the others are a handshake in progress.
  final String state;

  /// The nomad node this friend hosts, when one has been heard on the mesh.
  /// Null means no known page.
  final String? nomadNodeHash;

  factory Friend.fromJson(Map<String, dynamic> json) => Friend(
        identityHash: json['identity_hash'] as String,
        nickname: json['nickname'] as String? ?? '',
        note: json['note'] as String? ?? '',
        displayName: json['display_name'] as String? ?? '',
        addedAt: (json['added_at'] as num).toDouble(),
        lastSeenAt: (json['last_seen_at'] as num).toDouble(),
        isOnline: json['is_online'] as bool? ?? false,
        state: json['state'] as String? ?? 'accepted',
        nomadNodeHash: json['nomad_node_hash'] as String?,
      );
}
