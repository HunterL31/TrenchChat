class Member {
  const Member({
    required this.channelHash,
    required this.identityHash,
    required this.displayName,
    required this.role,
    required this.addedAt,
  });

  final String channelHash;
  final String identityHash;
  final String displayName;
  final String role;
  final double addedAt;

  factory Member.fromJson(Map<String, dynamic> json) => Member(
        channelHash: json['channel_hash'] as String,
        identityHash: json['identity_hash'] as String,
        displayName: json['display_name'] as String? ?? '',
        role: json['role'] as String? ?? 'member',
        addedAt: (json['added_at'] as num).toDouble(),
      );
}

class PresenceEntry {
  const PresenceEntry({
    required this.identityHash,
    required this.isOnline,
    this.displayName,
  });

  final String identityHash;
  final bool isOnline;
  final String? displayName;

  factory PresenceEntry.fromJson(Map<String, dynamic> json) => PresenceEntry(
        identityHash: json['identity_hash'] as String,
        isOnline: json['is_online'] as bool? ?? false,
      );
}
