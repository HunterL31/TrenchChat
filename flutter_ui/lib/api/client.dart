// REST client over the devtools/testenv/api.py surface. Mutating calls hit
// the exact same actions.py entry points the Qt GUI and the test harness UI
// use -- see api.py's module docstring.
import 'dart:convert';
import 'dart:typed_data';

import 'package:http/http.dart' as http;

import 'models/app_version.dart';
import 'models/bandwidth.dart';
import 'models/discovery.dart';
import 'models/dm.dart';
import 'models/emoji.dart';
import 'models/friend.dart';
import 'models/interface.dart';
import 'models/invite.dart';
import 'models/link_quality.dart';
import 'models/member.dart';
import 'models/message.dart';
import 'models/network_map.dart';
import 'models/nomad.dart';
import 'models/permissions.dart';
import 'models/server.dart';
import 'models/settings.dart';
import 'models/voice.dart';

/// Directory scopes accepted by GET /directory.
const String directoryScopeAll = 'all';
const String directoryScopeFriends = 'friends';
const String directoryScopeShared = 'shared';

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

const String tokenHeader = 'x-tc-token';

/// Adds the backend's API token to every request. The backend rejects
/// unauthenticated calls -- without a token it would be a remote control for
/// this identity, reachable by any process or web page that can hit the port.
class _TokenClient extends http.BaseClient {
  _TokenClient(this._inner, this._token);

  final http.Client _inner;
  final String _token;

  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) {
    request.headers[tokenHeader] = _token;
    return _inner.send(request);
  }

  @override
  void close() => _inner.close();
}

class ApiClient {
  ApiClient({required this.baseUrl, http.Client? client, String token = ''})
      : _token = token,
        _http = token.isEmpty
            ? (client ?? http.Client())
            : _TokenClient(client ?? http.Client(), token);

  final String baseUrl;
  final String _token;
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

