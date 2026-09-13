import 'dart:convert';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:stream_channel/stream_channel.dart';

import 'package:flutter_ui/api/events.dart';
import 'package:flutter_ui/api/models/permissions.dart';
import 'package:flutter_ui/api/models/screen.dart';
import 'package:flutter_ui/api/screen_watch.dart';
import 'package:flutter_ui/api/screen_wire.dart';
import 'package:flutter_ui/app_state.dart';

import 'api/screen_wire_test.dart';
import 'fake_backend.dart';

const _channelHash = 'channel-voice';
const _alice = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';

Map<String, Object> _voiceStatus() => {
      'channel': _channelHash,
      'muted': false,
      'stats': {'tx_packets': 0, 'rx_quality': {}},
      'audio': {'available': true, 'reason': ''},
    };

Map<String, Object?> _screenStatus({
  bool available = true,
  String reason = '',
  Map<String, Object?>? sharing,
  List<Map<String, Object?>> shares = const [],
}) =>
    {
      'available': {'ok': available, 'reason': reason},
      'sharing': sharing,
      'watching': null,
      'shares': shares,
    };

Map<String, Object?> _aliceShare() => {
      'peer': _alice,
      'channel': _channelHash,
      'display_name': 'Alice',
      'width': 320,
      'height': 200,
      'fps': 15,
      'since': 1.0,
    };

void main() {
  late FakeBackend backend;
  late AppState state;
  late StreamChannelController<dynamic> socket;
  final sentByClient = <dynamic>[];

  setUp(() {
    backend = FakeBackend();
    backend.routes['GET /voice/status'] = _voiceStatus();
    backend.routes['GET /channels/$_channelHash/my_permissions'] = {
      'send_message': true, 'voice_chat': true, 'screen_share': true,
    };
    backend.routes['GET /screen/status'] = _screenStatus();
    socket = StreamChannelController<dynamic>();
    sentByClient.clear();
    socket.foreign.stream.listen(sentByClient.add);
    state = AppState(
      baseUrl: backend.baseUrl,
      httpClient: backend.client(),
      screenWatchFactory: (peer) =>
          ScreenWatch(baseUrl: backend.baseUrl, peer: peer, connect: (_) => socket.local),
    );
    state.selectedChannelHash = _channelHash;
  });

  tearDown(() => state.dispose());

  test('the status is parsed into what the panel and roster read', () async {
    backend.routes['GET /screen/status'] = _screenStatus(shares: [_aliceShare()]);
    await state.refreshScreenStatus();
    expect(state.screenStatus.available, isTrue);
    expect(state.heldSharesByPeer.keys, [_alice]);
    expect(state.heldSharesByPeer[_alice]!.displayName, 'Alice');
  });

  test('the share button is gated on voice, permission and capture', () async {
    expect(state.screenShareDisabledReason, 'not_in_voice');
    await state.refreshVoiceStatus();
    await state.refreshScreenStatus();
    // In voice with the permission unknown: the gate fails closed.
    expect(state.screenShareDisabledReason, 'no_screen_permission');
    state.permissionsByChannel[_channelHash] = const ChannelPermissions(
        invite: false, kick: false, manageRoles: false, manageChannel: false,
        sendMessage: true, voiceChat: true, screenShare: true);
    expect(state.canShareScreen, isTrue);

    backend.routes['GET /screen/status'] =
        _screenStatus(available: false, reason: 'screen capture is unavailable on Wayland');
    await state.refreshScreenStatus();
    expect(state.screenShareDisabledReason, 'capture_unavailable');

    backend.routes['GET /screen/status'] = _screenStatus(sharing: {
      'channel': _channelHash, 'source': 'Monitor 1', 'preset': 'clearer',
      'fps': 15, 'width': 1920, 'height': 1080, 'since': 1.0,
      'viewers': [{'peer': _alice, 'display_name': 'Alice', 'since': 1.0,
                   'updates': 3, 'bytes': 100}],
    });
    await state.refreshScreenStatus();
    expect(state.screenShareDisabledReason, 'already_sharing');
    expect(state.screenViewerCount, 1);
  });

  test('a refused start reports the reason in words', () async {
    backend.routes['POST /screen/start'] = {'ok': false, 'reason': 'no_direct'};
    expect(await state.startScreenShare(monitor: 1, preset: ScreenPreset.clearer),
        isFalse);
    expect(state.actionError, screenReasonText('no_direct'));
    final request = backend.requests.firstWhere((r) => r.path == '/screen/start');
    expect(jsonDecode(request.body), {'monitor': 1, 'preset': 'clearer', 'fps': null});
  });

  test('watching opens the socket, paints what arrives and returns the credit',
      () async {
    backend.routes['GET /screen/status'] = _screenStatus(shares: [_aliceShare()]);
    await state.refreshScreenStatus();
    state.watchScreen(_alice);
    expect(state.watchingPeer, _alice);
    final buffer = state.screenBuffer!;
    expect(buffer.isEmpty, isTrue);

    final png = Uint8List.fromList(base64Decode(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=='));
    socket.foreign.sink.add(packUpdate(
        width: 1, height: 1, kind: kindFull, entries: [(0, 0, png)]));
    await Future<void>.delayed(const Duration(milliseconds: 200));
    expect(buffer.full, isNotNull);
    expect(sentByClient, ['r']);

    socket.foreign.sink.add(jsonEncode({'ended': 'session_lost'}));
    await Future<void>.delayed(const Duration(milliseconds: 50));
    expect(state.screenWatchEnded, screenReasonText('session_lost'));
    expect(state.watchingPeer, _alice, reason: 'the last picture stays up');

    state.stopWatchingScreen();
    expect(state.watchingPeer, isNull);
  });

  test('a screen_watch event with no peer ends the stage in words', () async {
    backend.routes['GET /screen/status'] = _screenStatus(shares: [_aliceShare()]);
    await state.refreshScreenStatus();
    state.watchScreen(_alice);
    state.applyEvent(const ScreenWatchEvent(null, 'stopped'));
    expect(state.screenWatchEnded, screenReasonText('stopped'));
  });

  test('leaving voice closes the stage', () async {
    backend.routes['GET /screen/status'] = _screenStatus(shares: [_aliceShare()]);
    backend.routes['POST /voice/leave'] = {'ok': true};
    await state.refreshScreenStatus();
    state.watchScreen(_alice);
    await state.leaveVoice();
    expect(state.watchingPeer, isNull);
  });
}
