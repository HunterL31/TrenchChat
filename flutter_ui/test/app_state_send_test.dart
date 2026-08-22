import 'dart:convert';

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

  test('an attached image rides along with the send', () async {
    backend.routes['POST /channels/$_channelHash/messages'] = {'ok': true};
    backend.routes['GET /channels/$_channelHash/messages'] = <Object>[];

    expect(
      await state.sendMessage('look', imageDataB64: base64Encode([1, 2, 3])),
      isTrue,
    );

    final sent = backend.requests
        .lastWhere((r) => r.path.endsWith('/messages') && r.method == 'POST');
    expect(jsonDecode(sent.body)['image_data_b64'], base64Encode([1, 2, 3]));
  });

  test('an image with no text is still a send', () async {
    backend.routes['POST /channels/$_channelHash/messages'] = {'ok': true};
    backend.routes['GET /channels/$_channelHash/messages'] = <Object>[];

    expect(
      await state.sendMessage('', imageDataB64: base64Encode([1, 2, 3])),
      isTrue,
    );
  });

  test('a send with neither text nor image never reaches the backend', () async {
    expect(await state.sendMessage('   '), isFalse);
    expect(backend.requests.where((r) => r.method == 'POST'), isEmpty);
  });

  test('a message attachment is fetched once and cached', () async {
    var fetches = 0;
    backend.routes['GET /channels/$_channelHash/messages/m1/image'] =
        <String, Object>{};
    final client = backend.client();
    state.dispose();
    state = AppState(baseUrl: backend.baseUrl, httpClient: client);
    backend.requests.clear();

    await state.attachmentFor(_channelHash, 'm1');
    fetches = backend.requests
        .where((r) => r.path.endsWith('/messages/m1/image'))
        .length;
    expect(fetches, 1);

    await state.attachmentFor(_channelHash, 'm1');
    expect(
      backend.requests.where((r) => r.path.endsWith('/messages/m1/image')).length,
      1,
    );
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
