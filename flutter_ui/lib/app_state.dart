// Central app state: owns the ApiClient/TcSocket, holds everything the
// three-column shell reads, and applies live WS events. Kept as one
// ChangeNotifier rather than a state-management package -- the surface
// area here is small enough that a package would add ceremony, not clarity.
import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;

import 'api/client.dart';
import 'api/events.dart';
import 'api/models/invite.dart';
import 'api/models/link_quality.dart';
import 'api/models/member.dart';
import 'api/models/message.dart';
import 'api/models/permissions.dart';
import 'api/models/server.dart';
import 'api/models/settings.dart';
import 'api/ws.dart';

class AppState extends ChangeNotifier {
  /// [httpClient] lets tests inject a mock transport; the real app leaves it
  /// null and gets a standard IO client.
  AppState({required String baseUrl, http.Client? httpClient})
      : api = ApiClient(baseUrl: baseUrl, client: httpClient),
        _socket = TcSocket(baseUrl: baseUrl);

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

  bool loading = true;
  String? error;

  /// Set by a failed mutating action (send, create, join, ...). Distinct from
  /// [error], which is fatal and takes over the whole screen on init failure --
  /// this one is transient and meant for a toast/snackbar, cleared on the next
  /// attempt. Single surface so every call site reports failures the same way.
  String? actionError;

  Channel? get selectedChannel {
    final hash = selectedChannelHash;
    if (hash == null) return null;
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

      loading = false;
      notifyListeners();

      _sub = _socket.events.listen(_onEvent);
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
      ]);
      membersByChannel[channelHashHex] = results[0] as List<Member>;
      messagesByChannel[channelHashHex] = results[1] as List<Message>;
      presenceByChannel[channelHashHex] = results[2] as List<PresenceEntry>;
      linkQualityByChannel[channelHashHex] = results[3] as ChannelLinkQuality;
      permissionsByChannel[channelHashHex] = results[4] as ChannelPermissions;
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

  Future<bool> sendMessage(String content) async {
    final channelHashHex = selectedChannelHash;
    if (channelHashHex == null || content.trim().isEmpty) return false;
    try {
      return await api.sendMessage(channelHashHex, content.trim());
    } catch (e) {
      _reportActionError(e);
      return false;
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

  Future<void> refreshInvites() async {
    try {
      pendingInvites = await api.getInvites();
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

  void _reportActionError(Object e) {
    actionError = e is ApiException ? e.message : e.toString();
    notifyListeners();
  }

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
        notifyListeners();
      case MemberListUpdatedEvent(:final channelHash):
        if (membersByChannel.containsKey(channelHash)) {
          unawaited(api.getMembers(channelHash).then((m) {
            membersByChannel[channelHash] = m;
            notifyListeners();
          }));
        }
      case ReactionUpdatedEvent():
        // Reaction counts arrive on the next message re-fetch; the mockup's
        // chips aren't latency-sensitive enough to warrant a per-reaction poll.
        break;
      case ChannelJoinedEvent():
        break;
      case ChannelDiscoveredEvent():
        unawaited(refreshDiscoveredChannels());
      case InviteReceivedEvent():
        unawaited(refreshInvites());
    }
  }

  @override
  void dispose() {
    _sub?.cancel();
    _socket.close();
    api.close();
    super.dispose();
  }
}
