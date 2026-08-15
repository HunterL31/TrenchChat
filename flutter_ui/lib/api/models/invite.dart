/// One entry of GET /invites -- an invite awaiting accept/decline. The token
/// stays server-side; accept/decline are keyed by channel hash alone.
class PendingInvite {
  const PendingInvite({
    required this.channelHashHex,
    required this.channelName,
    required this.expiry,
    required this.adminHex,
    required this.scopeKind,
  });

  final String channelHashHex;
  final String channelName;

  /// Unix seconds after which the token is no longer accepted.
  final double expiry;

  /// Identity hash of the admin who sent the invite.
  final String adminHex;

  /// `"server"` or `"channel"`.
  final String scopeKind;

  factory PendingInvite.fromJson(Map<String, dynamic> json) => PendingInvite(
        channelHashHex: json['channel_hash_hex'] as String,
        channelName: json['channel_name'] as String? ?? '',
        expiry: (json['expiry'] as num?)?.toDouble() ?? 0,
        adminHex: json['admin_hex'] as String? ?? '',
        scopeKind: json['scope_kind'] as String? ?? 'channel',
      );
}

/// One entry of GET /directory -- a peer heard via a trenchchat.user announce.
class DirectoryEntry {
  const DirectoryEntry({
    required this.identityHash,
    required this.displayName,
    required this.isOnline,
  });

  final String identityHash;
  final String displayName;
  final bool isOnline;

  factory DirectoryEntry.fromJson(Map<String, dynamic> json) => DirectoryEntry(
        identityHash: json['identity_hash'] as String,
        displayName: json['display_name'] as String? ?? '',
        isOnline: json['is_online'] as bool? ?? false,
      );
}
