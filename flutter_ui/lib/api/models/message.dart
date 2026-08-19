class Reaction {
  const Reaction({required this.emojiHash, required this.count, required this.reactedByMe});

  final String emojiHash;
  final int count;
  final bool reactedByMe;

  factory Reaction.fromJson(Map<String, dynamic> json) => Reaction(
        emojiHash: json['emoji_hash'] as String,
        count: json['count'] as int,
        reactedByMe: json['reacted_by_me'] as bool? ?? false,
      );
}

class Message {
  const Message({
    required this.messageId,
    required this.senderHash,
    required this.senderName,
    required this.content,
    required this.timestamp,
    required this.replyTo,
    required this.hasImage,
    required this.reactions,
    this.receivedAt,
    this.imageStripped = false,
  });

  final String messageId;
  final String senderHash;
  final String senderName;
  final String content;
  final double timestamp;
  final String? replyTo;
  final bool hasImage;

  /// The message arrived with an attachment we refused -- over the size cap,
  /// or a header declaring an implausible decode. The text is kept; saying so
  /// is better than a message that silently looks like it never had one.
  final bool imageStripped;

  final List<Reaction> reactions;

  /// Not yet served by the backend (Phase B seam). Always null today, so
  /// the "received late" marker never lights -- see ApiClient.messageIsLate.
  final double? receivedAt;

  factory Message.fromJson(Map<String, dynamic> json) => Message(
        messageId: json['message_id'] as String,
        senderHash: json['sender_hash'] as String,
        senderName: json['sender_name'] as String? ?? '',
        content: json['content'] as String? ?? '',
        timestamp: (json['timestamp'] as num).toDouble(),
        replyTo: json['reply_to'] as String?,
        hasImage: json['has_image'] as bool? ?? false,
        imageStripped: json['image_stripped'] as bool? ?? false,
        reactions: (json['reactions'] as List<dynamic>? ?? [])
            .map((r) => Reaction.fromJson(r as Map<String, dynamic>))
            .toList(),
        // TODO(phase-b): populate once `received_at` ships on _message_to_dict.
        receivedAt: null,
      );
}
