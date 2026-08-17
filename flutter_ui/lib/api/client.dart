// REST client over the devtools/testenv/api.py surface. Mutating calls hit
// the exact same actions.py entry points the Qt GUI and the test harness UI
// use -- see api.py's module docstring.
import 'dart:convert';
import 'dart:typed_data';

import 'package:http/http.dart' as http;

import 'models/emoji.dart';
import 'models/friend.dart';
import 'models/interface.dart';
import 'models/invite.dart';
import 'models/link_quality.dart';
import 'models/member.dart';
import 'models/message.dart';
import 'models/network_map.dart';
import 'models/permissions.dart';
import 'models/server.dart';
import 'models/settings.dart';

/// Thrown for any non-2xx response. [message] prefers the backend's own
/// `{"error": "..."}` body (used for expected failures like a permission
/// gate), falling back to FastAPI's validation `{"detail": ...}` shape or a
/// bare status line.
class ApiException implements Exception {
  const ApiException(this.statusCode, this.message);

  final int statusCode;
  final String message;

  @override
  String toString() => 'ApiException($statusCode): $message';
}

class ApiClient {
  ApiClient({required this.baseUrl, http.Client? client}) : _http = client ?? http.Client();

  final String baseUrl;
  final http.Client _http;

  static const Map<String, String> _jsonHeaders = {'content-type': 'application/json'};

  Uri _u(String path) => Uri.parse('$baseUrl$path');

  /// The backend labels JSON responses `application/json` with no charset, and
  /// package:http falls back to latin1 when the charset is missing -- decode the
  /// bytes as UTF-8 here so non-ASCII names and message text survive the trip.
  String _bodyText(http.Response res) => utf8.decode(res.bodyBytes, allowMalformed: true);

  /// Uniform response handling: throws [ApiException] for any non-2xx status,
  /// otherwise returns the decoded JSON body.
  dynamic _decode(http.Response res) {
    if (res.statusCode < 200 || res.statusCode >= 300) {
      String message = 'HTTP ${res.statusCode}';
      try {
        final body = jsonDecode(_bodyText(res));
        if (body is Map<String, dynamic>) {
          if (body['error'] is String) {
            message = body['error'] as String;
          } else if (body['detail'] != null) {
            message = body['detail'].toString();
          }
        }
      } on FormatException {
        // Body wasn't JSON; fall back to the bare status message above.
      }
      throw ApiException(res.statusCode, message);
    }
    try {
      return jsonDecode(_bodyText(res));
    } on FormatException {
      throw ApiException(res.statusCode, 'Malformed response body');
    }
  }

  Future<Map<String, dynamic>> getMe() async {
    final res = await _http.get(_u('/me'));
    return _decode(res) as Map<String, dynamic>;
  }

