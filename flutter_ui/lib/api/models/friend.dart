class Friend {
  const Friend({
    required this.identityHash,
    required this.nickname,
    required this.note,
    required this.displayName,
    required this.addedAt,
    required this.lastSeenAt,
    required this.isOnline,
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

  factory Friend.fromJson(Map<String, dynamic> json) => Friend(
        identityHash: json['identity_hash'] as String,
        nickname: json['nickname'] as String? ?? '',
        note: json['note'] as String? ?? '',
        displayName: json['display_name'] as String? ?? '',
        addedAt: (json['added_at'] as num).toDouble(),
        lastSeenAt: (json['last_seen_at'] as num).toDouble(),
        isOnline: json['is_online'] as bool? ?? false,
      );
}
