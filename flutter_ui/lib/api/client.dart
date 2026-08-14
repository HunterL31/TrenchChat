// REST client over the devtools/testenv/api.py surface. Mutating calls hit
// the exact same actions.py entry points the Qt GUI and the test harness UI
// use -- see api.py's module docstring.
import 'dart:convert';
import 'dart:typed_data';

import 'package:http/http.dart' as http;

import 'models/link_quality.dart';
import 'models/member.dart';
import 'models/message.dart';
import 'models/permissions.dart';
import 'models/server.dart';

class ApiClient {
  ApiClient({required this.baseUrl}) : _http = http.Client();

  final String baseUrl;
  final http.Client _http;

  Uri _u(String path) => Uri.parse('$baseUrl$path');

  Future<Map<String, dynamic>> getMe() async {
    final res = await _http.get(_u('/me'));
    return jsonDecode(res.body) as Map<String, dynamic>;
  }

  Future<List<Server>> getServers() async {
    final res = await _http.get(_u('/servers'));
    return (jsonDecode(res.body) as List<dynamic>)
        .map((e) => Server.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  Future<List<Channel>> getServerChannels(String serverHashHex) async {
    final res = await _http.get(_u('/servers/$serverHashHex/channels'));
    return (jsonDecode(res.body) as List<dynamic>)
        .map((e) => Channel.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  /// Joined standalone (non-server) channels.
  Future<List<Channel>> getChannels() async {
    final res = await _http.get(_u('/channels'));
    return (jsonDecode(res.body) as List<dynamic>)
        .map((e) => Channel.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  Future<List<Member>> getMembers(String channelHashHex) async {
    final res = await _http.get(_u('/channels/$channelHashHex/members'));
    return (jsonDecode(res.body) as List<dynamic>)
        .map((e) => Member.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  Future<List<Member>> getServerMembers(String serverHashHex) async {
    final res = await _http.get(_u('/servers/$serverHashHex/members'));
    return (jsonDecode(res.body) as List<dynamic>)
        .map((e) => Member.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  Future<List<Message>> getMessages(String channelHashHex) async {
    final res = await _http.get(_u('/channels/$channelHashHex/messages'));
    return (jsonDecode(res.body) as List<dynamic>)
        .map((e) => Message.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  Future<bool> sendMessage(String channelHashHex, String content) async {
    final res = await _http.post(
      _u('/channels/$channelHashHex/messages'),
      headers: {'content-type': 'application/json'},
      body: jsonEncode({'content': content}),
    );
    final body = jsonDecode(res.body) as Map<String, dynamic>;
    return body['ok'] as bool? ?? false;
  }

  Future<ChannelPermissions> getMyPermissions(String channelHashHex) async {
    final res = await _http.get(_u('/channels/$channelHashHex/my_permissions'));
    return ChannelPermissions.fromJson(jsonDecode(res.body) as Map<String, dynamic>);
  }

  Future<PresenceEntry> getPeerPresence(String peerHashHex) async {
    final res = await _http.get(_u('/peers/$peerHashHex/presence'));
    return PresenceEntry.fromJson(jsonDecode(res.body) as Map<String, dynamic>);
  }

  Future<Uint8List?> getPeerAvatar(String peerHashHex) async {
    final res = await _http.get(_u('/peers/$peerHashHex/avatar'));
    // A peer with no avatar is routine, and an unreachable backend must not
    // throw out of a widget build -- treat anything undecodable as "no avatar".
    final Object? body;
    try {
      body = jsonDecode(res.body);
    } on FormatException {
      return null;
    }
    if (body is! Map<String, dynamic>) return null;
    final b64 = body['avatar_data_b64'] as String?;
    if (b64 == null) return null;
    return base64Decode(b64);
  }

  Future<void> addReaction(String channelHashHex, String messageId, String emojiHash) async {
    await _http.post(
      _u('/channels/$channelHashHex/messages/$messageId/reactions'),
      headers: {'content-type': 'application/json'},
      body: jsonEncode({'emoji_hash': emojiHash}),
    );
  }

  Future<void> removeReaction(String channelHashHex, String messageId, String emojiHash) async {
    await _http.delete(_u('/channels/$channelHashHex/messages/$messageId/reactions/$emojiHash'));
  }

  Future<Map<String, dynamic>> getNetworkMap() async {
    final res = await _http.get(_u('/network/map'));
    return jsonDecode(res.body) as Map<String, dynamic>;
  }

  // --- Phase B seams -----------------------------------------------------
  // These two endpoints don't exist on the backend yet. Each is a thin,
  // clearly-marked composition of endpoints that do, so swapping in the
  // real thing later is a one-line change at the call site.

  /// TODO(phase-b): replace with a single GET /channels/{h}/presence call.
  Future<List<PresenceEntry>> getChannelPresence(String channelHashHex) async {
    final members = await getMembers(channelHashHex);
    return Future.wait(members.map((m) => getPeerPresence(m.identityHash)));
  }

  /// TODO(phase-b): replace with a single GET /channels/{h}/link_quality call.
  /// Until then this reads /network/map and takes the worst hop count among
  /// the channel's members that appear in the map; anyone not represented
  /// there is UNKNOWN rather than guessed.
  Future<ChannelLinkQuality> getChannelLinkQuality(String channelHashHex) async {
    final members = await getMembers(channelHashHex);
    final memberHashes = members.map((m) => m.identityHash).toSet();
    final map = await getNetworkMap();
    final nodes = (map['nodes'] as List<dynamic>? ?? []).cast<Map<String, dynamic>>();

    int? worstHops;
    for (final n in nodes) {
      final id = n['id'] as String?;
      if (id == null || !memberHashes.contains(id)) continue;
      final hops = n['hops'] as int?;
      if (hops == null) continue;
      if (worstHops == null || hops > worstHops) worstHops = hops;
    }
    if (worstHops == null) return ChannelLinkQuality.unknown;

    final LinkQualityLevel level;
    if (worstHops <= 1) {
      level = LinkQualityLevel.excellent;
    } else if (worstHops <= 2) {
      level = LinkQualityLevel.good;
    } else if (worstHops <= 4) {
      level = LinkQualityLevel.fair;
    } else {
      level = LinkQualityLevel.poor;
    }
    return ChannelLinkQuality(level: level, hops: worstHops);
  }

  void close() => _http.close();
}
