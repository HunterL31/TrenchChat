// Central app state: owns the ApiClient/TcSocket, holds everything the
// three-column shell reads, and applies live WS events. Kept as one
// ChangeNotifier rather than a state-management package -- the surface
// area here is small enough that a package would add ceremony, not clarity.
import 'dart:async';

import 'package:flutter/foundation.dart';

import 'api/client.dart';
import 'api/events.dart';
import 'api/models/link_quality.dart';
import 'api/models/member.dart';
import 'api/models/message.dart';
import 'api/models/permissions.dart';
import 'api/models/server.dart';
import 'api/ws.dart';

class AppState extends ChangeNotifier {
  AppState({required String baseUrl})
      : api = ApiClient(baseUrl: baseUrl),
        _socket = TcSocket(baseUrl: baseUrl);

  final ApiClient api;
  final TcSocket _socket;
  StreamSubscription<TcEvent>? _sub;

  String meHashHex = '';
  String meDisplayName = '';

  List<Server> servers = [];
  List<Channel> standaloneChannels = [];
  final Map<String, List<Channel>> channelsByServer = {};

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
      for (final s in servers) {
        channelsByServer[s.hash] = await api.getServerChannels(s.hash);
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
    return api.sendMessage(channelHashHex, content.trim());
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
