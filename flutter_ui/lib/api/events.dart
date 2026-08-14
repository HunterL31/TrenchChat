// Typed wrapper around the /ws event bus in devtools/testenv/api.py's
// EventBus.emit(). Only the event types this UI reacts to are modeled;
// unrecognized types are ignored.
import 'dart:convert';

import 'models/message.dart';

sealed class TcEvent {
  const TcEvent();

  static TcEvent? tryParse(String raw) {
    final Map<String, dynamic> json;
    try {
      json = jsonDecode(raw) as Map<String, dynamic>;
    } catch (_) {
      return null;
    }
    switch (json['type'] as String?) {
      case 'message':
        final channelHash = json['channel_hash'] as String;
        final messageJson = json['message'] as Map<String, dynamic>?;
        if (messageJson == null) return null;
        return MessageEvent(channelHash, Message.fromJson(messageJson));
      case 'presence':
        return PresenceEvent(json['identity_hash'] as String, json['is_online'] as bool);
      case 'reaction_updated':
        return ReactionUpdatedEvent(
          json['channel_hash'] as String,
          json['message_id'] as String,
        );
      case 'member_list_updated':
        return MemberListUpdatedEvent(json['channel_hash'] as String);
      case 'channel_joined':
        return ChannelJoinedEvent(
          json['channel_hash'] as String,
          json['channel_name'] as String,
        );
      default:
        return null;
    }
  }
}

class MessageEvent extends TcEvent {
  const MessageEvent(this.channelHash, this.message);
  final String channelHash;
  final Message message;
}

class PresenceEvent extends TcEvent {
  const PresenceEvent(this.identityHash, this.isOnline);
  final String identityHash;
  final bool isOnline;
}

class ReactionUpdatedEvent extends TcEvent {
  const ReactionUpdatedEvent(this.channelHash, this.messageId);
  final String channelHash;
  final String messageId;
}

class MemberListUpdatedEvent extends TcEvent {
  const MemberListUpdatedEvent(this.channelHash);
  final String channelHash;
}

class ChannelJoinedEvent extends TcEvent {
  const ChannelJoinedEvent(this.channelHash, this.channelName);
  final String channelHash;
  final String channelName;
}
