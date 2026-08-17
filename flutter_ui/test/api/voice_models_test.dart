import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/models/link_quality.dart';
import 'package:flutter_ui/api/models/voice.dart';

VoicePeerQuality _quality({double lossPct = 0, double jitterMs = 0}) =>
    VoicePeerQuality(received: 100, lost: 0, late: 0, jitterMs: jitterMs, lossPct: lossPct);

void main() {
  test('VoiceParticipant parses every link_state and defaults unknowns', () {
    const states = {
      'self': VoiceLinkState.self,
      'streaming': VoiceLinkState.streaming,
      'connecting': VoiceLinkState.connecting,
      'unreachable': VoiceLinkState.unreachable,
      'signalled': VoiceLinkState.signalled,
      'something-new': VoiceLinkState.unknown,
    };
    states.forEach((raw, expected) {
      final p = VoiceParticipant.fromJson({
        'identity_hash': 'aa',
        'link_state': raw,
      });
      expect(p.linkState, expected, reason: raw);
      expect(p.muted, isFalse);
      expect(p.speaking, isFalse);
      expect(p.displayName, isEmpty);
    });
  });

  test('copyWith flips speaking without touching the rest', () {
    final p = VoiceParticipant.fromJson({
      'identity_hash': 'aa',
      'display_name': 'Alice',
      'muted': true,
      'joined_at': 12.5,
      'link_state': 'streaming',
      'speaking': false,
    });
    final speaking = p.copyWith(speaking: true);
    expect(speaking.speaking, isTrue);
    expect(speaking.muted, isTrue);
    expect(speaking.displayName, 'Alice');
    expect(speaking.joinedAt, 12.5);
    expect(speaking.linkState, VoiceLinkState.streaming);
  });

  test('VoiceStatus tolerates sparse and absent stats/audio', () {
    final empty = VoiceStatus.fromJson({'channel': null, 'muted': false});
    expect(empty.channel, isNull);
    expect(empty.rxQuality, isEmpty);
    expect(empty.audioAvailable, isTrue);

    final full = VoiceStatus.fromJson({
      'channel': 'ch1',
      'muted': true,
      'stats': {
        'tx_packets': 7,
        'rx_quality': {
          'peer-a': {'received': 50, 'lost': 1, 'late': 0, 'jitter_ms': 3.5, 'loss_pct': 2.0},
        },
      },
      'audio': {'available': false, 'reason': 'libopus missing'},
    });
    expect(full.channel, 'ch1');
    expect(full.muted, isTrue);
    expect(full.txPackets, 7);
    expect(full.rxQuality['peer-a']!.jitterMs, 3.5);
    expect(full.audioAvailable, isFalse);
    expect(full.audioReason, 'libopus missing');
  });

  test('voicePeerLevel follows the Discord-comparable thresholds', () {
    expect(voicePeerLevel(_quality(lossPct: 2, jitterMs: 30)), LinkQualityLevel.excellent);
    expect(voicePeerLevel(_quality(lossPct: 2.1, jitterMs: 10)), LinkQualityLevel.good);
    expect(voicePeerLevel(_quality(lossPct: 1, jitterMs: 31)), LinkQualityLevel.good);
    expect(voicePeerLevel(_quality(lossPct: 5, jitterMs: 60)), LinkQualityLevel.good);
    expect(voicePeerLevel(_quality(lossPct: 12, jitterMs: 120)), LinkQualityLevel.fair);
    expect(voicePeerLevel(_quality(lossPct: 13, jitterMs: 0)), LinkQualityLevel.poor);
    expect(voicePeerLevel(_quality(lossPct: 0, jitterMs: 121)), LinkQualityLevel.poor);
  });

  test('voiceOverallLevel: worst peer wins, empty reads unknown', () {
    expect(voiceOverallLevel(VoiceStatus.idle), LinkQualityLevel.unknown);

    final mixed = VoiceStatus.fromJson({
      'channel': 'ch1',
      'muted': false,
      'stats': {
        'rx_quality': {
          'good-peer': {'loss_pct': 0.0, 'jitter_ms': 1.0},
          'bad-peer': {'loss_pct': 40.0, 'jitter_ms': 300.0},
        },
      },
    });
    expect(voiceOverallLevel(mixed), LinkQualityLevel.poor);
  });
}
