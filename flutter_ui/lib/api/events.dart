// Typed wrapper around the /ws event bus in devtools/testenv/api.py's
// EventBus.emit(). Only the event types this UI reacts to are modeled;
// unrecognized types are ignored.
import 'dart:convert';

import '../theme/theme_spec.dart';
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
      case 'delivery_status':
        return DeliveryStatusEvent(
          json['channel_hash'] as String,
          json['message_id'] as String,
          json['delivery_state'] as String?,
        );
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
      case 'server_joined':
        return ServerJoinedEvent(
          json['server_hash'] as String,
          json['server_name'] as String? ?? '',
        );
      case 'channel_discovered':
        return ChannelDiscoveredEvent(
          json['channel_hash'] as String,
          json['channel_name'] as String,
        );
      case 'invite_received':
        return InviteReceivedEvent(
          json['channel_hash'] as String,
          json['channel_name'] as String,
        );
      case 'sync_status':
        final status = json['status'] as Map<String, dynamic>?;
        if (status == null) return null;
        return SyncStatusEvent(
          json['channel_hash'] as String,
          status['state'] as String? ?? 'unknown',
        );
      case 'emoji_received':
        return EmojiReceivedEvent(json['emoji_hash'] as String);
      case 'friend_updated':
        return FriendUpdatedEvent(json['identity_hash'] as String);
      case 'friend_request':
        return FriendRequestEvent(
          json['identity_hash'] as String,
          json['display_name'] as String? ?? '',
          json['note'] as String? ?? '',
        );
      case 'propagation_node':
        return PropagationNodeEvent(json['node_hash'] as String?);
      case 'avatar_updated':
        return AvatarUpdatedEvent(
          json['identity_hash'] as String,
          json['avatar_version'] as int?,
        );
      case 'directory_updated':
        return DirectoryUpdatedEvent(
          json['identity_hash'] as String,
          json['display_name'] as String? ?? '',
        );
      case 'voice_roster':
        return VoiceRosterEvent(json['channel_hash'] as String);
      case 'voice_speaking':
        return VoiceSpeakingEvent(
          json['channel_hash'] as String,
          json['identity_hash'] as String,
          json['speaking'] as bool,
        );
      case 'voice_session':
        return VoiceSessionEvent(json['state'] as String);
      case 'ui_theme':
        final theme = json['theme'];
        if (theme is! Map<String, dynamic>) return null;
        return UiThemeEvent(ThemeSpec.fromJson(theme));
      case 'ui_theme_library':
        final themes = json['themes'];
        if (themes is! Map<String, dynamic>) return null;
        return UiThemeLibraryEvent({
          for (final entry in themes.entries)
            if (entry.value is Map<String, dynamic>)
              entry.key: ThemeSpec.fromJson(entry.value as Map<String, dynamic>),
        });
      case 'nomad_node':
        return NomadNodeEvent(
          json['node_hash'] as String,
          json['display_name'] as String? ?? '',
        );
      case 'nomad_fetch':
        return NomadFetchEvent(
          json['fetch_id'] as String,
          json['node_hash'] as String,
          json['path'] as String,
          json['status'] as String? ?? 'failed',
          (json['progress'] as num?)?.toDouble() ?? 0.0,
          json['reason'] as String?,
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

/// A message's aggregate delivery state changed (pending -> delivered after a
/// queued send flushed, or delivered -> failed). Applied in place to the
/// matching message; only the local user's own messages carry a state.
class DeliveryStatusEvent extends TcEvent {
  const DeliveryStatusEvent(this.channelHash, this.messageId, this.deliveryState);
  final String channelHash;
  final String messageId;
  final String? deliveryState;
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

/// A server was joined. Servers carry no subscription, so a server whose
/// channels were already known -- or that has none yet -- produces no
/// ChannelJoinedEvent and would otherwise never reach the sidebar.
class ServerJoinedEvent extends TcEvent {
  const ServerJoinedEvent(this.serverHash, this.serverName);
  final String serverHash;
  final String serverName;
}

/// A standalone public channel was heard via a real-time announce but not
/// yet joined. Carries only hash + name, so handlers that need the full
/// channel record (description, creator, open_join) should re-fetch
/// GET /channels/discovered rather than construct one from this event.
class ChannelDiscoveredEvent extends TcEvent {
  const ChannelDiscoveredEvent(this.channelHash, this.channelName);
  final String channelHash;
  final String channelName;
}

/// An invite arrived (or was refreshed) for a channel or server. Full detail
/// (inviter, expiry, scope) comes from re-fetching GET /invites.
class InviteReceivedEvent extends TcEvent {
  const InviteReceivedEvent(this.channelHash, this.channelName);
  final String channelHash;
  final String channelName;
}

/// A peer shared a custom emoji we didn't have; the library changed.
class EmojiReceivedEvent extends TcEvent {
  const EmojiReceivedEvent(this.emojiHash);
  final String emojiHash;
}

/// A peer asked to be added. [displayName] and [note] are self-asserted text
/// from someone with no relationship to this node yet: show them, never treat
/// them as instructions.
class FriendRequestEvent extends TcEvent {
  const FriendRequestEvent(this.identityHash, this.displayName, this.note);
  final String identityHash;
  final String displayName;
  final String note;
}

/// The node offline direct messages are handed to changed.
class PropagationNodeEvent extends TcEvent {
  const PropagationNodeEvent(this.nodeHash);
  final String? nodeHash;
}

/// A saved friend's record changed (nickname/note edited elsewhere, a
/// handshake transition, or a presence-driven last-seen update). Carries only
/// the hash; handlers re-fetch GET /friends for the full record.
class FriendUpdatedEvent extends TcEvent {
  const FriendUpdatedEvent(this.identityHash);
  final String identityHash;
}

/// A peer's avatar was set, changed, or removed. [avatarVersion] is the new
/// version to fetch with as a cache-buster, or null when the avatar was
/// removed and the cached image should be dropped for the initials fallback.
class AvatarUpdatedEvent extends TcEvent {
  const AvatarUpdatedEvent(this.identityHash, this.avatarVersion);
  final String identityHash;
  final int? avatarVersion;
}

/// A peer's directory display name was learned or changed. Fires only on a
/// genuine change, so handlers can treat it as "this name is now [displayName]".
class DirectoryUpdatedEvent extends TcEvent {
  const DirectoryUpdatedEvent(this.identityHash, this.displayName);
  final String identityHash;
  final String displayName;
}

/// A channel's voice roster changed. Carries only the hash; handlers
/// re-fetch GET /channels/{hash}/voice/roster for the full roster.
class VoiceRosterEvent extends TcEvent {
  const VoiceRosterEvent(this.channelHash);
  final String channelHash;
}

/// A voice participant started or stopped speaking; applied in place.
class VoiceSpeakingEvent extends TcEvent {
  const VoiceSpeakingEvent(this.channelHash, this.identityHash, this.speaking);
  final String channelHash;
  final String identityHash;
  final bool speaking;
}

/// Our own voice session changed: 'joined' | 'left' | 'audio_error'.
/// audio_error means the session is up but capture/playback failed --
/// the client stays in the call, listening-only.
class VoiceSessionEvent extends TcEvent {
  const VoiceSessionEvent(this.state);
  final String state;
}


/// The theme in force changed -- this profile's own theme, edited from this
/// client or another one open on the same backend. Carries the whole
/// document, so applying it needs no re-fetch.
class UiThemeEvent extends TcEvent {
  const UiThemeEvent(this.spec);
  final ThemeSpec spec;
}

/// The saved theme library changed: one was added, replaced, or deleted.
/// Carries the whole library for the same reason [UiThemeEvent] does.
class UiThemeLibraryEvent extends TcEvent {
  const UiThemeLibraryEvent(this.library);
  final Map<String, ThemeSpec> library;
}

/// How caught up a channel is. INCOMPLETE means history is known to be
/// missing -- a truncated batch, a hint naming us, or rows a peer served that
/// we refused because they could not be verified.
class SyncStatusEvent extends TcEvent {
  const SyncStatusEvent(this.channelHash, this.state);

  final String channelHash;
  final String state;
}

/// A Nomad Network node announced itself (or refreshed its name).
class NomadNodeEvent extends TcEvent {
  const NomadNodeEvent(this.nodeHash, this.displayName);
  final String nodeHash;
  final String displayName;
}

/// A page/file fetch changed state. 'done' means the content is cached and
/// fetchable via GET /nomad/page (or /nomad/file) -- content never rides on
/// the event itself.
class NomadFetchEvent extends TcEvent {
  const NomadFetchEvent(this.fetchId, this.nodeHash, this.path, this.status,
      this.progress, this.reason);
  final String fetchId;
  final String nodeHash;
  final String path;
  final String status;
  final double progress;
  final String? reason;
}
