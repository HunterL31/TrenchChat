import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/events.dart';
import 'package:flutter_ui/app_state.dart';
import 'package:flutter_ui/screens/main_window/browser_tab.dart';

import '../fake_backend.dart';

const _node = 'ddaaddaaddaaddaaddaaddaaddaaddaa';

Widget _harness(AppState state) =>
    MaterialApp(home: Scaffold(body: BrowserTab(state: state)));

void _emitFetchEvent(AppState state, String fetchId, String status,
    {String? reason}) {
  state.applyEvent(TcEvent.tryParse(jsonEncode({
    'type': 'nomad_fetch',
    'fetch_id': fetchId,
    'node_hash': _node,
    'path': '/page/index.mu',
    'status': status,
    'progress': status == 'done' ? 1.0 : 0.0,
    'reason': reason,
  }))!);
}

void main() {
  late FakeBackend backend;
  late AppState state;

  setUp(() {
    backend = FakeBackend();
    backend.routes['GET /nomad/nodes'] = [
      {
        'node_hash': _node,
        'display_name': 'Test Node',
        'first_seen': 100.0,
        'last_seen': 200.0,
      },
    ];
    backend.routes['GET /nomad/bookmarks'] = <Object>[];
    backend.routes['POST /nomad/browse'] = {
      'ok': true,
      'fetch_id': 'f1',
      'node_hash': _node,
      'path': '/page/index.mu',
      'kind': 'page',
    };
    state = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
  });

  tearDown(() => state.dispose());

  testWidgets('lists discovered nodes with name and hash', (tester) async {
    await tester.pumpWidget(_harness(state));
    await settle(tester);
    expect(find.text('Test Node'), findsOneWidget);
    expect(find.textContaining('ddaaddaaddaa'), findsOneWidget);
  });

  testWidgets('tapping a node starts a fetch and a done event renders the page',
      (tester) async {
    backend.routes['GET /nomad/page/$_node'] = {
      'ok': true,
      'content_b64': base64Encode(utf8.encode('>Welcome aboard')),
      'fetched_at': 1.0,
    };
    await tester.pumpWidget(_harness(state));
    await settle(tester);

    await tester.tap(find.text('Test Node'));
    await settle(tester);
    expect(
      backend.requests.any((r) => r.path == '/nomad/browse'),
      isTrue,
    );

    _emitFetchEvent(state, 'f1', 'done');
    await settle(tester);
    expect(find.textContaining('Welcome aboard'), findsOneWidget);
  });

  testWidgets('a failed fetch shows the friendly reason with RETRY',
      (tester) async {
    await tester.pumpWidget(_harness(state));
    await settle(tester);

    await tester.tap(find.text('Test Node'));
    await settle(tester);

    _emitFetchEvent(state, 'f1', 'failed', reason: 'unreachable');
    await settle(tester);
    expect(find.textContaining('Node unreachable'), findsOneWidget);
    expect(find.text('RETRY'), findsOneWidget);
  });

  testWidgets('a page still renders when no fetch event ever arrives',
      (tester) async {
    // The socket can be down for the whole fetch, in which case the only way
    // the tab learns is by asking. Regression guard for a page that sat on
    // FETCHING until the user pressed RELOAD.
    backend.routes['GET /nomad/page/$_node'] =
        const FakeError(404, {'ok': false, 'reason': 'not_cached'});
    await tester.pumpWidget(_harness(state));
    await settle(tester);
    await tester.tap(find.text('Test Node'));
    await settle(tester);
    expect(find.textContaining('arrived by polling', findRichText: true),
        findsNothing);

    // The backend finishes and caches the page while the socket is down, so
    // no nomad_fetch event is ever delivered.
    backend.routes['GET /nomad/page/$_node'] = {
      'ok': true,
      'content_b64': base64Encode(utf8.encode('arrived by polling')),
      'fetched_at': 1.0,
    };
    backend.routes['GET /nomad/fetch/f1'] = {
      'ok': true,
      'node_hash': _node,
      'path': '/page/index.mu',
      'status': 'done',
      'progress': 1.0,
      'reason': null,
    };

    await tester.pump(const Duration(seconds: 3));
    await settle(tester);
    expect(find.textContaining('arrived by polling', findRichText: true),
        findsOneWidget);
  });

  testWidgets('a fetch the backend has forgotten falls back to the cache',
      (tester) async {
    backend.routes['GET /nomad/page/$_node'] =
        const FakeError(404, {'ok': false, 'reason': 'not_cached'});
    await tester.pumpWidget(_harness(state));
    await settle(tester);
    await tester.tap(find.text('Test Node'));
    await settle(tester);

    // The page landed in the cache, but the backend no longer knows the
    // fetch id (restarted, or evicted), so its status endpoint 404s.
    backend.routes['GET /nomad/page/$_node'] = {
      'ok': true,
      'content_b64': base64Encode(utf8.encode('recovered from cache')),
      'fetched_at': 1.0,
    };

    await tester.pump(const Duration(seconds: 3));
    await settle(tester);
    expect(find.textContaining('recovered from cache', findRichText: true),
        findsOneWidget);
    expect(find.text('RETRY'), findsNothing);
  });

  testWidgets('back returns to the node list state after browsing',
      (tester) async {
    backend.routes['GET /nomad/page/$_node'] = {
      'ok': true,
      'content_b64': base64Encode(utf8.encode('page one')),
      'fetched_at': 1.0,
    };
    await tester.pumpWidget(_harness(state));
    await settle(tester);
    await tester.tap(find.text('Test Node'));
    await settle(tester);
    _emitFetchEvent(state, 'f1', 'done');
    await settle(tester);
    expect(find.textContaining('page one'), findsOneWidget);

    await tester.tap(find.text('NODES'));
    await settle(tester);
    expect(find.text('Test Node'), findsOneWidget);
  });
}
