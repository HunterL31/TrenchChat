import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/app_state.dart';

import 'fake_backend.dart';

const _channelHash = 'channel-general';

void main() {
  late FakeBackend backend;
  late AppState state;

  setUp(() {
    backend = FakeBackend();
    state = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
    state.selectedChannelHash = _channelHash;
  });

  tearDown(() {
    state.dispose();
  });

  test('a stored send refreshes the channel so the sender sees their message',
      () async {
    backend.routes['POST /channels/$_channelHash/messages'] = {'ok': true};
    backend.routes['GET /channels/$_channelHash/messages'] = [
      {
        'channel_hash': _channelHash,
        'message_id': 'm1',
        'sender_hash': 'aa',
        'sender_name': 'me',
        'content': 'hello mesh',
        'timestamp': 1000.0,
      },
    ];

    expect(await state.sendMessage('hello mesh'), isTrue);
    // The refresh is fire-and-forget; give its microtasks a turn.
    await Future<void>.delayed(Duration.zero);

    expect(state.messagesByChannel[_channelHash], hasLength(1));
    expect(state.messagesByChannel[_channelHash]!.single.content, 'hello mesh');
    expect(state.actionError, isNull);
  });

  test('a rejected send surfaces the backend reason instead of vanishing',
      () async {
    backend.routes['POST /channels/$_channelHash/messages'] = {
      'ok': false,
      'reason': 'no_recipients',
    };

    expect(await state.sendMessage('hello mesh'), isFalse);
    expect(state.actionError, contains('no known subscribers'));
  });
}