  Future<AppVersionInfo> getVersion() async {
    final res = await _http.get(_u('/version'));
    return AppVersionInfo.fromJson(_decode(res) as Map<String, dynamic>);
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

  /// Unread message counts per subscribed channel, own messages excluded.
  Future<Map<String, int>> getChannelUnread() async {
    final res = await _http.get(_u('/channels/unread'));
    final body = _decode(res) as Map<String, dynamic>;
    final counts = body['counts'] as Map<String, dynamic>? ?? {};
    return counts.map((k, v) => MapEntry(k, (v as num).toInt()));
  }

  /// Advances the channel's read watermark to now.
  Future<bool> markChannelRead(String channelHashHex) async {
    final res = await _http.post(_u('/channels/$channelHashHex/read'));
    return (_decode(res) as Map<String, dynamic>)['ok'] as bool? ?? false;
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

  /// Unsubscribes from a standalone channel. Stored history is kept; ok=false
  /// means the backend has no such channel.
  Future<bool> leaveChannel(String channelHashHex) async {
    final res = await _http.post(_u('/channels/$channelHashHex/leave'));
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

  /// A page of a channel's messages. With [beforeTs] set the backend returns
  /// up to [limit] messages older than it; without it, the newest [limit].
  /// Either way the page is ordered ascending for display.
  Future<List<Message>> getMessages(String channelHashHex,
      {int? limit, double? beforeTs}) async {
    final query = <String, String>{
      if (limit != null) 'limit': '$limit',
      if (beforeTs != null) 'before_ts': '$beforeTs',
    };
    final uri = _u('/channels/$channelHashHex/messages')
        .replace(queryParameters: query.isEmpty ? null : query);
    final res = await _http.get(uri);
    return (_decode(res) as List<dynamic>)
        .map((e) => Message.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  /// [reason] is the backend's machine-readable cause when [ok] is false
  /// (`no_send_permission`, `no_recipients`).
  Future<({bool ok, String? reason})> sendMessage(
      String channelHashHex, String content,
      {String? replyTo, String? imageDataB64}) async {
    final res = await _http.post(
      _u('/channels/$channelHashHex/messages'),
      headers: _jsonHeaders,
      body: jsonEncode({
        'content': content,
        'reply_to': ?replyTo,
        'image_data_b64': ?imageDataB64,
      }),
    );
    final body = _decode(res) as Map<String, dynamic>;
    return (ok: body['ok'] as bool? ?? false, reason: body['reason'] as String?);
  }

  /// The image attached to a message, or null when it has none.
  ///
  /// Same never-throw contract as [getPeerAvatar], for the same reason: the
  /// backend stores peer bytes without requiring them to parse, so a missing
  /// or undecodable attachment is routine rather than exceptional.
  Future<Uint8List?> getMessageImage(String channelHashHex, String messageId) async {
    try {
      final res =
          await _http.get(_u('/channels/$channelHashHex/messages/$messageId/image'));
      if (res.statusCode != 200) return null;
      return res.bodyBytes;
    } catch (_) {
      return null;
    }
  }

  Future<ChannelPermissions> getMyPermissions(String channelHashHex) async {
    final res = await _http.get(_u('/channels/$channelHashHex/my_permissions'));
    return ChannelPermissions.fromJson(_decode(res) as Map<String, dynamic>);
  }

  /// The channel's overall sync state -- `synced`, `incomplete`, `syncing`,
  /// `waiting` or `unknown`. WS `sync_status` events carry the same value;
  /// this is the read for a client that just started.
  Future<String> getSyncState(String channelHashHex) async {
    final res = await _http.get(_u('/channels/$channelHashHex/sync_status'));
    final body = _decode(res) as Map<String, dynamic>;
    return body['state'] as String? ?? 'unknown';
  }

  Future<PresenceEntry> getPeerPresence(String peerHashHex) async {
    final res = await _http.get(_u('/peers/$peerHashHex/presence'));
    return PresenceEntry.fromJson(_decode(res) as Map<String, dynamic>);
  }

  Future<Uint8List?> getPeerAvatar(String peerHashHex, {int? version}) async {
    // A peer with no avatar is routine, and an unreachable backend must not
    // throw out of a widget build -- treat any decode/status failure as
    // "no avatar" rather than propagating.
    final path = version == null
        ? '/peers/$peerHashHex/avatar'
        : '/peers/$peerHashHex/avatar?v=$version';
    final Map<String, dynamic> body;
    try {
      final res = await _http.get(_u(path));
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

  /// ok=false means the join was refused: no voice permission, already in
  /// a session, or the room is full (the backend gives no reason).
  Future<bool> joinVoice(String channelHashHex) async {
    final res = await _http.post(_u('/channels/$channelHashHex/voice/join'));
    return (_decode(res) as Map<String, dynamic>)['ok'] as bool? ?? false;
  }

  Future<bool> leaveVoice() async {
    final res = await _http.post(_u('/voice/leave'));
    return (_decode(res) as Map<String, dynamic>)['ok'] as bool? ?? false;
  }

  Future<void> setVoiceMuted(bool muted) async {
    final res = await _http.post(
      _u('/voice/mute'),
      headers: _jsonHeaders,
      body: jsonEncode({'muted': muted}),
    );
    _decode(res);
  }

  Future<List<VoiceParticipant>> getVoiceRoster(String channelHashHex) async {
    final res = await _http.get(_u('/channels/$channelHashHex/voice/roster'));
    return (_decode(res) as List<dynamic>)
        .map((e) => VoiceParticipant.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  Future<VoiceStatus> getVoiceStatus() async {
    final res = await _http.get(_u('/voice/status'));
    return VoiceStatus.fromJson(_decode(res) as Map<String, dynamic>);
  }

  Future<AudioDevices> getVoiceDevices() async {
    final res = await _http.get(_u('/voice/devices'));
    return AudioDevices.fromJson(_decode(res) as Map<String, dynamic>);
  }

  /// Persists the device choice (null means the system default) and rebuilds
  /// any live audio pipeline, so a mid-call switch takes effect immediately.
  Future<AudioDevices> setVoiceDevices({
    String? inputDevice,
    String? outputDevice,
  }) async {
    final res = await _http.post(
      _u('/voice/devices'),
      headers: _jsonHeaders,
      body: jsonEncode({
        'input_device': inputDevice,
        'output_device': outputDevice,
      }),
    );
    final body = _decode(res) as Map<String, dynamic>;
    return AudioDevices.fromJson(body['devices'] as Map<String, dynamic>);
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

  // --- friend requests ---

  Future<FriendRequests> getFriendRequests() async {
    final res = await _http.get(_u('/friends/requests'));
    return FriendRequests.fromJson(_decode(res) as Map<String, dynamic>);
  }

  Future<bool> sendFriendRequest(String identityHashHex,
      {String note = '', String nickname = ''}) async {
    final res = await _http.post(
      _u('/friends/requests'),
      headers: _jsonHeaders,
      body: jsonEncode({
        'identity_hash': identityHashHex,
        'note': note,
        'nickname': nickname,
      }),
    );
    return (_decode(res) as Map<String, dynamic>)['ok'] as bool? ?? false;
  }

  Future<bool> acceptFriendRequest(String identityHashHex,
      {String nickname = ''}) async {
    final res = await _http.post(
      _u('/friends/requests/$identityHashHex/accept'),
      headers: _jsonHeaders,
      body: jsonEncode({'nickname': nickname}),
    );
    return (_decode(res) as Map<String, dynamic>)['ok'] as bool? ?? false;
  }

  Future<bool> declineFriendRequest(String identityHashHex) async {
    final res = await _http.post(_u('/friends/requests/$identityHashHex/decline'));
    return (_decode(res) as Map<String, dynamic>)['ok'] as bool? ?? false;
  }

  Future<bool> cancelFriendRequest(String identityHashHex) async {
    final res = await _http.delete(_u('/friends/requests/$identityHashHex'));
    return (_decode(res) as Map<String, dynamic>)['ok'] as bool? ?? false;
  }

  // --- direct messages ---
  //
  // A conversation's transcript is read with getMessages(conversationHash):
  // they are the same rows the channel endpoints serve.

  Future<List<DmConversation>> getDms() async {
    final res = await _http.get(_u('/dms'));
    return (_decode(res) as List<dynamic>)
        .map((e) => DmConversation.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  /// The conversation with a peer, created on first use. Throws
  /// [ApiException] (403) when they are not an accepted friend.
  Future<String> openDm(String peerHashHex) async {
    final res = await _http.post(_u('/dms/$peerHashHex'));
    return (_decode(res) as Map<String, dynamic>)['hash'] as String;
  }

  /// Returns the stored message id. Throws [ApiException] (403) when the peer
  /// is not an accepted friend.
  Future<String> sendDm(String peerHashHex, String content,
      {String? replyTo, Uint8List? imageData}) async {
    final res = await _http.post(
      _u('/dms/$peerHashHex/messages'),
      headers: _jsonHeaders,
      body: jsonEncode({
        'content': content,
        'reply_to': ?replyTo,
        if (imageData != null) 'image_data_b64': base64Encode(imageData),
      }),
    );
    return (_decode(res) as Map<String, dynamic>)['message_id'] as String;
  }

  Future<bool> markDmRead(String conversationHashHex) async {
    final res = await _http.post(_u('/dms/$conversationHashHex/read'));
    return (_decode(res) as Map<String, dynamic>)['ok'] as bool? ?? false;
  }

  Future<bool> deleteDm(String conversationHashHex) async {
    final res = await _http.delete(_u('/dms/$conversationHashHex'));
    return (_decode(res) as Map<String, dynamic>)['ok'] as bool? ?? false;
  }

  // --- propagation node (how offline direct messages get through) ---

  Future<PropagationStatus> getPropagation() async {
    final res = await _http.get(_u('/propagation'));
    return PropagationStatus.fromJson(_decode(res) as Map<String, dynamic>);
  }

  /// Pass an empty hash to go back to automatic selection.
  Future<bool> pinPropagationNode(String nodeHashHex) async {
    final res = await _http.post(
      _u('/propagation/node'),
      headers: _jsonHeaders,
      body: jsonEncode({'node_hash': nodeHashHex}),
    );
    return (_decode(res) as Map<String, dynamic>)['ok'] as bool? ?? false;
  }

  Future<bool> collectPropagated() async {
    final res = await _http.post(_u('/propagation/sync'));
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

  Future<BandwidthReport> getBandwidth() async {
    final res = await _http.get(_u('/bandwidth'));
    return BandwidthReport.fromJson(_decode(res) as Map<String, dynamic>);
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

  Future<DiscoveryReport> getDiscovery() async {
    final res = await _http.get(_u('/reticulum/discovery'));
    return DiscoveryReport.fromJson(_decode(res) as Map<String, dynamic>);
  }

  Future<void> setDiscoverySettings(bool discoverInterfaces,
      int autoconnectCount, {int? requiredDiscoveryValue}) async {
    final res = await _http.put(
      _u('/reticulum/discovery'),
      headers: _jsonHeaders,
      body: jsonEncode({
        'discover_interfaces': discoverInterfaces,
        'autoconnect_discovered_interfaces': autoconnectCount,
        'required_discovery_value': requiredDiscoveryValue,
      }),
    );
    _decode(res);
  }

  Future<String> pinDiscoveredInterface(String discoveryHash) async {
    final res = await _http.post(
      _u('/reticulum/discovery/pin'),
      headers: _jsonHeaders,
      body: jsonEncode({'discovery_hash': discoveryHash}),
    );
    final body = _decode(res) as Map<String, dynamic>;
    return body['name'] as String? ?? '';
  }

  /// Suggested bootstrap defaults not yet in the config: name -> host:port.
  Future<Map<String, String>> getSuggestedDefaults() async {
    final res = await _http.get(_u('/reticulum/interfaces_suggested'));
    final body = _decode(res) as Map<String, dynamic>;
    final missing = body['missing'] as Map<String, dynamic>? ?? {};
    return missing.map((name, cfg) {
      final m = cfg as Map<String, dynamic>;
      return MapEntry(name, '${m['target_host']}:${m['target_port']}');
    });
  }

  Future<void> applySuggestedDefaults() async {
    final res = await _http.post(_u('/reticulum/interfaces_suggested'));
    _decode(res);
  }

  /// [scope] narrows the result to `friends` or `shared` (peers sharing a
  /// channel with this node); anything else returns the whole directory.
  Future<List<DirectoryEntry>> searchDirectory(String query,
      {String scope = directoryScopeAll}) async {
    final res = await _http.get(_u('/directory?q=${Uri.encodeQueryComponent(query)}'
        '&scope=${Uri.encodeQueryComponent(scope)}'));
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

  /// The saved per-section UI theme document, `{}` when none is stored. The
  /// backend persists it verbatim; the client is what gives it meaning (see
  /// theme/theme_spec.dart).
  Future<Map<String, dynamic>> getUiTheme() async {
    final res = await _http.get(_u('/ui_theme'));
    final theme = (_decode(res) as Map<String, dynamic>)['theme'];
    return theme is Map<String, dynamic> ? theme : <String, dynamic>{};
  }

  Future<void> setUiTheme(Map<String, dynamic> theme) async {
    final res = await _http.post(
      _u('/ui_theme'),
      headers: _jsonHeaders,
      body: jsonEncode({'theme': theme}),
    );
    _decode(res);
  }

  /// Every theme saved under a name, keyed by that name. The documents have
  /// the same shape as GET /ui_theme's, and are just as uninterpreted by the
  /// backend.
  Future<Map<String, dynamic>> getThemeLibrary() async {
    final res = await _http.get(_u('/ui_theme_library'));
    final themes = (_decode(res) as Map<String, dynamic>)['themes'];
    return themes is Map<String, dynamic> ? themes : <String, dynamic>{};
  }

  /// Saves [theme] under [name], replacing any theme already saved there.
  Future<void> saveThemeToLibrary(String name, Map<String, dynamic> theme) async {
    final res = await _http.post(
      _u('/ui_theme_library'),
      headers: _jsonHeaders,
      body: jsonEncode({'name': name, 'theme': theme}),
    );
    _decode(res);
  }

  /// Deletes a saved theme. The name travels in the body, not the path: a
  /// name containing '/' cannot be addressed as a path segment even encoded.
  Future<void> deleteThemeFromLibrary(String name) async {
    final res = await _http.post(
      _u('/ui_theme_library/delete'),
      headers: _jsonHeaders,
      body: jsonEncode({'name': name}),
    );
    _decode(res);
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

  /// A channel's presence roster: one entry per subscriber/member with their
  /// identity hash, display name and online state. Populated for open-join
  /// channels too, sourced from subscribers by the backend.
  Future<List<PresenceEntry>> getChannelPresence(String channelHashHex) async {
    final res = await _http.get(_u('/channels/$channelHashHex/presence'));
    return (_decode(res) as List<dynamic>)
        .map((e) => PresenceEntry.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  /// The channel's overall mesh link quality, computed over its subscribers.
  Future<ChannelLinkQuality> getChannelLinkQuality(String channelHashHex) async {
    final res = await _http.get(_u('/channels/$channelHashHex/link_quality'));
    return ChannelLinkQuality.fromRoster(_decode(res) as List<dynamic>);
  }

  /// Leaves a server: drops the membership so the server disappears from
  /// GET /servers. ok=false means the backend had no such server.
  Future<bool> leaveServer(String serverHashHex) async {
    final res = await _http.post(_u('/servers/$serverHashHex/leave'));
    return (_decode(res) as Map<String, dynamic>)['ok'] as bool? ?? false;
  }

  Future<ChannelPermissions> getMyServerPermissions(String serverHashHex) async {
    final res = await _http.get(_u('/servers/$serverHashHex/my_permissions'));
    return ChannelPermissions.fromJson(_decode(res) as Map<String, dynamic>);
  }

  Future<void> inviteToServer(String serverHashHex, String peerHashHex) async {
    final res = await _http.post(
      _u('/servers/$serverHashHex/invite'),
      headers: _jsonHeaders,
      body: jsonEncode({'peer_hash_hex': peerHashHex}),
    );
    _decode(res);
  }

  Future<ScopePermissions> getServerPermissions(String serverHashHex) async {
    final res = await _http.get(_u('/servers/$serverHashHex/permissions'));
    return ScopePermissions.fromJson(_decode(res) as Map<String, dynamic>);
  }

  /// Returns false when the backend's MANAGE_CHANNEL gate dropped the change.
  Future<bool> updateServerPermissions(
      String serverHashHex, List<String> admin, List<String> member) async {
    final res = await _http.post(
      _u('/servers/$serverHashHex/permissions'),
      headers: _jsonHeaders,
      body: jsonEncode({'admin': admin, 'member': member}),
    );
    return (_decode(res) as Map<String, dynamic>)['ok'] as bool? ?? false;
  }

  // --- nomad network page browsing ---

  Future<List<NomadNode>> getNomadNodes() async {
    final res = await _http.get(_u('/nomad/nodes'));
    return (_decode(res) as List<dynamic>)
        .map((e) => NomadNode.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  Future<({bool ok, String? fetchId, String? nodeHash, String? path,
      bool cached})> browseNomad(String url,
          {String? currentNode, Map<String, String>? data,
          bool refresh = false}) async {
    final res = await _http.post(
      _u('/nomad/browse'),
      headers: _jsonHeaders,
      body: jsonEncode({
        'url': url,
        'current_node': currentNode,
        if (data != null && data.isNotEmpty) 'data': data,
        if (refresh) 'refresh': true,
      }),
    );
    final body = _decode(res) as Map<String, dynamic>;
    return (
      ok: body['ok'] as bool? ?? false,
      fetchId: body['fetch_id'] as String?,
      nodeHash: body['node_hash'] as String?,
      path: body['path'] as String?,
      cached: body['cached'] as bool? ?? false,
    );
  }

  /// How a fetch is doing, straight from the backend. Null once the backend
  /// has forgotten it. The event socket can drop; this cannot.
  Future<NomadFetchStatus?> getNomadFetch(String fetchId) async {
    final res = await _http.get(_u('/nomad/fetch/$fetchId'));
    if (res.statusCode == 404) return null;
    final body = _decode(res) as Map<String, dynamic>;
    return NomadFetchStatus(
      nodeHash: body['node_hash'] as String? ?? '',
      path: body['path'] as String? ?? '',
      status: body['status'] as String? ?? '',
      progress: (body['progress'] as num?)?.toDouble() ?? 0,
      reason: body['reason'] as String?,
    );
  }

  /// The cached copy of a fetched page, or null when nothing is cached yet.
  Future<NomadPage?> getNomadPage(String nodeHash, String path) async {
    final res = await _http
        .get(_u('/nomad/page/$nodeHash?path=${Uri.encodeQueryComponent(path)}'));
    if (res.statusCode == 404) return null;
    return NomadPage.fromJson(_decode(res) as Map<String, dynamic>);
  }

  Future<List<NomadBookmark>> getNomadBookmarks() async {
    final res = await _http.get(_u('/nomad/bookmarks'));
    return (_decode(res) as List<dynamic>)
        .map((e) => NomadBookmark.fromJson(e as Map<String, dynamic>))
        .toList();
  }

  Future<bool> addNomadBookmark(
      String nodeHash, String path, String label) async {
    final res = await _http.post(
      _u('/nomad/bookmarks'),
      headers: _jsonHeaders,
      body: jsonEncode({'node_hash': nodeHash, 'path': path, 'label': label}),
    );
    return (_decode(res) as Map<String, dynamic>)['ok'] as bool? ?? false;
  }

  Future<bool> removeNomadBookmark(String nodeHash, String path) async {
    final res = await _http.post(
      _u('/nomad/bookmarks/delete'),
      headers: _jsonHeaders,
      body: jsonEncode({'node_hash': nodeHash, 'path': path}),
    );
    return (_decode(res) as Map<String, dynamic>)['ok'] as bool? ?? false;
  }

  Future<NomadHosting> getNomadHosting() async {
    final res = await _http.get(_u('/nomad/hosting'));
    return NomadHosting.fromJson(_decode(res) as Map<String, dynamic>);
  }

  Future<NomadHosting> setNomadHosting({bool? enabled, String? nodeName}) async {
    final res = await _http.post(
      _u('/nomad/hosting'),
      headers: _jsonHeaders,
      body: jsonEncode({'enabled': enabled, 'node_name': nodeName}),
    );
    return NomadHosting.fromJson(_decode(res) as Map<String, dynamic>);
  }

  Future<NomadHosting> refreshNomadHosting() async {
    final res = await _http.post(_u('/nomad/hosting/refresh'));
    return NomadHosting.fromJson(_decode(res) as Map<String, dynamic>);
  }

  /// A browser-openable URL for a cached /file/ download. Carries the token
  /// as a query parameter because the browser can't set headers on a plain
  /// navigation -- the same reason the WS handshake uses ?token=.
  Uri nomadFileUri(String nodeHash, String path) {
    final query = _token.isEmpty
        ? 'path=${Uri.encodeQueryComponent(path)}'
        : 'path=${Uri.encodeQueryComponent(path)}'
            '&token=${Uri.encodeQueryComponent(_token)}';
    return Uri.parse('$baseUrl/nomad/file/$nodeHash?$query');
  }

  void close() => _http.close();
}
