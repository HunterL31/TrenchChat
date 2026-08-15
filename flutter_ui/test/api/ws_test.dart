// Real-socket tests for TcSocket's reconnect behavior, against an in-process
// WebSocket server. Plain `test()` (no widget binding), so real IO works.
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/events.dart';
import 'package:flutter_ui/api/ws.dart';

void main() {
  late HttpServer server;
  final List<WebSocket> clients = [];

  setUp(() async {
    server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    server.listen((req) async {
      final ws = await WebSocketTransformer.upgrade(req);
      clients.add(ws);
    });
  });

  tearDown(() async {
    for (final ws in clients) {
      await ws.close();
    }
    clients.clear();
    await server.close(force: true);
  });

  Future<void> waitFor(bool Function() condition,
      {Duration timeout = const Duration(seconds: 10)}) async {
    final deadline = DateTime.now().add(timeout);
    while (!condition()) {
      if (DateTime.now().isAfter(deadline)) fail('timed out');
      await Future<void>.delayed(const Duration(milliseconds: 50));
    }
  }

  test('delivers events and reconnects after the server drops the socket', () async {
    final socket = TcSocket(baseUrl: 'http://127.0.0.1:${server.port}');
    addTearDown(socket.close);
    var reconnects = 0;
    socket.onReconnected = () => reconnects++;

    final received = <TcEvent>[];
    socket.events.listen(received.add);

    await waitFor(() => clients.length == 1);
    clients[0].add(jsonEncode(
        {'type': 'presence', 'identity_hash': 'aa', 'is_online': true}));
    await waitFor(() => received.length == 1);
    expect(received.single, isA<PresenceEvent>());
    expect(reconnects, 0);

    // Server drops the connection; the client comes back on its own.
    await clients[0].close();
    await waitFor(() => clients.length == 2);
    await waitFor(() => reconnects == 1);

    clients[1].add(jsonEncode(
        {'type': 'presence', 'identity_hash': 'bb', 'is_online': false}));
    await waitFor(() => received.length == 2);
  });

  test('close() stops reconnect attempts', () async {
    final socket = TcSocket(baseUrl: 'http://127.0.0.1:${server.port}');
    socket.events.listen((_) {});
    await waitFor(() => clients.length == 1);

    socket.close();
    await clients[0].close();
    await Future<void>.delayed(const Duration(seconds: 3));
    expect(clients.length, 1);
  });
}
