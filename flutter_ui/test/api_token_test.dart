import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:flutter_ui/api/client.dart';
import 'package:flutter_ui/main.dart';

void main() {
  group('resolveToken', () {
    test('web reads the ?token= query parameter', () {
      expect(
        resolveToken(
          isWeb: true,
          pageUri: Uri.parse('http://127.0.0.1:8810/?token=abc123'),
        ),
        'abc123',
      );
    });

    test('desktop honors the TC_API_TOKEN process environment variable', () {
      expect(
        resolveToken(isWeb: false, environment: const {'TC_API_TOKEN': 'xyz'}),
        'xyz',
      );
    });

    test('web ignores the process environment', () {
      expect(
        resolveToken(
          isWeb: true,
          pageUri: Uri.parse('http://127.0.0.1:8810/'),
          environment: const {'TC_API_TOKEN': 'xyz'},
        ),
        '',
      );
    });

    test('missing token resolves to empty rather than a guess', () {
      expect(resolveToken(isWeb: false, environment: const {}), '');
      expect(
        resolveToken(isWeb: true, pageUri: Uri.parse('http://127.0.0.1:8810/')),
        '',
      );
    });
  });

  group('ApiClient token header', () {
    test('sends the token on every request', () async {
      final seen = <String?>[];
      final mock = MockClient((request) async {
        seen.add(request.headers[tokenHeader]);
        return http.Response('{"hash_hex":"aa","display_name":"A"}', 200,
            headers: {'content-type': 'application/json'});
      });

      final client = ApiClient(
          baseUrl: 'http://127.0.0.1:8810', client: mock, token: 'secret');
      await client.getMe();

      expect(seen, ['secret']);
    });

    test('sends no token header when none was given', () async {
      final seen = <String?>[];
      final mock = MockClient((request) async {
        seen.add(request.headers[tokenHeader]);
        return http.Response('{"hash_hex":"aa","display_name":"A"}', 200,
            headers: {'content-type': 'application/json'});
      });

      final client =
          ApiClient(baseUrl: 'http://127.0.0.1:8810', client: mock);
      await client.getMe();

      expect(seen, [null]);
    });
  });
}
