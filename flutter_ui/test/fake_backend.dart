// Canned-response API transport for dialog/tab tests that need a real
// request/response round trip (unlike the dead-port tests, which only need
// failure). flutter_test's binding stubs all real HTTP to status 400, so
// this rides package:http's MockClient instead of a socket: canned JSON per
// "METHOD /path", every request recorded. Responses complete in microtasks,
// so a plain pump() after the interaction is enough to settle them.
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

class RecordedRequest {
  const RecordedRequest(this.method, this.path, this.body);
  final String method;
  final String path;
  final String body;
}

class FakeBackend {
  /// Seeded with the routes every AppState startup touches regardless of
  /// what a test is exercising; a test overrides an entry by assigning it.
  final Map<String, Object> routes = {
    'GET /ui_theme': <String, dynamic>{'theme': <String, dynamic>{}},
    'POST /ui_theme': <String, dynamic>{'ok': true},
  };
  final List<RecordedRequest> requests = [];

  String get baseUrl => 'http://fake.test';

  http.Client client() => MockClient((req) async {
        requests.add(RecordedRequest(req.method, req.url.path, req.body));
        final handler = routes['${req.method} ${req.url.path}'];
        if (handler == null) {
          return http.Response(jsonEncode({'error': 'not found'}), 404,
              headers: {'content-type': 'application/json'});
        }
        return http.Response(jsonEncode(handler), 200,
            headers: {'content-type': 'application/json'});
      });
}

/// Flushes the mock transport's microtask-completed futures and any
/// resulting animations.
Future<void> settle(WidgetTester tester) => tester.pumpAndSettle();
