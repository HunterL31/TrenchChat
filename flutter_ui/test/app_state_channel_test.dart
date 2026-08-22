// Leaving a channel from its row menu: the subscription goes, the list is
// re-read, and the open channel never ends up pointing at one that is gone.
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/models/permissions.dart';
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

Map<String, Object> _voiceStatusInCall(String channelHash) => {
      'channel': channelHash,
      'muted': false,
      'stats': {
        'tx_packets': 0,
        'rx_frames': <String, Object>{},
        'rx_quality': <String, Object>{},
      },
      'audio': {'available': true, 'reason': ''},
    };

/// Everything AppState.loadChannel reads, so the reload after a leave adds no
/// failures of its own.
void _seedChannelReads(FakeBackend backend, String hash) {
  backend.routes['GET /channels/$hash/members'] = <Object>[];
  backend.routes['GET /channels/$hash/messages'] = <Object>[];
  backend.routes['GET /channels/$hash/presence'] = <Object>[];
  backend.routes['GET /channels/$hash/link_quality'] = <Object>[];
  backend.routes['GET /channels/$hash/my_permissions'] = {'invite': false};
  backend.routes['GET /channels/$hash/voice/roster'] = <Object>[];
  backend.routes['GET /channels/$hash/sync_status'] = {'state': 'synced'};
}