  Future<List<Server>> getServers() async {
    final res = await _http.get(_u('/servers'));
    return (_decode(res) as List<dynamic>)
        .map((e) => Server.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  Future<String> createServer(String name, String description) async {
    final res = await _http.post(
      _u('/servers'),
      headers: _jsonHeaders,
      body: jsonEncode({'name': name, 'description': description}),
    );
    return (_decode(res) as Map<String, dynamic>)['hash'] as String;
  }

  Future<List<Channel>> getServerChannels(String serverHashHex) async {
    final res = await _http.get(_u('/servers/$serverHashHex/channels'));
    return (_decode(res) as List<dynamic>)
        .map((e) => Channel.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  /// Creates a channel inside a server. Throws [ApiException] with status 403
  /// when the caller lacks `create_channel` on that server.
  Future<String> createServerChannel(
      String serverHashHex, String name, String description) async {
    final res = await _http.post(
      _u('/servers/$serverHashHex/channels'),
      headers: _jsonHeaders,
      body: jsonEncode({'name': name, 'description': description}),
    );
    return (_decode(res) as Map<String, dynamic>)['hash'] as String;
  }

  /// Joined standalone (non-server) channels.
  Future<List<Channel>> getChannels() async {
    final res = await _http.get(_u('/channels'));
    return (_decode(res) as List<dynamic>)
        .map((e) => Channel.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  /// Standalone channels announced on the mesh but not yet joined.
  Future<List<Channel>> getDiscoveredChannels() async {
    final res = await _http.get(_u('/channels/discovered'));
    return (_decode(res) as List<dynamic>)
        .map((e) => Channel.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  /// [access] is `"public"` or `"invite"`.
  Future<String> createChannel(String name, String description, String access) async {
    final res = await _http.post(
      _u('/channels'),
      headers: _jsonHeaders,
      body: jsonEncode({'name': name, 'description': description, 'access': access}),
    );
    return (_decode(res) as Map<String, dynamic>)['hash'] as String;
  }

  Future<bool> joinChannel(String channelHashHex) async {
    final res = await _http.post(_u('/channels/$channelHashHex/join'));
    return (_decode(res) as Map<String, dynamic>)['ok'] as bool? ?? false;
  }

  Future<List<Member>> getMembers(String channelHashHex) async {
    final res = await _http.get(_u('/channels/$channelHashHex/members'));
    return (_decode(res) as List<dynamic>)
        .map((e) => Member.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  Future<List<Member>> getServerMembers(String serverHashHex) async {
    final res = await _http.get(_u('/servers/$serverHashHex/members'));
    return (_decode(res) as List<dynamic>)
        .map((e) => Member.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  Future<List<Message>> getMessages(String channelHashHex) async {
    final res = await _http.get(_u('/channels/$channelHashHex/messages'));
    return (_decode(res) as List<dynamic>)
        .map((e) => Message.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  /// [reason] is the backend's machine-readable cause when [ok] is false
  /// (`no_send_permission`, `no_recipients`).
  Future<({bool ok, String? reason})> sendMessage(
      String channelHashHex, String content) async {
    final res = await _http.post(
      _u('/channels/$channelHashHex/messages'),
      headers: _jsonHeaders,
      body: jsonEncode({'content': content}),
    );
    final body = _decode(res) as Map<String, dynamic>;
    return (ok: body['ok'] as bool? ?? false, reason: body['reason'] as String?);
  }

  Future<ChannelPermissions> getMyPermissions(String channelHashHex) async {
    final res = await _http.get(_u('/channels/$channelHashHex/my_permissions'));
    return ChannelPermissions.fromJson(_decode(res) as Map<String, dynamic>);
  }

  Future<PresenceEntry> getPeerPresence(String peerHashHex) async {
    final res = await _http.get(_u('/peers/$peerHashHex/presence'));
    return PresenceEntry.fromJson(_decode(res) as Map<String, dynamic>);
  }

  Future<Uint8List?> getPeerAvatar(String peerHashHex) async {
    // A peer with no avatar is routine, and an unreachable backend must not
    // throw out of a widget build -- treat any decode/status failure as
    // "no avatar" rather than propagating.
    final Map<String, dynamic> body;
    try {
      final res = await _http.get(_u('/peers/$peerHashHex/avatar'));
      body = _decode(res) as Map<String, dynamic>;
    } catch (_) {
      return null;
    }
    final b64 = body['avatar_data_b64'] as String?;
    if (b64 == null) return null;
    return base64Decode(b64);
  }

  Future<void> addReaction(String channelHashHex, String messageId, String emojiHash) async {
    final res = await _http.post(
      _u('/channels/$channelHashHex/messages/$messageId/reactions'),
      headers: _jsonHeaders,
      body: jsonEncode({'emoji_hash': emojiHash}),
    );
    _decode(res);
  }

  Future<void> removeReaction(String channelHashHex, String messageId, String emojiHash) async {
    final res = await _http.delete(_u('/channels/$channelHashHex/messages/$messageId/reactions/$emojiHash'));
    _decode(res);
  }

  Future<List<Friend>> getFriends() async {
    final res = await _http.get(_u('/friends'));
    return (_decode(res) as List<dynamic>)
        .map((e) => Friend.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  Future<bool> addFriend(String identityHashHex, String nickname, String note) async {
    final res = await _http.post(
      _u('/friends'),
      headers: _jsonHeaders,
      body: jsonEncode({'identity_hash': identityHashHex, 'nickname': nickname, 'note': note}),
    );
    return (_decode(res) as Map<String, dynamic>)['ok'] as bool? ?? false;
  }

  /// Partial update: omit [nickname] or [note] to leave it unchanged.
  Future<bool> updateFriend(String identityHashHex, {String? nickname, String? note}) async {
    final body = <String, dynamic>{};
    if (nickname != null) body['nickname'] = nickname;
    if (note != null) body['note'] = note;
    final res = await _http.put(
      _u('/friends/$identityHashHex'),
      headers: _jsonHeaders,
      body: jsonEncode(body),
    );
    return (_decode(res) as Map<String, dynamic>)['ok'] as bool? ?? false;
  }

  Future<bool> removeFriend(String identityHashHex) async {
    final res = await _http.delete(_u('/friends/$identityHashHex'));
    return (_decode(res) as Map<String, dynamic>)['ok'] as bool? ?? false;
  }

  Future<Map<String, dynamic>> getNetworkMap() async {
    final res = await _http.get(_u('/network/map'));
    return _decode(res) as Map<String, dynamic>;
  }

  Future<NetworkMapData> getNetworkMapData() async =>
      NetworkMapData.fromJson(await getNetworkMap());

  Future<TcSettings> getSettings() async {
    final res = await _http.get(_u('/settings'));
    return TcSettings.fromJson(_decode(res) as Map<String, dynamic>);
  }

  Future<void> updateSettings(TcSettings settings) async {
    final res = await _http.post(
      _u('/settings'),
      headers: _jsonHeaders,
      body: jsonEncode(settings.toJson()),
    );
    _decode(res);
  }

  Future<void> setDisplayName(String displayName) async {
    final res = await _http.post(
      _u('/me/display_name'),
      headers: _jsonHeaders,
      body: jsonEncode({'display_name': displayName}),
    );
    _decode(res);
  }

  Future<List<RetInterface>> getInterfaces() async {
    final res = await _http.get(_u('/reticulum/interfaces'));
    return (_decode(res) as List<dynamic>)
        .map((e) => RetInterface.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  Future<void> createInterface(String name, String type, bool enabled,
      Map<String, String> typeValues, Map<String, String> commonValues) async {
    final res = await _http.post(
      _u('/reticulum/interfaces'),
      headers: _jsonHeaders,
      body: jsonEncode({
        'name': name,
        'type': type,
        'enabled': enabled,
        'type_values': typeValues,
        'common_values': commonValues,
      }),
    );
    _decode(res);
  }

  Future<void> updateInterface(String name, String type, bool enabled,
      Map<String, String> typeValues, Map<String, String> commonValues) async {
    final res = await _http.put(
      _u('/reticulum/interfaces/${Uri.encodeComponent(name)}'),
      headers: _jsonHeaders,
      body: jsonEncode({
        'type': type,
        'enabled': enabled,
        'type_values': typeValues,
        'common_values': commonValues,
      }),
    );
    _decode(res);
  }

  Future<void> deleteInterface(String name) async {
    final res =
        await _http.delete(_u('/reticulum/interfaces/${Uri.encodeComponent(name)}'));
    _decode(res);
  }

  Future<List<DirectoryEntry>> searchDirectory(String query) async {
    final res = await _http.get(_u('/directory?q=${Uri.encodeQueryComponent(query)}'));
    return (_decode(res) as List<dynamic>)
        .map((e) => DirectoryEntry.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  Future<void> inviteToChannel(String channelHashHex, String peerHashHex) async {
    final res = await _http.post(
      _u('/channels/$channelHashHex/invite'),
      headers: _jsonHeaders,
      body: jsonEncode({'peer_hash_hex': peerHashHex}),
    );
    _decode(res);
  }

  Future<List<PendingInvite>> getInvites() async {
    final res = await _http.get(_u('/invites'));
    return (_decode(res) as List<dynamic>)
        .map((e) => PendingInvite.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  Future<bool> acceptInvite(String channelHashHex) async {
    final res = await _http.post(_u('/invites/$channelHashHex/accept'));
    return (_decode(res) as Map<String, dynamic>)['ok'] as bool? ?? false;
  }

  Future<void> declineInvite(String channelHashHex) async {
    final res = await _http.post(_u('/invites/$channelHashHex/decline'));
    _decode(res);
  }

  Future<ScopePermissions> getChannelPermissions(String channelHashHex) async {
    final res = await _http.get(_u('/channels/$channelHashHex/permissions'));
    return ScopePermissions.fromJson(_decode(res) as Map<String, dynamic>);
  }

  /// Returns false when the backend's MANAGE_CHANNEL gate dropped the change.
  Future<bool> updateChannelPermissions(
      String channelHashHex, List<String> admin, List<String> member) async {
    final res = await _http.post(
      _u('/channels/$channelHashHex/permissions'),
      headers: _jsonHeaders,
      body: jsonEncode({'admin': admin, 'member': member}),
    );
    return (_decode(res) as Map<String, dynamic>)['ok'] as bool? ?? false;
  }

  Future<List<CustomEmoji>> getEmoji() async {
    final res = await _http.get(_u('/emoji'));
    return (_decode(res) as List<dynamic>)
        .map((e) => CustomEmoji.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  Future<void> importEmoji(String name, String imageDataB64) async {
    final res = await _http.post(
      _u('/emoji/import'),
      headers: _jsonHeaders,
      body: jsonEncode({'name': name, 'image_data_b64': imageDataB64}),
    );
    _decode(res);
  }

  /// Kick members and grant/revoke admin in one call. Returns false when the
  /// backend's own KICK/MANAGE_ROLES gate silently dropped the request.
  Future<bool> updateChannelRoles(
    String channelHashHex, {
    List<String> removeMembers = const [],
    List<String> addAdmins = const [],
    List<String> removeAdmins = const [],
  }) async {
    final res = await _http.post(
      _u('/channels/$channelHashHex/roles'),
      headers: _jsonHeaders,
      body: jsonEncode({
        'remove_members': removeMembers,
        'add_admins': addAdmins,
        'remove_admins': removeAdmins,
      }),
    );
    return (_decode(res) as Map<String, dynamic>)['ok'] as bool? ?? false;
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
