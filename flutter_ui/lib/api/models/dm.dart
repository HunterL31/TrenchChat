/// One direct-message conversation, from GET /dms.
///
/// [hash] addresses it exactly like a channel hash does, so the message list,
/// composer and reaction widgets all take it unchanged.
class DmConversation {
  const DmConversation({
    required this.hash,
    required this.peerHash,
    required this.displayName,
    required this.createdAt,
    required this.lastMessageAt,
    required this.unread,
    required this.isOnline,
    required this.isFriend,
    this.peerIsTrenchchat = false,
  });

  final String hash;
  final String peerHash;

  /// The peer's self-asserted name; empty when nothing is known but the hash.
  final String displayName;
  final double createdAt;

  /// Unix seconds of the newest message, or 0 for an empty conversation.
  final double lastMessageAt;
  final int unread;
  final bool isOnline;

  /// False once the friendship ends: the transcript stays, but nothing more
  /// can pass either way.
  final bool isFriend;

  /// Whether the other end runs TrenchChat. A conversation works either way --
  /// the wire format is plain LXMF -- but reactions and other TrenchChat-only
  /// extras are not sent to a peer using a different client.
  final bool peerIsTrenchchat;

  factory DmConversation.fromJson(Map<String, dynamic> json) => DmConversation(
        hash: json['hash'] as String,
        peerHash: json['peer_hash'] as String,
        displayName: json['display_name'] as String? ?? '',
        createdAt: (json['created_at'] as num?)?.toDouble() ?? 0,
        lastMessageAt: (json['last_message_at'] as num?)?.toDouble() ?? 0,
        unread: (json['unread'] as num?)?.toInt() ?? 0,
        isOnline: json['is_online'] as bool? ?? false,
        isFriend: json['is_friend'] as bool? ?? false,
        peerIsTrenchchat: json['peer_is_trenchchat'] as bool? ?? false,
      );
}

/// Friend requests waiting on somebody, from GET /friends/requests.
class FriendRequests {
  const FriendRequests({required this.incoming, required this.outgoing});

  const FriendRequests.empty() : incoming = const [], outgoing = const [];

  /// Peers who have asked us. Their note is self-asserted text from someone
  /// we have no relationship with -- display it, never act on it.
  final List<FriendRequest> incoming;

  /// Peers we have asked, who have not answered.
  final List<FriendRequest> outgoing;

  factory FriendRequests.fromJson(Map<String, dynamic> json) => FriendRequests(
        incoming: _list(json['incoming']),
        outgoing: _list(json['outgoing']),
      );

  static List<FriendRequest> _list(dynamic raw) => (raw as List<dynamic>? ?? [])
      .map((e) => FriendRequest.fromJson(e as Map<String, dynamic>))
      .toList();
}

class FriendRequest {
  const FriendRequest({
    required this.identityHash,
    required this.displayName,
    required this.nickname,
    required this.note,
    required this.addedAt,
    this.message,
    this.messageCount = 0,
    this.fromTrenchchat = false,
  });

  final String identityHash;
  final String displayName;
  final String nickname;
  final String note;
  final double addedAt;

  /// The most recent message this peer sent while unaccepted, or null when
  /// they asked with a handshake instead. Peer-written text from someone we
  /// have no relationship with -- display it, never act on it.
  final String? message;

  /// How many of their messages are being held. Only the newest is shown; all
  /// of them are filed into the conversation on accept.
  final int messageCount;

  /// Whether the held message came from TrenchChat. False means a client with
  /// no friend-request concept, for which messaging is the only way to ask.
  final bool fromTrenchchat;

  /// True when this peer asked with words rather than a handshake.
  bool get isMessageRequest => messageCount > 0;

  factory FriendRequest.fromJson(Map<String, dynamic> json) => FriendRequest(
        identityHash: json['identity_hash'] as String,
        displayName: json['display_name'] as String? ?? '',
        nickname: json['nickname'] as String? ?? '',
        note: json['note'] as String? ?? '',
        addedAt: (json['added_at'] as num?)?.toDouble() ?? 0,
        message: json['message'] as String?,
        messageCount: (json['message_count'] as num?)?.toInt() ?? 0,
        fromTrenchchat: json['from_trenchchat'] as bool? ?? false,
      );
}

/// Where offline direct messages are left, from GET /propagation.
class PropagationStatus {
  const PropagationStatus({
    required this.selected,
    required this.pinned,
    required this.nodes,
    required this.syncState,
  });

  const PropagationStatus.none()
      : selected = null,
        pinned = '',
        nodes = const [],
        syncState = 0;

  /// The node in use, or null when none has been heard yet -- in which case a
  /// message to an offline friend can only wait in the local queue.
  final String? selected;

  /// The node the user fixed, or empty when the choice is automatic.
  final String pinned;
  final List<PropagationNode> nodes;

  /// LXMF's transfer state for the last collection attempt.
  final int syncState;

  factory PropagationStatus.fromJson(Map<String, dynamic> json) => PropagationStatus(
        selected: json['selected'] as String?,
        pinned: json['pinned'] as String? ?? '',
        nodes: (json['nodes'] as List<dynamic>? ?? [])
            .map((e) => PropagationNode.fromJson(e as Map<String, dynamic>))
            .toList(),
        syncState: (json['sync_state'] as num?)?.toInt() ?? 0,
      );
}

class PropagationNode {
  const PropagationNode({
    required this.hash,
    required this.hops,
    required this.lastHeard,
    required this.selected,
  });

  final String hash;
  final int hops;
  final double lastHeard;
  final bool selected;

  factory PropagationNode.fromJson(Map<String, dynamic> json) => PropagationNode(
        hash: json['hash'] as String,
        hops: (json['hops'] as num?)?.toInt() ?? 0,
        lastHeard: (json['last_heard'] as num?)?.toDouble() ?? 0,
        selected: json['selected'] as bool? ?? false,
      );
}