void main() {
  late FakeBackend backend;
  late AppState state;

  setUp(() {
    backend = FakeBackend();
    state = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
    state.standaloneChannels = [
      Channel.fromJson(_channelJson('alpha')),
      Channel.fromJson(_channelJson('beta')),
    ];
    state.selectedChannelHash = 'hash-alpha';
  });

  tearDown(() => state.dispose());

  test('leaving the open channel drops it and opens what is left', () async {
    backend.routes['POST /channels/hash-alpha/leave'] = {'ok': true};
    backend.routes['GET /channels'] = [_channelJson('beta')];
    _seedChannelReads(backend, 'hash-beta');

    expect(await state.leaveChannel('hash-alpha'), isTrue);

    expect(state.standaloneChannels.map((c) => c.hash), ['hash-beta']);
    expect(state.selectedChannelHash, 'hash-beta');
    expect(state.actionError, isNull);
  });

  test('leaving the last channel leaves nothing selected', () async {
    backend.routes['POST /channels/hash-alpha/leave'] = {'ok': true};
    backend.routes['GET /channels'] = <Object>[];
    state.standaloneChannels = [Channel.fromJson(_channelJson('alpha'))];

    expect(await state.leaveChannel('hash-alpha'), isTrue);

    expect(state.standaloneChannels, isEmpty);
    expect(state.selectedChannelHash, isNull);
  });

  test('leaving a channel that is not open keeps the open one', () async {
    backend.routes['POST /channels/hash-beta/leave'] = {'ok': true};
    backend.routes['GET /channels'] = [_channelJson('alpha')];

    expect(await state.leaveChannel('hash-beta'), isTrue);

    expect(state.selectedChannelHash, 'hash-alpha');
  });

  test('a refused leave changes nothing', () async {
    backend.routes['POST /channels/hash-alpha/leave'] = {'ok': false};

    expect(await state.leaveChannel('hash-alpha'), isFalse);

    expect(state.standaloneChannels, hasLength(2));
    expect(state.selectedChannelHash, 'hash-alpha');
  });

  test('leaving a channel you are in voice on also ends the call', () async {
    backend.routes['POST /channels/hash-alpha/voice/join'] = {'ok': true};
    backend.routes['GET /voice/status'] = _voiceStatusInCall('hash-alpha');
    backend.routes['GET /channels/hash-alpha/voice/roster'] = <Object>[];
    await state.joinVoice('hash-alpha');
    expect(state.voiceChannelHash, 'hash-alpha');

    backend.routes['POST /channels/hash-alpha/leave'] = {'ok': true};
    backend.routes['POST /voice/leave'] = {'ok': true};
    backend.routes['GET /channels'] = [_channelJson('beta')];
    _seedChannelReads(backend, 'hash-beta');

    expect(await state.leaveChannel('hash-alpha'), isTrue);
    await Future<void>.delayed(Duration.zero);

    expect(state.voiceChannelHash, isNull);
    expect(state.selectedChannelHash, 'hash-beta');
    expect(
      backend.requests.any((r) => r.method == 'POST' && r.path == '/voice/leave'),
      isTrue,
    );
  });

  test('leaving a voice channel that is not the open one still ends the call', () async {
    backend.routes['POST /channels/hash-beta/voice/join'] = {'ok': true};
    backend.routes['GET /voice/status'] = _voiceStatusInCall('hash-beta');
    backend.routes['GET /channels/hash-beta/voice/roster'] = <Object>[];
    await state.joinVoice('hash-beta');
    expect(state.voiceChannelHash, 'hash-beta');

    backend.routes['POST /channels/hash-beta/leave'] = {'ok': true};
    backend.routes['POST /voice/leave'] = {'ok': true};
    backend.routes['GET /channels'] = [_channelJson('alpha')];

    expect(await state.leaveChannel('hash-beta'), isTrue);
    await Future<void>.delayed(Duration.zero);

    expect(state.voiceChannelHash, isNull);
    expect(state.selectedChannelHash, 'hash-alpha');
  });

  test('leaving a direct channel with a server selected keeps a valid selection',
      () async {
    state.selectedServerHash = 'server-1';
    state.channelsByServer['server-1'] = const [];
    backend.routes['POST /channels/hash-alpha/leave'] = {'ok': true};
    backend.routes['GET /channels'] = [_channelJson('beta')];
    _seedChannelReads(backend, 'hash-beta');

    expect(await state.leaveChannel('hash-alpha'), isTrue);

    expect(state.selectedChannelHash, 'hash-beta');
    expect(state.channelByHash(state.selectedChannelHash!), isNotNull);
  });

  test('leaving the last direct channel with an empty server selects nothing',
      () async {
    state.selectedServerHash = 'server-1';
    state.channelsByServer['server-1'] = const [];
    state.standaloneChannels = [Channel.fromJson(_channelJson('alpha'))];
    backend.routes['POST /channels/hash-alpha/leave'] = {'ok': true};
    backend.routes['GET /channels'] = <Object>[];

    expect(await state.leaveChannel('hash-alpha'), isTrue);

    expect(state.selectedChannelHash, isNull);
  });

  test('leaving a direct channel with an empty server falls back to a server channel',
      () async {
    state.selectedServerHash = 'server-1';
    state.channelsByServer['server-1'] = [
      Channel.fromJson(_channelJson('team', serverHash: 'server-1')),
    ];
    state.standaloneChannels = [Channel.fromJson(_channelJson('alpha'))];
    backend.routes['POST /channels/hash-alpha/leave'] = {'ok': true};
    backend.routes['GET /channels'] = <Object>[];
    _seedChannelReads(backend, 'hash-team');

    expect(await state.leaveChannel('hash-alpha'), isTrue);

    expect(state.selectedChannelHash, 'hash-team');
  });

  test('joinableDiscoveredChannels omits invite-only and already-joined channels', () {
    state.standaloneChannels = [Channel.fromJson(_channelJson('alpha'))];
    state.discoveredChannels = [
      Channel.fromJson(_channelJson('alpha')),
      Channel.fromJson(_channelJson('secret', openJoin: false)),
      Channel.fromJson(_channelJson('public2')),
    ];

    expect(
      state.joinableDiscoveredChannels.map((c) => c.name),
      ['public2'],
    );
  });

  test('ensureChannelMeta loads permissions and sync state for an unvisited channel',
      () async {
    backend.routes['GET /channels/hash-beta/my_permissions'] = {
      'invite': true,
      'manage_channel': true,
    };
    backend.routes['GET /channels/hash-beta/sync_status'] = {'state': 'incomplete'};

    expect(state.permissionsByChannel.containsKey('hash-beta'), isFalse);
    await state.ensureChannelMeta(['hash-beta']);

    expect(state.permissionsByChannel['hash-beta']!.invite, isTrue);
    expect(state.permissionsByChannel['hash-beta']!.manageChannel, isTrue);
    expect(state.syncStateByChannel['hash-beta'], 'incomplete');
  });

  test('ensureChannelMeta skips a channel already loaded', () async {
    state.permissionsByChannel['hash-alpha'] = const ChannelPermissions(
      invite: true,
      kick: false,
      manageRoles: false,
      manageChannel: false,
      sendMessage: true,
      voiceChat: false,
    );

    await state.ensureChannelMeta(['hash-alpha']);

    expect(
      backend.requests.any((r) => r.path.contains('hash-alpha')),
      isFalse,
    );
  });
}
