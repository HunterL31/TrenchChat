// Server lifecycle in state (#47): leaving a server drops it from the rail
// and, when it was selected, falls back to the DIRECT CHANNELS view with no
// server selected.
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/models/server.dart';
import 'package:flutter_ui/app_state.dart';

import 'fake_backend.dart';

Server _server(String hash, String name) => Server(
      hash: hash,
      name: name,
      description: '',
      creatorHash: 'creator',
      createdAt: 0,
    );

Channel _channel(String hash, String name) => Channel(
      hash: hash,
      name: name,
      description: '',
      creatorHash: 'creator',
      openJoin: true,
      createdAt: 0,
      serverHash: null,
    );

void main() {
  late FakeBackend backend;
  late AppState state;

  setUp(() {
    backend = FakeBackend();
    state = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
    state.servers = [_server('srv-a', 'mesh-crew'), _server('srv-b', 'RF Ops')];
    state.channelsByServer['srv-a'] = [];
    state.channelsByServer['srv-b'] = [];
    state.standaloneChannels = [_channel('dm-1', 'lora-testing')];
    // Pre-seed so the home fallback's loadChannel is a no-op needing no routes.
    state.messagesByChannel['dm-1'] = [];
    state.selectedServerHash = 'srv-a';
    state.selectedChannelHash = null;
  });

  tearDown(() => state.dispose());

  test('leaving the selected server removes it and returns home', () async {
    backend.routes['POST /servers/srv-a/leave'] = {'ok': true};

    expect(await state.leaveServer('srv-a'), isTrue);

    expect(state.servers.map((s) => s.hash), ['srv-b']);
    expect(state.channelsByServer.containsKey('srv-a'), isFalse);
    // Deselected the server, landed on the first direct channel.
    expect(state.selectedServerHash, isNull);
    expect(state.selectedChannelHash, 'dm-1');
  });

  test('a refused leave changes nothing', () async {
    backend.routes['POST /servers/srv-a/leave'] = {'ok': false};

    expect(await state.leaveServer('srv-a'), isFalse);
    expect(state.servers, hasLength(2));
    expect(state.selectedServerHash, 'srv-a');
  });

  test('leaving a non-selected server keeps the current selection', () async {
    backend.routes['POST /servers/srv-b/leave'] = {'ok': true};

    expect(await state.leaveServer('srv-b'), isTrue);
    expect(state.servers.map((s) => s.hash), ['srv-a']);
    expect(state.selectedServerHash, 'srv-a');
  });

  test('selectHome deselects the server and opens a direct channel', () async {
    await state.selectHome();
    expect(state.selectedServerHash, isNull);
    expect(state.selectedChannelHash, 'dm-1');
  });
}
