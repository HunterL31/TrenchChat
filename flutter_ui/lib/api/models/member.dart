import 'upgrade.dart';

class Member {
  const Member({
    required this.channelHash,
    required this.identityHash,
    required this.displayName,
    required this.role,
    required this.addedAt,
    this.path = PeerPath.unknown,
  });

  final String channelHash;
  final String identityHash;
  final String displayName;
  final String role;
  final double addedAt;

  /// Which path this node reached the member over when the row was read.
  /// A path_changed event moves on without the row, so what the UI renders
  /// is AppState.pathFor; this is where that map is filled from.
  final PeerPath path;

  factory Member.fromJson(Map<String, dynamic> json) => Member(
        channelHash: json['channel_hash'] as String,
        identityHash: json['identity_hash'] as String,
        displayName: json['display_name'] as String? ?? '',
        role: json['role'] as String? ?? 'member',
        addedAt: (json['added_at'] as num).toDouble(),
        path: peerPathFrom(json['path'] as String?),
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
        displayName: (json['display_name'] as String?)?.isNotEmpty == true
            ? json['display_name'] as String
            : null,
      );
}
