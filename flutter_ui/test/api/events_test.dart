import 'dart:convert';

import 'package:flutter/painting.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/events.dart';

void main() {
  test('invite_received parses into InviteReceivedEvent', () {
    final event = TcEvent.tryParse(jsonEncode({
      'type': 'invite_received',
      'channel_hash': 'channel-ops',
      'channel_name': 'ops',
    }));

    expect(
      event,
      isA<InviteReceivedEvent>()
          .having((e) => e.channelHash, 'channelHash', 'channel-ops')
          .having((e) => e.channelName, 'channelName', 'ops'),
    );
  });

  test('parses a friend_updated event', () {
    final event = TcEvent.tryParse(jsonEncode({
      'type': 'friend_updated',
      'identity_hash': 'abc123',
    }));

    expect(event, isA<FriendUpdatedEvent>());
    expect((event as FriendUpdatedEvent).identityHash, 'abc123');
  });

  test('voice_roster parses into VoiceRosterEvent', () {
    final event = TcEvent.tryParse(jsonEncode({
      'type': 'voice_roster',
      'channel_hash': 'channel-voice',
    }));

    expect(
      event,
      isA<VoiceRosterEvent>().having((e) => e.channelHash, 'channelHash', 'channel-voice'),
    );
  });

  test('voice_speaking parses into VoiceSpeakingEvent', () {
    final event = TcEvent.tryParse(jsonEncode({
      'type': 'voice_speaking',
      'channel_hash': 'channel-voice',
      'identity_hash': 'abc123',
      'speaking': true,
    }));

    expect(
      event,
      isA<VoiceSpeakingEvent>()
          .having((e) => e.channelHash, 'channelHash', 'channel-voice')
          .having((e) => e.identityHash, 'identityHash', 'abc123')
          .having((e) => e.speaking, 'speaking', true),
    );
  });

  test('voice_session parses into VoiceSessionEvent', () {
    final event = TcEvent.tryParse(jsonEncode({
      'type': 'voice_session',
      'state': 'audio_error',
    }));

    expect(event, isA<VoiceSessionEvent>().having((e) => e.state, 'state', 'audio_error'));
  });

  test('ui_theme carries the whole theme document', () {
    final event = TcEvent.tryParse(jsonEncode({
      'type': 'ui_theme',
      'theme': {
        'version': 1,
        'base': {'bgApp': '#ff0000'},
      },
    }));

    expect(event, isA<UiThemeEvent>());
    expect((event as UiThemeEvent).spec.base['bgApp'], const Color(0xFFFF0000));
  });

  test('ui_theme_library carries every saved theme, skipping junk entries', () {
    final event = TcEvent.tryParse(jsonEncode({
      'type': 'ui_theme_library',
      'themes': {
        'Deep': {
          'version': 1,
          'base': {'bgApp': '#ff0000'},
        },
        'Broken': 'not a document',
      },
    }));

    expect(event, isA<UiThemeLibraryEvent>());
    final library = (event as UiThemeLibraryEvent).library;
    expect(library.keys, ['Deep']);
    expect(library['Deep']!.base['bgApp'], const Color(0xFFFF0000));
  });

  test('a theme event with no document is ignored', () {
    expect(TcEvent.tryParse(jsonEncode({'type': 'ui_theme'})), isNull);
    expect(TcEvent.tryParse(jsonEncode({'type': 'ui_theme_library'})), isNull);
  });

  test('unknown event types are ignored', () {
    expect(TcEvent.tryParse(jsonEncode({'type': 'sync_status'})), isNull);
  });
}
