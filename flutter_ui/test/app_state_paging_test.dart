// History paging (#38): loadOlderMessages fetches the next older page with a
// before_ts cursor, prepends it, and stops asking once history runs short.
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:flutter_ui/api/models/message.dart';
import 'package:flutter_ui/app_state.dart';

const _hash = 'chan-hash';

Map<String, Object?> _msgJson(String id, double ts) => {
      'channel_hash': _hash,
      'message_id': id,
      'sender_hash': 'aa',
      'sender_name': 'peer',
      'content': id,
      'timestamp': ts,
    };

Message _msg(String id, double ts) => Message.fromJson(_msgJson(id, ts));

void main() {
  test('loadOlderMessages fetches a before_ts page and prepends it', () async {
    double? seenBeforeTs;
    final client = MockClient((req) async {
      seenBeforeTs = double.tryParse(req.url.queryParameters['before_ts'] ?? '');
      // One older page of 50 messages, all older than the cursor.
      final older = [for (var i = 0; i < 50; i++) _msgJson('old-$i', 100.0 + i)];
      return http.Response(jsonEncode(older), 200,
          headers: {'content-type': 'application/json'});
    });
    final state = AppState(baseUrl: 'http://fake.test', httpClient: client);
    addTearDown(state.dispose);

    // A full newest page is already loaded (timestamps 1000+).
    state.messagesByChannel[_hash] = [for (var i = 0; i < 50; i++) _msg('new-$i', 1000.0 + i)];
    state.hasMoreOlderByChannel[_hash] = true;

    await state.loadOlderMessages(_hash);

    // The cursor was the oldest loaded timestamp.
    expect(seenBeforeTs, 1000.0);
    // The older page landed at the front; the newest message is unchanged.
    final msgs = state.messagesByChannel[_hash]!;
    expect(msgs, hasLength(100));
    expect(msgs.first.messageId, 'old-0');
    expect(msgs.last.messageId, 'new-49');
    // A full page back means there may be more.
    expect(state.hasMoreOlder(_hash), isTrue);
    expect(state.loadingOlder(_hash), isFalse);
  });

  test('a short older page marks the end of history and stops future fetches', () async {
    var calls = 0;
    final client = MockClient((req) async {
      calls++;
      // Only three messages left before the cursor.
      final older = [for (var i = 0; i < 3; i++) _msgJson('old-$i', 100.0 + i)];
      return http.Response(jsonEncode(older), 200,
          headers: {'content-type': 'application/json'});
    });
    final state = AppState(baseUrl: 'http://fake.test', httpClient: client);
    addTearDown(state.dispose);

    state.messagesByChannel[_hash] = [_msg('new-0', 1000.0)];
    state.hasMoreOlderByChannel[_hash] = true;

    await state.loadOlderMessages(_hash);
    expect(calls, 1);
    expect(state.hasMoreOlder(_hash), isFalse);

    // With history exhausted, another call is a no-op.
    await state.loadOlderMessages(_hash);
    expect(calls, 1);
  });

  test('loadOlderMessages is a no-op when nothing more is known', () async {
    var calls = 0;
    final client = MockClient((req) async {
      calls++;
      return http.Response('[]', 200, headers: {'content-type': 'application/json'});
    });
    final state = AppState(baseUrl: 'http://fake.test', httpClient: client);
    addTearDown(state.dispose);

    state.messagesByChannel[_hash] = [_msg('new-0', 1000.0)];
    state.hasMoreOlderByChannel[_hash] = false;

    await state.loadOlderMessages(_hash);
    expect(calls, 0);
  });
}
