// A dropped-and-restored event socket must not leave the shell stale: the
// reconnect resync refetches the server, channel and friend lists (not only
// the open channel), and a channel_joined event adds the joined channel
// instead of doing nothing.
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/events.dart';
import 'package:flutter_ui/api/models/server.dart';
import 'package:flutter_ui/app_state.dart';

import 'fake_backend.dart';

Map<String, Object?> _channelJson(String name, {bool openJoin = true, String? serverHash}) => {
      'hash': 'hash-$name',
      'name': name,
      'description': '',
      'creator_hash': 'creator',
      'open_join': openJoin,
      'created_at': 0,
      'server_hash': serverHash,
    };

Map<String, Object?> _friendJson(String hash, String nickname) => {
      'identity_hash': hash,
      'nickname': nickname,
      'note': '',
      'display_name': nickname,
      'added_at': 0,
      'last_seen_at': 0,
      'is_online': false,
    };

/// Everything AppState.loadChannel reads for [hash], so a resync's channel
/// reload adds no failures of its own.
void _seedChannelReads(FakeBackend backend, String hash) {
  backend.routes['GET /channels/$hash/members'] = <Object>[];
  backend.routes['GET /channels/$hash/messages'] = <Object>[];
  backend.routes['GET /channels/$hash/presence'] = <Object>[];
  backend.routes['GET /channels/$hash/link_quality'] = <Object>[];
  backend.routes['GET /channels/$hash/my_permissions'] = {'invite': false};
  backend.routes['GET /channels/$hash/voice/roster'] = <Object>[];
  backend.routes['GET /channels/$hash/sync_status'] = {'state': 'synced'};
}

/// The top-level lists reconnect and channel_joined refetch.
void _seedTopLevelLists(FakeBackend backend,
    {required List<Object> channels, List<Object> servers = const []}) {
  backend.routes['GET /servers'] = servers;
  backend.routes['GET /channels'] = channels;
  backend.routes['GET /friends'] = <Object>[];
  backend.routes['GET /invites'] = <Object>[];
  backend.routes['GET /emoji'] = <Object>[];
  backend.routes['GET /voice/status'] = {
    'channel': null,
    'muted': false,
    'stats': {
      'tx_packets': 0,
      'rx_frames': <String, Object>{},
      'rx_quality': <String, Object>{},
    },
    'audio': {'available': true, 'reason': ''},
  };
}

void main() {
  late FakeBackend backend;
  late AppState state;

  setUp(() {
    backend = FakeBackend();
    state = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
  });

  tearDown(() => state.dispose());

  test('reconnect refetches the server, channel and friend lists', () async {
    // Started with only alpha and no friends; while the socket was down beta
    // was joined and a friend was added.
    state.standaloneChannels = [Channel.fromJson(_channelJson('alpha'))];
    state.selectedChannelHash = 'hash-alpha';
    _seedChannelReads(backend, 'hash-alpha');
    _seedTopLevelLists(backend, channels: [_channelJson('alpha'), _channelJson('beta')]);
    backend.routes['GET /friends'] = [_friendJson('peer-x', 'Xavier')];

    state.simulateReconnect();
    await Future<void>.delayed(const Duration(milliseconds: 50));

    expect(state.standaloneChannels.map((c) => c.hash), ['hash-alpha', 'hash-beta']);
    expect(state.friends.map((f) => f.identityHash), ['peer-x']);
  });

  test('reconnect refetches the server list and each server\'s channels', () async {
    _seedTopLevelLists(
      backend,
      channels: const [],
      servers: [
        {'hash': 'srv-1', 'name': 'Server One', 'description': '', 'creator_hash': 'c', 'created_at': 0},
      ],
    );
    backend.routes['GET /servers/srv-1/channels'] = [_channelJson('general', serverHash: 'srv-1')];
    backend.routes['GET /servers/srv-1/members'] = <Object>[];
    backend.routes['GET /servers/srv-1/my_permissions'] = {'invite': false};

    state.simulateReconnect();
    await Future<void>.delayed(const Duration(milliseconds: 50));

    expect(state.servers.map((s) => s.hash), ['srv-1']);
    expect(state.channelsByServer['srv-1']!.map((c) => c.hash), ['hash-general']);
  });

  test('a channel_joined event adds the joined channel to the list', () async {
    state.standaloneChannels = [Channel.fromJson(_channelJson('alpha'))];
    _seedTopLevelLists(backend, channels: [_channelJson('alpha'), _channelJson('beta')]);

    expect(state.channelByHash('hash-beta'), isNull);

    state.applyEvent(const ChannelJoinedEvent('hash-beta', 'beta'));
    await Future<void>.delayed(const Duration(milliseconds: 50));

    expect(state.channelByHash('hash-beta'), isNotNull);
    expect(state.standaloneChannels.map((c) => c.hash), ['hash-alpha', 'hash-beta']);
  });
}
