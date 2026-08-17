import 'dart:convert';

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

  test('unknown event types are ignored', () {
    expect(TcEvent.tryParse(jsonEncode({'type': 'sync_status'})), isNull);
  });
}
