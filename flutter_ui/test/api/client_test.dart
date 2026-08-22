// ApiClient's uniform non-2xx handling: every mutating call used to feed the
// raw response straight to jsonDecode, so a 403/422/500 surfaced as a bare
// FormatException/TypeError instead of a message callers could show. This
// verifies the fix -- ApiException carrying the backend's own error text.
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:flutter_ui/api/client.dart';

void main() {
  test('a 2xx response decodes normally', () async {
    final client = ApiClient(
      baseUrl: 'http://example.test',
      client: MockClient((request) async {
        return http.Response(jsonEncode({'hash': 'abc123'}), 200);
      }),
    );

    final hash = await client.createServer('mesh-crew', '');
    expect(hash, 'abc123');
  });

  test('a 403 with a backend {"error": ...} body throws ApiException with that message',
      () async {
    final client = ApiClient(
      baseUrl: 'http://example.test',
      client: MockClient((request) async {
        return http.Response(
          jsonEncode({'error': 'missing create_channel on this server'}),
          403,
        );
      }),
    );

    await expectLater(
      () => client.createServerChannel('server-hash', 'ops', ''),
      throwsA(isA<ApiException>()
          .having((e) => e.statusCode, 'statusCode', 403)
          .having((e) => e.message, 'message', 'missing create_channel on this server')),
    );
  });

  test('a FastAPI validation 422 with a {"detail": ...} body throws ApiException', () async {
    final client = ApiClient(
      baseUrl: 'http://example.test',
      client: MockClient((request) async {
        return http.Response(
          jsonEncode({
            'detail': [
              {'loc': ['body', 'name'], 'msg': 'field required', 'type': 'missing'}
            ]
          }),
          422,
        );
      }),
    );

    await expectLater(
      () => client.createServer('', ''),
      throwsA(isA<ApiException>().having((e) => e.statusCode, 'statusCode', 422)),
    );
  });

  test('a non-JSON error body still throws ApiException instead of a raw decode error',
      () async {
    final client = ApiClient(
      baseUrl: 'http://example.test',
      client: MockClient((request) async {
        return http.Response('Internal Server Error', 500);
      }),
    );

    await expectLater(
      () => client.getServers(),
      throwsA(isA<ApiException>().having((e) => e.statusCode, 'statusCode', 500)),
    );
  });

  test('getPeerAvatar swallows a failure and returns null rather than throwing', () async {
    final client = ApiClient(
      baseUrl: 'http://example.test',
      client: MockClient((request) async {
        return http.Response('not json', 500);
      }),
    );

    final avatar = await client.getPeerAvatar('peer-hash');
    expect(avatar, isNull);
  });

  test('joinChannel decodes the ok flag', () async {
    final client = ApiClient(
      baseUrl: 'http://example.test',
      client: MockClient((request) async {
        expect(request.url.path, '/channels/chan-hash/join');
        return http.Response(jsonEncode({'ok': true}), 200);
      }),
    );

    expect(await client.joinChannel('chan-hash'), isTrue);
  });

  test('leaveChannel posts to the channel\'s leave route', () async {
    final client = ApiClient(
      baseUrl: 'http://example.test',
      client: MockClient((request) async {
        expect(request.method, 'POST');
        expect(request.url.path, '/channels/chan-hash/leave');
        return http.Response(jsonEncode({'ok': true}), 200);
      }),
    );

    expect(await client.leaveChannel('chan-hash'), isTrue);
  });

  test('getChannelPresence reads the channel presence endpoint with names', () async {
    final client = ApiClient(
      baseUrl: 'http://example.test',
      client: MockClient((request) async {
        expect(request.method, 'GET');
        expect(request.url.path, '/channels/chan-hash/presence');
        return http.Response(
          jsonEncode([
            {'identity_hash': 'aa', 'display_name': 'Alice', 'is_online': true},
            {'identity_hash': 'bb', 'display_name': '', 'is_online': false},
          ]),
          200,
        );
      }),
    );

    final roster = await client.getChannelPresence('chan-hash');
    expect(roster, hasLength(2));
    expect(roster.first.identityHash, 'aa');
    expect(roster.first.displayName, 'Alice');
    expect(roster.first.isOnline, isTrue);
    // An empty display name is stored as null, not "".
    expect(roster.last.displayName, isNull);
  });

  test('getChannelLinkQuality reads the channel link-quality endpoint', () async {
    final client = ApiClient(
      baseUrl: 'http://example.test',
      client: MockClient((request) async {
        expect(request.url.path, '/channels/chan-hash/link_quality');
        return http.Response(jsonEncode({'level': 'good', 'hops': 2}), 200);
      }),
    );

    final quality = await client.getChannelLinkQuality('chan-hash');
    expect(quality.level.name, 'good');
    expect(quality.hops, 2);
  });

  test('getMessages passes limit and before_ts as query params', () async {
    Uri? seen;
    final client = ApiClient(
      baseUrl: 'http://example.test',
      client: MockClient((request) async {
        seen = request.url;
        return http.Response(jsonEncode(<Object>[]), 200);
      }),
    );

    await client.getMessages('chan-hash', limit: 50, beforeTs: 1234.5);
    expect(seen!.path, '/channels/chan-hash/messages');
    expect(seen!.queryParameters['limit'], '50');
    expect(seen!.queryParameters['before_ts'], '1234.5');
  });

  test('leaveServer posts to the server leave route', () async {
    final client = ApiClient(
      baseUrl: 'http://example.test',
      client: MockClient((request) async {
        expect(request.method, 'POST');
        expect(request.url.path, '/servers/srv-hash/leave');
        return http.Response(jsonEncode({'ok': true}), 200);
      }),
    );

    expect(await client.leaveServer('srv-hash'), isTrue);
  });

  test('getFriends decodes the friend list', () async {
    final client = ApiClient(
      baseUrl: 'http://example.test',
      client: MockClient((request) async {
        expect(request.url.path, '/friends');
        // Mirrors a real FastAPI reply: UTF-8 bytes labelled `application/json`
        // with no charset, which package:http would otherwise read as latin1.
        return http.Response.bytes(
          utf8.encode(jsonEncode([
            {
              'identity_hash': 'abc123',
              'nickname': 'Alice',
              'note': 'runs the coast node',
              'display_name': 'f3a1…9c2e',
              'added_at': 1000.0,
              'last_seen_at': 2000.0,
              'is_online': true,
            }
          ])),
          200,
          headers: const {'content-type': 'application/json'},
        );
      }),
    );

    final friends = await client.getFriends();
    expect(friends, hasLength(1));
    expect(friends.first.identityHash, 'abc123');
    expect(friends.first.nickname, 'Alice');
    expect(friends.first.displayName, 'f3a1…9c2e');
    expect(friends.first.isOnline, isTrue);
  });

  test('addFriend posts identity_hash, nickname, and note', () async {
    final client = ApiClient(
      baseUrl: 'http://example.test',
      client: MockClient((request) async {
        expect(request.method, 'POST');
        expect(request.url.path, '/friends');
        final body = jsonDecode(request.body) as Map<String, dynamic>;
        expect(body, {'identity_hash': 'abc123', 'nickname': 'Alice', 'note': 'a note'});
        return http.Response(jsonEncode({'ok': true}), 200);
      }),
    );

    expect(await client.addFriend('abc123', 'Alice', 'a note'), isTrue);
  });

  test('addFriend surfaces a 400 backend error', () async {
    final client = ApiClient(
      baseUrl: 'http://example.test',
      client: MockClient((request) async {
        return http.Response(jsonEncode({'ok': false, 'error': 'already a friend'}), 400);
      }),
    );

    await expectLater(
      () => client.addFriend('abc123', '', ''),
      throwsA(isA<ApiException>()
          .having((e) => e.statusCode, 'statusCode', 400)
          .having((e) => e.message, 'message', 'already a friend')),
    );
  });

  test('updateFriend PUTs only the provided fields', () async {
    final client = ApiClient(
      baseUrl: 'http://example.test',
      client: MockClient((request) async {
        expect(request.method, 'PUT');
        expect(request.url.path, '/friends/abc123');
        final body = jsonDecode(request.body) as Map<String, dynamic>;
        expect(body, {'nickname': 'New name'});
        return http.Response(jsonEncode({'ok': true}), 200);
      }),
    );

    expect(await client.updateFriend('abc123', nickname: 'New name'), isTrue);
  });

  test('removeFriend DELETEs the friend', () async {
    final client = ApiClient(
      baseUrl: 'http://example.test',
      client: MockClient((request) async {
        expect(request.method, 'DELETE');
        expect(request.url.path, '/friends/abc123');
        return http.Response(jsonEncode({'ok': true}), 200);
      }),
    );

    expect(await client.removeFriend('abc123'), isTrue);
  });
}
