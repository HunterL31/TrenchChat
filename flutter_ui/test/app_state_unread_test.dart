// Channel unread accounting: counts load from the backend, live messages
// bump only channels not on screen, and selecting a channel clears its badge
// and persists the read watermark.
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/events.dart';
import 'package:flutter_ui/api/models/dm.dart';
import 'package:flutter_ui/api/models/message.dart';
import 'package:flutter_ui/app_state.dart';

import 'fake_backend.dart';

Message _msg(String sender, String content) => Message(
      messageId: 'id-$sender-$content',
      senderHash: sender,
      senderName: sender,
      content: content,
      timestamp: 1700000000,
      replyTo: null,
      hasImage: false,
      reactions: const [],
    );

void _seedChannelReads(FakeBackend backend, String hash) {
  backend.routes['GET /channels/$hash/members'] = <Object>[];
  backend.routes['GET /channels/$hash/messages'] = <Object>[];
  backend.routes['GET /channels/$hash/presence'] = <Object>[];
  backend.routes['GET /channels/$hash/link_quality'] = <Object>[];
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
    state.meHashHex = 'me';
    state.selectedChannelHash = 'hash-alpha';
    backend.routes['POST /channels/hash-beta/read'] = {'ok': true};
    backend.routes['POST /channels/hash-alpha/read'] = {'ok': true};
  });

  tearDown(() => state.dispose());

  test('a live message for another channel bumps its unread count', () {
    state.applyEvent(MessageEvent('hash-beta', _msg('alice', 'hi')));
    expect(state.unreadByChannel['hash-beta'], 1);
    state.applyEvent(MessageEvent('hash-beta', _msg('alice', 'again')));
    expect(state.unreadByChannel['hash-beta'], 2);
  });

  test('a duplicate delivery of the same message does not double-count', () {
    final m = _msg('alice', 'once');
    state.applyEvent(MessageEvent('hash-beta', m));
    state.applyEvent(MessageEvent('hash-beta', m));
    expect(state.unreadByChannel['hash-beta'], 1);
  });

  test('own messages never count as unread', () {
    state.applyEvent(MessageEvent('hash-beta', _msg('me', 'mine')));
    expect(state.unreadByChannel['hash-beta'], isNull);
  });

  test('the channel on screen stays at zero and advances the watermark',
      () async {
    state.applyEvent(MessageEvent('hash-alpha', _msg('alice', 'hi')));
    expect(state.unreadByChannel['hash-alpha'], isNull);
    await Future<void>.delayed(Duration.zero);
    expect(
      backend.requests.any(
          (r) => r.method == 'POST' && r.path == '/channels/hash-alpha/read'),
      isTrue,
    );
  });

  test('a message in a conversation refreshes the DM list, not the channel map',
      () async {
    state.dms = [
      DmConversation.fromJson({
        'hash': 'dm-1',
        'peer_hash': 'peer',
        'display_name': 'peer',
        'created_at': 0,
        'last_message_at': 0,
        'unread': 0,
        'is_online': true,
        'is_friend': true,
        'peer_is_trenchchat': true,
      }),
    ];
    backend.routes['GET /dms'] = <Object>[];
    state.applyEvent(MessageEvent('dm-1', _msg('peer', 'yo')));
    expect(state.unreadByChannel['dm-1'], isNull);
    await Future<void>.delayed(Duration.zero);
    expect(
      backend.requests.any((r) => r.method == 'GET' && r.path == '/dms'),
      isTrue,
    );
  });

  test('selecting a channel clears its badge and persists the read', () async {
    _seedChannelReads(backend, 'hash-beta');
    state.applyEvent(MessageEvent('hash-beta', _msg('alice', 'hi')));
    expect(state.unreadByChannel['hash-beta'], 1);

    await state.selectChannel('hash-beta');
    expect(state.unreadByChannel['hash-beta'], 0);
    await Future<void>.delayed(Duration.zero);
    expect(
      backend.requests.any(
          (r) => r.method == 'POST' && r.path == '/channels/hash-beta/read'),
      isTrue,
    );
  });

  test('refreshUnreadCounts loads the map but keeps the open channel at zero',
      () async {
    backend.routes['GET /channels/unread'] = {
      'counts': {'hash-alpha': 5, 'hash-beta': 2},
    };
    await state.refreshUnreadCounts();
    expect(state.unreadByChannel['hash-beta'], 2);
    expect(state.unreadByChannel['hash-alpha'], 0);
  });

  test('a backend without the unread endpoint is non-fatal', () async {
    await state.refreshUnreadCounts();
    expect(state.unreadByChannel, isEmpty);
  });
}
