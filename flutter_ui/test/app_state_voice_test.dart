import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/models/voice.dart';
import 'package:flutter_ui/app_state.dart';

import 'fake_backend.dart';

const _channelHash = 'channel-voice';

Map<String, Object> _statusInCall({bool muted = false}) => {
      'channel': _channelHash,
      'muted': muted,
      'stats': {
        'tx_packets': 3,
        'rx_frames': {'peer-a': 10},
        'rx_quality': {
          'peer-a': {'received': 10, 'lost': 0, 'late': 0, 'jitter_ms': 1.0, 'loss_pct': 0.0},
        },
      },
      'audio': {'available': true, 'reason': ''},
    };

void main() {
  late FakeBackend backend;
  late AppState state;

  setUp(() {
    backend = FakeBackend();
    state = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
    state.selectedChannelHash = _channelHash;
  });

  tearDown(() {
    // Also cancels the voice status poll timer.
    state.dispose();
  });

  test('a successful join picks up the session status and roster', () async {
    backend.routes['POST /channels/$_channelHash/voice/join'] = {'ok': true};
    backend.routes['GET /voice/status'] = _statusInCall();
    backend.routes['GET /channels/$_channelHash/voice/roster'] = [
      {
        'identity_hash': 'peer-a',
        'display_name': 'Alice',
        'muted': false,
        'joined_at': 100.0,
        'link_state': 'streaming',
        'speaking': false,
      },
    ];

    expect(await state.joinVoice(_channelHash), isTrue);

    expect(state.voiceChannelHash, _channelHash);
    expect(state.voiceRosterByChannel[_channelHash], hasLength(1));
    expect(state.voiceRosterByChannel[_channelHash]!.single.displayName, 'Alice');
    expect(state.actionError, isNull);
    expect(
      backend.requests.any(
          (r) => r.method == 'POST' && r.path == '/channels/$_channelHash/voice/join'),
      isTrue,
    );
  });

  test('a refused join reports the combined failure reason', () async {
    backend.routes['POST /channels/$_channelHash/voice/join'] = {'ok': false};

    expect(await state.joinVoice(_channelHash), isFalse);
    expect(state.voiceChannelHash, isNull);
    expect(state.actionError, contains("Couldn't join voice"));
  });

  test('toggleVoiceMute flips optimistically and posts the new state', () async {
    backend.routes['POST /voice/mute'] = {'ok': true};

    expect(state.voiceMuted, isFalse);
    expect(await state.toggleVoiceMute(), isTrue);
    expect(state.voiceMuted, isTrue);

    final muteRequest =
        backend.requests.singleWhere((r) => r.method == 'POST' && r.path == '/voice/mute');
    expect(jsonDecode(muteRequest.body), {'muted': true});
  });

  test('leaveVoice resets the session to idle', () async {
    backend.routes['POST /channels/$_channelHash/voice/join'] = {'ok': true};
    backend.routes['GET /voice/status'] = _statusInCall();
    backend.routes['GET /channels/$_channelHash/voice/roster'] = <Object>[];
    await state.joinVoice(_channelHash);
    expect(state.voiceChannelHash, _channelHash);

    backend.routes['POST /voice/leave'] = {'ok': true};
    expect(await state.leaveVoice(), isTrue);
    await Future<void>.delayed(Duration.zero);

    expect(state.voiceStatus.channel, isNull);
    expect(state.voiceChannelHash, isNull);
    expect(state.voiceAudioError, isFalse);
  });

  test('refreshVoiceStatus reconciles the optimistic mute state', () async {
    backend.routes['GET /voice/status'] = _statusInCall(muted: true);

    await state.refreshVoiceStatus();

    expect(state.voiceMuted, isTrue);
    expect(state.voiceQualityLevel.name, 'excellent');
  });

  test('loadChannel also loads the voice roster', () async {
    backend.routes['GET /channels/$_channelHash/members'] = <Object>[];
    backend.routes['GET /channels/$_channelHash/messages'] = <Object>[];
    // Presence and link quality are client-side compositions over members
    // and the network map (see client.dart's phase-b seams).
    backend.routes['GET /network/map'] = {'nodes': <Object>[]};
    backend.routes['GET /channels/$_channelHash/my_permissions'] = {
      'kick': false,
      'manage_roles': false,
      'manage_channel': false,
      'voice_chat': true,
    };
    backend.routes['GET /channels/$_channelHash/voice/roster'] = [
      {
        'identity_hash': 'peer-a',
        'link_state': 'signalled',
      },
    ];

    await state.loadChannel(_channelHash);

    expect(state.voiceRosterByChannel[_channelHash], hasLength(1));
    expect(state.voiceRosterByChannel[_channelHash]!.single.linkState,
        VoiceLinkState.signalled);
    expect(state.permissionsByChannel[_channelHash]!.voiceChat, isTrue);
  });
}
