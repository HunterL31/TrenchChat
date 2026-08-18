// Reaction chips must appear without the user refreshing: a reaction_updated
// event has to pull the channel's messages again, because reaction counts
// only ride along with a message fetch.
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/events.dart';
import 'package:flutter_ui/api/models/message.dart';
import 'package:flutter_ui/app_state.dart';

import 'fake_backend.dart';

const _channelHash = 'channel-reactions';
const _messageId = 'msg-1';

Map<String, Object?> _message({required List<Map<String, Object>> reactions}) => {
      'message_id': _messageId,
      'sender_hash': 'peer-a',
      'sender_name': 'Peer A',
      'content': 'hello',
      'timestamp': 1000.0,
      'reply_to': null,
      'has_image': false,
      'reactions': reactions,
    };

/// Longer than AppState's coalescing window, so the refresh has fired.
Future<void> _pastTheWindow() =>
    Future<void>.delayed(const Duration(milliseconds: 400));

void main() {
  late FakeBackend backend;
  late AppState state;

  setUp(() {
    backend = FakeBackend();
    state = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
    state.selectedChannelHash = _channelHash;
    // A channel the user is looking at, whose message has no reactions yet.
    state.messagesByChannel[_channelHash] = [
      Message.fromJson(Map<String, dynamic>.from(_message(reactions: []))),
    ];
  });

  tearDown(() => state.dispose());

  int messageFetches() => backend.requests
      .where((r) =>
          r.method == 'GET' && r.path == '/channels/$_channelHash/messages')
      .length;

  test('a reaction event refreshes the channel so the chip renders', () async {
    backend.routes['GET /channels/$_channelHash/messages'] = [
      _message(reactions: [
        {'emoji_hash': '\u{1F44D}', 'count': 1, 'reacted_by_me': false},
      ]),
    ];

    state.applyEvent(const ReactionUpdatedEvent(_channelHash, _messageId));
    await _pastTheWindow();

    final reactions = state.messagesByChannel[_channelHash]!.single.reactions;
    expect(reactions.single.emojiHash, '\u{1F44D}');
    expect(reactions.single.count, 1);
  });

  test('a burst of reactions coalesces into one refresh', () async {
    backend.routes['GET /channels/$_channelHash/messages'] = [
      _message(reactions: [
        {'emoji_hash': '\u{1F44D}', 'count': 1, 'reacted_by_me': false},
      ]),
    ];

    for (var i = 0; i < 16; i++) {
      state.applyEvent(const ReactionUpdatedEvent(_channelHash, _messageId));
    }
    await _pastTheWindow();

    expect(messageFetches(), 1);
  });

  test('a later reaction refreshes again once the window has passed', () async {
    backend.routes['GET /channels/$_channelHash/messages'] = [
      _message(reactions: []),
    ];

    state.applyEvent(const ReactionUpdatedEvent(_channelHash, _messageId));
    await _pastTheWindow();
    state.applyEvent(const ReactionUpdatedEvent(_channelHash, _messageId));
    await _pastTheWindow();

    expect(messageFetches(), 2);
  });

  test('a reaction on a channel we never loaded fetches nothing', () async {
    state.applyEvent(const ReactionUpdatedEvent('other-channel', _messageId));
    await _pastTheWindow();

    expect(backend.requests, isEmpty);
  });
}
