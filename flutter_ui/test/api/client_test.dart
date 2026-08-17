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
}
