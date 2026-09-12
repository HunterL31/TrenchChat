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

  test('getChannelLinkQuality reads the summary and every peer row', () async {
    // The shape api.py really returns: a channel summary over a sorted roster.
    final client = ApiClient(
      baseUrl: 'http://example.test',
      client: MockClient((request) async {
        expect(request.url.path, '/channels/chan-hash/link_quality');
        return http.Response(
          jsonEncode({
            'summary': {
              'level': 3,
              'level_label': 'Good',
              'reachable': 2,
              'total': 3,
              'median_hops': 2,
              'best_identity_hash': 'bb',
              'best_hops': 2,
            },
            'peers': [
              {'identity_hash': 'bb', 'display_name': 'grace', 'quality': 3,
               'quality_label': 'Good', 'hops': 2, 'via': 'cc11',
               'rtt_ms': 42.5, 'path_expires_in': 300.0,
               'is_online': true, 'last_seen': 99.0},
              {'identity_hash': 'aa', 'display_name': 'ada', 'quality': 2,
               'quality_label': 'Fair', 'hops': 4, 'via': null,
               'rtt_ms': null, 'path_expires_in': 120.0,
               'is_online': false, 'last_seen': 0.0},
              {'identity_hash': 'dd', 'display_name': 'hopper', 'quality': 0,
               'quality_label': 'Unknown', 'hops': null, 'via': null,
               'rtt_ms': null, 'path_expires_in': null,
               'is_online': false, 'last_seen': 0.0},
            ],
          }),
          200,
        );
      }),
    );

    final quality = await client.getChannelLinkQuality('chan-hash');
    expect(quality.level.name, 'good');
    expect(quality.medianHops, 2);
    expect(quality.reachable, 2);
    expect(quality.total, 3);
    expect(quality.bestName, 'grace', reason: 'resolved out of the peer rows');
    expect(quality.bestHops, 2);

    // Server-side order is kept: the backend sorted it, the client does not.
    expect(quality.peers.map((p) => p.identityHash), ['bb', 'aa', 'dd']);
    final closest = quality.peers.first;
    expect(closest.displayName, 'grace');
    expect(closest.via, 'cc11');
    expect(closest.rttMs, 42.5);
    expect(closest.pathExpiresIn, 300.0);
    expect(closest.isOnline, isTrue);
    expect(closest.isReachable, isTrue);
    expect(quality.unreachablePeers.single.identityHash, 'dd');
  });

  test('getChannelLinkQuality reads an empty or unusable body as unknown',
      () async {
    Object body = {'summary': <String, Object?>{}, 'peers': <Object>[]};
    final client = ApiClient(
      baseUrl: 'http://example.test',
      client: MockClient((request) async => http.Response(jsonEncode(body), 200)),
    );

    final empty = await client.getChannelLinkQuality('chan-hash');
    expect(empty.level.name, 'unknown');
    expect(empty.reachable, 0);
    expect(empty.total, 0);
    expect(empty.peers, isEmpty);

    // A body that is not the agreed object at all still reads, rather than
    // taking the whole channel load down with it.
    body = <Object>[];
    expect((await client.getChannelLinkQuality('chan-hash')).level.name, 'unknown');

    body = {
      'summary': {'level': 0, 'reachable': 0, 'total': 1, 'median_hops': null},
      'peers': [
        {'identity_hash': 'aa', 'quality': 0, 'quality_label': 'Unknown', 'hops': null},
        'not an entry',
      ],
    };
    final quality = await client.getChannelLinkQuality('chan-hash');
    expect(quality.level.name, 'unknown');
    expect(quality.medianHops, isNull);
    expect(quality.bestName, isNull);
    expect(quality.peers.single.isReachable, isFalse);
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
