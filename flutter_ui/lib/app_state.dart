// Central app state: owns the ApiClient/TcSocket, holds everything the
// three-column shell reads, and applies live WS events. Kept as one
// ChangeNotifier rather than a state-management package -- the surface
// area here is small enough that a package would add ceremony, not clarity.
import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;

import 'api/client.dart';
import 'api/events.dart';
import 'api/models/emoji.dart';
import 'api/models/friend.dart';
import 'api/models/invite.dart';
import 'api/models/link_quality.dart';
import 'api/models/member.dart';
import 'api/models/message.dart';
import 'api/models/permissions.dart';
import 'api/models/server.dart';
import 'api/models/settings.dart';
import 'api/models/voice.dart';
import 'api/ws.dart';

/// How long reaction events for one channel are coalesced before the
/// channel's messages are re-fetched.
const Duration _reactionRefreshWindow = Duration(milliseconds: 250);

class AppState extends ChangeNotifier {
  /// [httpClient] lets tests inject a mock transport; the real app leaves it
  /// null and gets a standard IO client.
  AppState({required String baseUrl, http.Client? httpClient, String token = ''})
      : api = ApiClient(baseUrl: baseUrl, client: httpClient, token: token),
        _socket = TcSocket(baseUrl: baseUrl, token: token);

  final ApiClient api;
  final TcSocket _socket;
  StreamSubscription<TcEvent>? _sub;

  String meHashHex = '';
  String meDisplayName = '';

  List<Server> servers = [];
  List<Channel> standaloneChannels = [];
  List<Channel> discoveredChannels = [];
  List<PendingInvite> pendingInvites = [];
  final Map<String, List<Channel>> channelsByServer = {};
  final Map<String, int> serverMemberCounts = {};

  String? selectedServerHash;
  String? selectedChannelHash;

  final Map<String, List<Member>> membersByChannel = {};
  final Map<String, List<Message>> messagesByChannel = {};
  final Map<String, List<PresenceEntry>> presenceByChannel = {};
  final Map<String, ChannelLinkQuality> linkQualityByChannel = {};
  final Map<String, ChannelPermissions> permissionsByChannel = {};
  final Map<String, Uint8List?> avatarCache = {};

  final Map<String, List<VoiceParticipant>> voiceRosterByChannel = {};

  /// The live voice session, straight from GET /voice/status; idle when not
  /// in a call. Refreshed on session events and by [_voicePollTimer].
  VoiceStatus voiceStatus = VoiceStatus.idle;

  /// Optimistic local mute state; reconciled from the backend on each poll.
  bool voiceMuted = false;

  /// Set by a `voice_session: audio_error` event: the session is up but
  /// capture/playback failed -- we stay in the call, listening-only.
  bool voiceAudioError = false;
  Timer? _voicePollTimer;

  /// Per-channel debounce for reaction refreshes. A sync backfill or a burst
  /// of reactions fires one event each; without coalescing that is one full
  /// message re-fetch per reaction.
  final Map<String, Timer> _reactionRefreshTimers = {};

  String? get voiceChannelHash => voiceStatus.channel;
  LinkQualityLevel get voiceQualityLevel => voiceOverallLevel(voiceStatus);

  /// Custom emoji library, keyed by emoji hash. Loaded lazily on first
  /// [ensureEmojiLoaded] and kept fresh on [EmojiReceivedEvent].
  final Map<String, CustomEmoji> customEmojis = {};
  bool _emojisLoaded = false;

  /// Locally saved contacts. Tab-only: never used as the display name in
  /// message bubbles or the presence roster (see friends_tab.dart).
  List<Friend> friends = [];

  bool loading = true;
  String? error;

  /// Set by a failed mutating action (send, create, join, ...). Distinct from
  /// [error], which is fatal and takes over the whole screen on init failure --
  /// this one is transient and meant for a toast/snackbar, cleared on the next
  /// attempt. Single surface so every call site reports failures the same way.
  String? actionError;

  Channel? get selectedChannel {
    final hash = selectedChannelHash;
    return hash == null ? null : channelByHash(hash);
  }

  Channel? channelByHash(String hash) {
    for (final c in standaloneChannels) {
      if (c.hash == hash) return c;
    }
    for (final list in channelsByServer.values) {
      for (final c in list) {
        if (c.hash == hash) return c;
      }
    }
    return null;
  }

