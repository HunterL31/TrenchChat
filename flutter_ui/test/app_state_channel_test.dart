// Leaving a channel from its row menu: the subscription goes, the list is
// re-read, and the open channel never ends up pointing at one that is gone.
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/models/server.dart';
import 'package:flutter_ui/app_state.dart';

import 'fake_backend.dart';

Map<String, Object?> _channelJson(String name) => {
      'hash': 'hash-$name',
      'name': name,
      'description': '',
      'creator_hash': 'creator',
      'open_join': true,
      'created_at': 0,
      'server_hash': null,
    };

/// Everything AppState.loadChannel reads, so the reload after a leave adds no
/// failures of its own.
void _seedChannelReads(FakeBackend backend, String hash) {
  backend.routes['GET /channels/$hash/members'] = <Object>[];
  backend.routes['GET /channels/$hash/messages'] = <Object>[];
  backend.routes['GET /network/map'] = {'nodes': <Object>[]};
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
}
