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

/// The states a shared file can be in for this node, as api.py reports them.
/// [fileStateAvailable] is a manifest nobody here has asked for yet;
/// [fileStateDone] means the bytes are held, whether they were downloaded or
/// shared from here; [fileStateUnavailable] means no member holding it is
/// reachable, which is the normal case on a mesh rather than a failure.
const String fileStateAvailable = 'available';
const String fileStateQueued = 'queued';
const String fileStateFetching = 'fetching';
const String fileStateDone = 'done';
const String fileStateUnavailable = 'unavailable';
const String fileStateFailed = 'failed';

/// The manifest a message names, plus how the download of it is doing. The
/// bytes are never part of a message: they are fetched from a member who
/// holds them, on request.
class FileAttachment {
  const FileAttachment({
    required this.name,
    required this.size,
    required this.hash,
    required this.state,
    required this.progress,
    this.reason,
  });

  final String name;
  final int size;

  /// SHA-256 of the file, which is its address everywhere.
  final String hash;

  final String state;

  /// Chunks verified over chunks total, so it never goes backwards.
  final double progress;

  /// Why a download failed: `refused`, `corrupt` or `storage`. Null otherwise.
  final String? reason;

  bool get isDone => state == fileStateDone;
  bool get inFlight => state == fileStateQueued || state == fileStateFetching;

  FileAttachment withFetch(String state, double progress, String? reason) =>
      FileAttachment(
        name: name,
        size: size,
        hash: hash,
        state: state,
        progress: progress,
        reason: reason,
      );

  factory FileAttachment.fromJson(Map<String, dynamic> json) => FileAttachment(
        name: json['name'] as String? ?? '',
        size: (json['size'] as num?)?.toInt() ?? 0,
        hash: json['hash'] as String? ?? '',
        state: json['state'] as String? ?? fileStateAvailable,
        progress: (json['progress'] as num?)?.toDouble() ?? 0.0,
        reason: json['reason'] as String?,
      );
}

/// A download's own snapshot, from the fetch endpoints and the file_fetch
/// event. [messageIds] names the messages the file is attached to, which is
/// how a live update finds the cards to move.
class FileFetch {
  const FileFetch({
    required this.fileHash,
    required this.state,
    required this.progress,
    this.reason,
    this.messageIds = const [],
    this.channels = const [],
  });

  final String fileHash;
  final String state;
  final double progress;
  final String? reason;
  final List<String> messageIds;
  final List<String> channels;

  factory FileFetch.fromJson(Map<String, dynamic> json) => FileFetch(
        fileHash: json['file_hash'] as String? ?? '',
        state: json['state'] as String? ?? fileStateAvailable,
        progress: (json['progress'] as num?)?.toDouble() ?? 0.0,
        reason: json['reason'] as String?,
        messageIds: (json['message_ids'] as List<dynamic>? ?? const [])
            .map((e) => e.toString())
            .toList(),
        channels: (json['channels'] as List<dynamic>? ?? const [])
            .map((e) => e.toString())
            .toList(),
      );
}

/// What the file store holds against each of its three budgets, and the
/// largest file this backend will share.
class FileUsage {
  const FileUsage({
    required this.own,
    required this.received,
    required this.partial,
    required this.ownLimit,
    required this.receivedLimit,
    required this.partialLimit,
    required this.maxFileBytes,
  });

  final int own;
  final int received;
  final int partial;
  final int ownLimit;
  final int receivedLimit;
  final int partialLimit;
  final int maxFileBytes;

  factory FileUsage.fromJson(Map<String, dynamic> json) {
    final usage = json['usage'] as Map<String, dynamic>? ?? const {};
    final limits = json['limits'] as Map<String, dynamic>? ?? const {};
    int at(Map<String, dynamic> m, String key) => (m[key] as num?)?.toInt() ?? 0;
    return FileUsage(
      own: at(usage, 'own'),
      received: at(usage, 'received'),
      partial: at(usage, 'partial'),
      ownLimit: at(limits, 'own'),
      receivedLimit: at(limits, 'received'),
      partialLimit: at(limits, 'partial'),
      maxFileBytes: (json['max_file_bytes'] as num?)?.toInt() ?? 0,
    );
  }
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
    this.file,
    this.fileStripped = false,
    this.deliveryState,
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

  /// The file this message names, or null when it names none.
  final FileAttachment? file;

  /// The message arrived with a file manifest we refused, for the same
  /// reasons and with the same answer as [imageStripped].
  final bool fileStripped;

  final List<Reaction> reactions;

  /// Not yet served by the backend (Phase B seam). Always null today, so
  /// the "received late" marker never lights -- see ApiClient.messageIsLate.
  final double? receivedAt;

  /// Delivery state of the local user's own outbound message: "pending",
  /// "delivered", "failed", or null (not tracked -- a peer's message, or one
  /// aged out of the backend's tracker). Drives the per-row status glyph.
  final String? deliveryState;

  /// A [Message] identical to this one but with a new [deliveryState], for
  /// applying a live delivery_status event in place.
  Message withDeliveryState(String? state) => Message(
        messageId: messageId,
        senderHash: senderHash,
        senderName: senderName,
        content: content,
        timestamp: timestamp,
        replyTo: replyTo,
        hasImage: hasImage,
        reactions: reactions,
        receivedAt: receivedAt,
        imageStripped: imageStripped,
        file: file,
        fileStripped: fileStripped,
        deliveryState: state,
      );

  /// A [Message] identical to this one but with a new [file], for applying a
  /// live file_fetch event in place.
  Message withFile(FileAttachment? attachment) => Message(
        messageId: messageId,
        senderHash: senderHash,
        senderName: senderName,
        content: content,
        timestamp: timestamp,
        replyTo: replyTo,
        hasImage: hasImage,
        reactions: reactions,
        receivedAt: receivedAt,
        imageStripped: imageStripped,
        file: attachment,
        fileStripped: fileStripped,
        deliveryState: deliveryState,
      );

  factory Message.fromJson(Map<String, dynamic> json) => Message(
        messageId: json['message_id'] as String,
        senderHash: json['sender_hash'] as String,
        senderName: json['sender_name'] as String? ?? '',
        content: json['content'] as String? ?? '',
        timestamp: (json['timestamp'] as num).toDouble(),
        replyTo: json['reply_to'] as String?,
        hasImage: json['has_image'] as bool? ?? false,
        imageStripped: json['image_stripped'] as bool? ?? false,
        file: json['file'] is Map<String, dynamic>
            ? FileAttachment.fromJson(json['file'] as Map<String, dynamic>)
            : null,
        fileStripped: json['file_stripped'] as bool? ?? false,
        reactions: (json['reactions'] as List<dynamic>? ?? [])
            .map((r) => Reaction.fromJson(r as Map<String, dynamic>))
            .toList(),
        // TODO(phase-b): populate once `received_at` ships on _message_to_dict.
        receivedAt: null,
        deliveryState: json['delivery_state'] as String?,
      );
}