  Future<void> init() async {
    try {
      final me = await api.getMe();
      meHashHex = me['hash_hex'] as String;
      meDisplayName = me['display_name'] as String;

      servers = await api.getServers();
      standaloneChannels = await api.getChannels();
      pendingInvites = await api.getInvites();
      for (final s in servers) {
        channelsByServer[s.hash] = await api.getServerChannels(s.hash);
        serverMemberCounts[s.hash] = (await api.getServerMembers(s.hash)).length;
      }

      selectedServerHash = servers.isNotEmpty ? servers.first.hash : null;
      final initialChannels = selectedServerHash != null
          ? channelsByServer[selectedServerHash] ?? []
          : standaloneChannels;
      selectedChannelHash = initialChannels.isNotEmpty
          ? initialChannels.first.hash
          : (standaloneChannels.isNotEmpty ? standaloneChannels.first.hash : null);

      if (selectedChannelHash != null) {
        await loadChannel(selectedChannelHash!);
      }

      await loadFriends();

      loading = false;
      notifyListeners();

      _socket.onReconnected = _onSocketReconnected;
      _sub = _socket.events.listen(_onEvent);
      unawaited(ensureEmojiLoaded());
      // The backend session outlives client restarts; pick it up if live.
      await refreshVoiceStatus();
      if (voiceStatus.channel != null) _startVoicePoll();
    } catch (e) {
      error = e.toString();
      loading = false;
      notifyListeners();
    }
  }

  Future<void> loadChannel(String channelHashHex) async {
    try {
      final results = await Future.wait([
        api.getMembers(channelHashHex),
        api.getMessages(channelHashHex),
        api.getChannelPresence(channelHashHex),
        api.getChannelLinkQuality(channelHashHex),
        api.getMyPermissions(channelHashHex),
        api.getVoiceRoster(channelHashHex),
      ]);
      membersByChannel[channelHashHex] = results[0] as List<Member>;
      messagesByChannel[channelHashHex] = results[1] as List<Message>;
      presenceByChannel[channelHashHex] = results[2] as List<PresenceEntry>;
      linkQualityByChannel[channelHashHex] = results[3] as ChannelLinkQuality;
      permissionsByChannel[channelHashHex] = results[4] as ChannelPermissions;
      voiceRosterByChannel[channelHashHex] = results[5] as List<VoiceParticipant>;
      notifyListeners();
    } catch (e) {
      _reportActionError(e);
    }
  }

  Future<void> selectServer(String serverHashHex) async {
    selectedServerHash = serverHashHex;
    final chans = channelsByServer[serverHashHex] ?? [];
    if (chans.isNotEmpty) {
      await selectChannel(chans.first.hash);
    } else {
      notifyListeners();
    }
  }

  Future<void> selectChannel(String channelHashHex) async {
    selectedChannelHash = channelHashHex;
    notifyListeners();
    if (!messagesByChannel.containsKey(channelHashHex)) {
      await loadChannel(channelHashHex);
    } else {
      // Refresh presence/link quality; message history is kept live via WS.
      unawaited(loadChannel(channelHashHex));
    }
  }

  Future<Uint8List?> avatarFor(String identityHashHex) async {
    if (avatarCache.containsKey(identityHashHex)) return avatarCache[identityHashHex];
    final data = await api.getPeerAvatar(identityHashHex);
    avatarCache[identityHashHex] = data;
    notifyListeners();
    return data;
  }

  /// Anything whose WS events may have been missed while the socket was down.
  void _onSocketReconnected() {
    final channelHash = selectedChannelHash;
    if (channelHash != null) unawaited(loadChannel(channelHash));
    unawaited(refreshInvites());
    unawaited(refreshEmoji());
    unawaited(refreshVoiceStatus());
  }

  Future<bool> sendMessage(String content) async {
    final channelHashHex = selectedChannelHash;
    if (channelHashHex == null || content.trim().isEmpty) return false;
    try {
      final result = await api.sendMessage(channelHashHex, content.trim());
      if (result.ok) {
        // The WS event echoes it too; this covers a dropped socket so the
        // sender always sees their own message land.
        unawaited(refreshMessages(channelHashHex));
        return true;
      }
      actionError = switch (result.reason) {
        'no_send_permission' => "You don't have permission to send in this channel.",
        'no_recipients' =>
          'Not sent: no known subscribers to deliver to yet. Try again once peers are online.',
        _ => 'Message was not sent.',
      };
      notifyListeners();
      return false;
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  Future<void> refreshMessages(String channelHashHex) async {
    try {
      messagesByChannel[channelHashHex] = await api.getMessages(channelHashHex);
      notifyListeners();
    } catch (_) {
      // Next WS event or channel reload will catch it up.
    }
  }

  /// Standalone channels announced on the mesh but not yet joined. Refreshed
  /// on demand (join dialog open) and live on [ChannelDiscoveredEvent].
  Future<void> refreshDiscoveredChannels() async {
    try {
      discoveredChannels = await api.getDiscoveredChannels();
      notifyListeners();
    } catch (e) {
      _reportActionError(e);
    }
  }

  /// Returns the new server's hash, or null (with [actionError] set) on failure.
  Future<String?> createServer(String name, String description) async {
    try {
      final hash = await api.createServer(name, description);
      servers = await api.getServers();
      channelsByServer.putIfAbsent(hash, () => []);
      serverMemberCounts[hash] = (await api.getServerMembers(hash)).length;
      notifyListeners();
      return hash;
    } catch (e) {
      _reportActionError(e);
      return null;
    }
  }

  /// Creates a channel inside [serverHashHex], inheriting the server's
  /// permissions. Returns the new channel's hash, or null (with
  /// [actionError] set -- e.g. missing create_channel permission) on failure.
  Future<String?> createChannelInServer(
      String serverHashHex, String name, String description) async {
    try {
      final hash = await api.createServerChannel(serverHashHex, name, description);
      channelsByServer[serverHashHex] = await api.getServerChannels(serverHashHex);
      selectedServerHash = serverHashHex;
      notifyListeners();
      await selectChannel(hash);
      return hash;
    } catch (e) {
      _reportActionError(e);
      return null;
    }
  }

  /// Creates a standalone channel with [access] `"public"` or `"invite"`.
  /// Returns the new channel's hash, or null (with [actionError] set) on failure.
  Future<String?> createStandaloneChannel(
      String name, String description, String access) async {
    try {
      final hash = await api.createChannel(name, description, access);
      standaloneChannels = await api.getChannels();
      notifyListeners();
      await selectChannel(hash);
      return hash;
    } catch (e) {
      _reportActionError(e);
      return null;
    }
  }

  /// Joins a previously-discovered standalone public channel.
  Future<bool> joinChannel(String channelHashHex) async {
    try {
      final ok = await api.joinChannel(channelHashHex);
      if (ok) {
        standaloneChannels = await api.getChannels();
        discoveredChannels = discoveredChannels.where((c) => c.hash != channelHashHex).toList();
        notifyListeners();
        await selectChannel(channelHashHex);
      }
      return ok;
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  /// Joins [channelHashHex]'s voice session. Returns false (with
  /// [actionError] set) when the backend refused the join.
  Future<bool> joinVoice(String channelHashHex) async {
    try {
      final ok = await api.joinVoice(channelHashHex);
      if (ok) {
        await refreshVoiceStatus();
        await refreshVoiceRoster(channelHashHex);
        _startVoicePoll();
        return true;
      }
      // The backend gives no machine-readable reason yet.
      actionError =
          "Couldn't join voice — no permission, already in a call, or the room is full.";
      notifyListeners();
      return false;
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  Future<bool> leaveVoice() async {
    final oldChannel = voiceStatus.channel;
    try {
      final ok = await api.leaveVoice();
      _stopVoicePoll();
      voiceStatus = VoiceStatus.idle;
      voiceAudioError = false;
      notifyListeners();
      if (oldChannel != null) unawaited(refreshVoiceRoster(oldChannel));
      return ok;
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  /// Optimistic: flips the local state immediately, reverts on failure.
  Future<bool> toggleVoiceMute() async {
    final target = !voiceMuted;
    voiceMuted = target;
    notifyListeners();
    try {
      await api.setVoiceMuted(target);
      return true;
    } catch (e) {
      voiceMuted = !target;
      _reportActionError(e);
      return false;
    }
  }

  Future<void> refreshVoiceRoster(String channelHashHex) async {
    try {
      voiceRosterByChannel[channelHashHex] = await api.getVoiceRoster(channelHashHex);
      notifyListeners();
    } catch (_) {
      // Next WS event or poll tick will catch it up.
    }
  }

  Future<void> refreshVoiceStatus() async {
    try {
      voiceStatus = await api.getVoiceStatus();
      voiceMuted = voiceStatus.muted;
      notifyListeners();
    } catch (_) {
      // Next poll tick will catch it up.
    }
  }

  /// Quality isn't pushed over WS, so poll /voice/status while in a session;
  /// the roster refresh also self-heals any missed voice_roster event.
  void _startVoicePoll() {
    _voicePollTimer ??= Timer.periodic(const Duration(seconds: 4), (_) {
      unawaited(refreshVoiceStatus());
      final channelHash = voiceStatus.channel;
      if (channelHash != null) unawaited(refreshVoiceRoster(channelHash));
    });
  }

  void _stopVoicePoll() {
    _voicePollTimer?.cancel();
    _voicePollTimer = null;
  }

  Future<void> refreshInvites() async {
    try {
      pendingInvites = await api.getInvites();
      notifyListeners();
    } catch (e) {
      _reportActionError(e);
    }
  }

  Future<void> loadFriends() async {
    try {
      friends = await api.getFriends();
      notifyListeners();
    } catch (e) {
      _reportActionError(e);
    }
  }

  /// Accepts a pending invite and joins its channel/server. Returns true on
  /// success; on failure [actionError] is set.
  Future<bool> acceptInvite(String channelHashHex) async {
    try {
      final ok = await api.acceptInvite(channelHashHex);
      pendingInvites =
          pendingInvites.where((i) => i.channelHashHex != channelHashHex).toList();
      if (ok) {
        // The accepted scope may be a server or a standalone channel; refresh
        // both lists rather than guessing from scope_kind.
        servers = await api.getServers();
        standaloneChannels = await api.getChannels();
        for (final s in servers) {
          channelsByServer[s.hash] ??= await api.getServerChannels(s.hash);
        }
      }
      notifyListeners();
      return ok;
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  /// Returns true on success, or false (with [actionError] set) on failure.
  Future<bool> addFriend(String identityHashHex, String nickname, String note) async {
    try {
      final ok = await api.addFriend(identityHashHex, nickname, note);
      if (ok) await loadFriends();
      return ok;
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  Future<void> declineInvite(String channelHashHex) async {
    try {
      await api.declineInvite(channelHashHex);
      pendingInvites =
          pendingInvites.where((i) => i.channelHashHex != channelHashHex).toList();
      notifyListeners();
    } catch (e) {
      _reportActionError(e);
    }
  }

  /// Sends an invite for the channel. Returns true on success; on failure
  /// [actionError] is set.
  Future<bool> inviteToChannel(String channelHashHex, String peerHashHex) async {
    try {
      await api.inviteToChannel(channelHashHex, peerHashHex);
      return true;
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  /// Kick/promote/demote, then refresh the member list. Returns false when
  /// the backend's permission gate dropped the request.
  Future<bool> updateChannelRoles(
    String channelHashHex, {
    List<String> removeMembers = const [],
    List<String> addAdmins = const [],
    List<String> removeAdmins = const [],
  }) async {
    try {
      final ok = await api.updateChannelRoles(
        channelHashHex,
        removeMembers: removeMembers,
        addAdmins: addAdmins,
        removeAdmins: removeAdmins,
      );
      membersByChannel[channelHashHex] = await api.getMembers(channelHashHex);
      notifyListeners();
      return ok;
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  /// Partial update: omit [nickname] or [note] to leave it unchanged.
  Future<bool> updateFriend(String identityHashHex, {String? nickname, String? note}) async {
    try {
      final ok = await api.updateFriend(identityHashHex, nickname: nickname, note: note);
      if (ok) await loadFriends();
      return ok;
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  /// Replaces the role-permission matrix. Returns false when the backend's
  /// MANAGE_CHANNEL gate dropped the change.
  Future<bool> updateChannelPermissions(
      String channelHashHex, List<String> admin, List<String> member) async {
    try {
      return await api.updateChannelPermissions(channelHashHex, admin, member);
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  /// Loads the custom emoji library once; safe to call from build paths.
  Future<void> ensureEmojiLoaded() async {
    if (_emojisLoaded) return;
    _emojisLoaded = true;
    await refreshEmoji();
  }

  Future<void> refreshEmoji() async {
    try {
      final list = await api.getEmoji();
      customEmojis
        ..clear()
        ..addEntries(list.map((e) => MapEntry(e.emojiHash, e)));
      notifyListeners();
    } catch (_) {
      // A missing emoji library is cosmetic; chips fall back to hash text.
    }
  }

  /// Imports a custom emoji. Returns true on success; on failure
  /// [actionError] is set.
  Future<bool> importEmoji(String name, String imageDataB64) async {
    try {
      await api.importEmoji(name, imageDataB64);
      await refreshEmoji();
      return true;
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  /// Saves the propagation/outbound settings. Returns true on success; on
  /// failure [actionError] is set.
  Future<bool> saveSettings(TcSettings settings) async {
    try {
      await api.updateSettings(settings);
      return true;
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  /// Sets the display name and re-announces. Returns true on success.
  Future<bool> saveDisplayName(String displayName) async {
    try {
      await api.setDisplayName(displayName);
      meDisplayName = displayName;
      notifyListeners();
      return true;
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  Future<bool> removeFriend(String identityHashHex) async {
    try {
      final ok = await api.removeFriend(identityHashHex);
      if (ok) await loadFriends();
      return ok;
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  void _reportActionError(Object e) {
    actionError = e is ApiException ? e.message : e.toString();
    notifyListeners();
  }

  /// Applies a socket event directly, so tests can exercise event handling
  /// without standing up a WebSocket.
  @visibleForTesting
  void applyEvent(TcEvent event) => _onEvent(event);

  void _onEvent(TcEvent event) {
    switch (event) {
      case MessageEvent(:final channelHash, :final message):
        final list = messagesByChannel.putIfAbsent(channelHash, () => []);
        final idx = list.indexWhere((m) => m.messageId == message.messageId);
        if (idx >= 0) {
          list[idx] = message;
        } else {
          list.add(message);
        }
        notifyListeners();
      case PresenceEvent(:final identityHash, :final isOnline):
        for (final entry in presenceByChannel.entries) {
          final list = entry.value;
          final idx = list.indexWhere((p) => p.identityHash == identityHash);
          if (idx >= 0) {
            list[idx] = PresenceEntry(identityHash: identityHash, isOnline: isOnline);
          }
        }
        final friendIdx = friends.indexWhere((f) => f.identityHash == identityHash);
        if (friendIdx >= 0) {
          final f = friends[friendIdx];
          friends[friendIdx] = Friend(
            identityHash: f.identityHash,
            nickname: f.nickname,
            note: f.note,
            displayName: f.displayName,
            addedAt: f.addedAt,
            lastSeenAt: f.lastSeenAt,
            isOnline: isOnline,
          );
        }
        notifyListeners();
      case MemberListUpdatedEvent(:final channelHash):
        if (membersByChannel.containsKey(channelHash)) {
          unawaited(api.getMembers(channelHash).then((m) {
            membersByChannel[channelHash] = m;
            notifyListeners();
          }));
        }
      case ReactionUpdatedEvent(:final channelHash):
        _scheduleReactionRefresh(channelHash);
      case ChannelJoinedEvent():
        break;
      case ChannelDiscoveredEvent():
        unawaited(refreshDiscoveredChannels());
      case InviteReceivedEvent():
        unawaited(refreshInvites());
      case EmojiReceivedEvent():
        unawaited(refreshEmoji());
      case FriendUpdatedEvent():
        unawaited(loadFriends());
      case VoiceRosterEvent(:final channelHash):
        if (channelHash == selectedChannelHash ||
            channelHash == voiceStatus.channel ||
            voiceRosterByChannel.containsKey(channelHash)) {
          unawaited(refreshVoiceRoster(channelHash));
        }
      case VoiceSpeakingEvent(:final channelHash, :final identityHash, :final speaking):
        final roster = voiceRosterByChannel[channelHash];
        if (roster != null) {
          final idx = roster.indexWhere((p) => p.identityHash == identityHash);
          if (idx >= 0) {
            roster[idx] = roster[idx].copyWith(speaking: speaking);
            notifyListeners();
          }
        }
      case VoiceSessionEvent(:final state):
        switch (state) {
          case 'joined':
            unawaited(refreshVoiceStatus());
            _startVoicePoll();
          case 'left':
            _stopVoicePoll();
            voiceStatus = VoiceStatus.idle;
            voiceAudioError = false;
            notifyListeners();
          case 'audio_error':
            voiceAudioError = true;
            notifyListeners();
        }
    }
  }

  /// Re-fetch a channel's messages so updated reaction chips render, at most
  /// once per [_reactionRefreshWindow] however many reactions land in it.
  void _scheduleReactionRefresh(String channelHash) {
    if (!messagesByChannel.containsKey(channelHash)) return;
    if (_reactionRefreshTimers.containsKey(channelHash)) return;
    _reactionRefreshTimers[channelHash] = Timer(_reactionRefreshWindow, () {
      _reactionRefreshTimers.remove(channelHash);
      unawaited(refreshMessages(channelHash));
    });
  }

  @override
  void dispose() {
    for (final t in _reactionRefreshTimers.values) {
      t.cancel();
    }
    _reactionRefreshTimers.clear();
    _voicePollTimer?.cancel();
    _sub?.cancel();
    _socket.close();
    api.close();
    super.dispose();
  }
}
