import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/events.dart';
import 'package:flutter_ui/app_state.dart';

import 'fake_backend.dart';

const _node = 'ccbbccbbccbbccbbccbbccbbccbbccbb';

void main() {
  late FakeBackend backend;
  late AppState state;

  setUp(() {
    backend = FakeBackend();
    state = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
  });

  tearDown(() => state.dispose());

  test('loadNomadNodes fills the node map', () async {
    backend.routes['GET /nomad/nodes'] = [
      {
        'node_hash': _node,
        'display_name': 'A Node',
        'first_seen': 100.0,
        'last_seen': 200.0,
      },
    ];
    await state.loadNomadNodes();
    expect(state.nomadNodes, hasLength(1));
    expect(state.nomadNodes[_node]!.displayName, 'A Node');
  });

  test('a nomad_node event upserts without losing first_seen', () {
    state.applyEvent(TcEvent.tryParse(jsonEncode({
      'type': 'nomad_node',
      'node_hash': _node,
      'display_name': 'First',
    }))!);
    final firstSeen = state.nomadNodes[_node]!.firstSeen;
    state.applyEvent(TcEvent.tryParse(jsonEncode({
      'type': 'nomad_node',
      'node_hash': _node,
      'display_name': 'Renamed',
    }))!);
    expect(state.nomadNodes[_node]!.displayName, 'Renamed');
    expect(state.nomadNodes[_node]!.firstSeen, firstSeen);
  });

  test('browseNomad records the fetch and events advance it', () async {
    backend.routes['POST /nomad/browse'] = {
      'ok': true,
      'fetch_id': 'f1',
      'node_hash': _node,
      'path': '/page/index.mu',
      'kind': 'page',
    };
    final target = await state.browseNomad('$_node:/page/index.mu');
    expect(target!.fetchId, 'f1');
    expect(target.nodeHash, _node);
    expect(target.path, '/page/index.mu');
    expect(target.cached, isFalse);
    expect(state.nomadFetches['f1']!.status, 'queued');

    state.applyEvent(TcEvent.tryParse(jsonEncode({
      'type': 'nomad_fetch',
      'fetch_id': 'f1',
      'node_hash': _node,
      'path': '/page/index.mu',
      'status': 'done',
      'progress': 1.0,
      'reason': null,
    }))!);
    expect(state.nomadFetches['f1']!.status, 'done');

    final taken = state.takeNomadFetch('f1');
    expect(taken!.isTerminal, isTrue);
    expect(state.nomadFetches.containsKey('f1'), isFalse);
  });

  test('a rejected URL surfaces an action error, not a crash', () async {
    backend.routes['POST /nomad/browse'] =
        const FakeError(400, {'ok': false, 'error': 'invalid request path'});
    final target = await state.browseNomad('$_node:/etc/passwd');
    expect(target, isNull);
    expect(state.takeActionError(), contains('invalid request path'));
  });

  test('fetchCachedNomadPage decodes content and misses return null', () async {
    backend.routes['GET /nomad/page/$_node'] = {
      'ok': true,
      'content_b64': base64Encode(utf8.encode('>Hello')),
      'fetched_at': 123.0,
    };
    final page = await state.fetchCachedNomadPage(_node, '/page/index.mu');
    expect(page!.source, '>Hello');

    backend.routes.remove('GET /nomad/page/$_node');
    final miss = await state.fetchCachedNomadPage(_node, '/page/index.mu');
    expect(miss, isNull);
  });

  test('toggleNomadBookmark round-trips through the API', () async {
    backend.routes['POST /nomad/bookmarks'] = {'ok': true};
    backend.routes['POST /nomad/bookmarks/delete'] = {'ok': true};
    backend.routes['GET /nomad/bookmarks'] = [
      {
        'node_hash': _node,
        'path': '/page/index.mu',
        'label': 'Home',
        'added_at': 1.0,
      },
    ];
    await state.toggleNomadBookmark(_node, '/page/index.mu', 'Home');
    expect(state.isNomadBookmarked(_node, '/page/index.mu'), isTrue);
    expect(
      backend.requests
          .any((r) => r.method == 'POST' && r.path == '/nomad/bookmarks'),
      isTrue,
    );

    backend.routes['GET /nomad/bookmarks'] = <Object>[];
    await state.toggleNomadBookmark(_node, '/page/index.mu', 'Home');
    expect(state.isNomadBookmarked(_node, '/page/index.mu'), isFalse);
    expect(
      backend.requests.any(
          (r) => r.method == 'POST' && r.path == '/nomad/bookmarks/delete'),
      isTrue,
    );
  });

  test('reconnect refreshes nodes only once the tab has loaded them', () async {
    backend.routes['GET /nomad/nodes'] = <Object>[];
    state.simulateReconnect();
    await Future<void>.delayed(Duration.zero);
    expect(
      backend.requests.any((r) => r.path == '/nomad/nodes'),
      isFalse,
    );

    backend.routes['GET /nomad/nodes'] = [
      {
        'node_hash': _node,
        'display_name': 'A Node',
        'first_seen': 1.0,
        'last_seen': 2.0,
      },
    ];
    await state.loadNomadNodes();
    backend.requests.clear();
    state.simulateReconnect();
    await Future<void>.delayed(Duration.zero);
    expect(
      backend.requests.any((r) => r.path == '/nomad/nodes'),
      isTrue,
    );
  });

  test('a done event that beats the browse response is not clobbered', () async {
    backend.routes['POST /nomad/browse'] = {
      'ok': true,
      'fetch_id': 'f2',
      'node_hash': _node,
      'path': '/page/index.mu',
      'kind': 'page',
    };
    // On a warm link the WS done event can land before browseNomad's own
    // continuation runs; the terminal status must survive.
    state.applyEvent(TcEvent.tryParse(jsonEncode({
      'type': 'nomad_fetch',
      'fetch_id': 'f2',
      'node_hash': _node,
      'path': '/page/index.mu',
      'status': 'done',
      'progress': 1.0,
      'reason': null,
    }))!);
    final target = await state.browseNomad('$_node:/page/index.mu');
    expect(target!.fetchId, 'f2');
    expect(state.nomadFetches['f2']!.status, 'done');
  });

  test('a page answered from its own declared cache starts no fetch', () async {
    // The backend serves a page still inside its #!c= lifetime without
    // asking the node again, so there is no fetch to wait on.
    backend.routes['POST /nomad/browse'] = {
      'ok': true,
      'fetch_id': null,
      'node_hash': _node,
      'path': '/page/index.mu',
      'kind': 'page',
      'cached': true,
    };
    final target = await state.browseNomad('$_node:/page/index.mu');
    expect(target!.cached, isTrue);
    expect(target.fetchId, isNull);
    expect(target.nodeHash, _node);
    expect(state.nomadFetches, isEmpty);
  });

  test('RELOAD asks the node again instead of taking the cache', () async {
    backend.routes['POST /nomad/browse'] = {
      'ok': true,
      'fetch_id': 'f3',
      'node_hash': _node,
      'path': '/page/index.mu',
      'kind': 'page',
      'cached': false,
    };
    await state.browseNomad('$_node:/page/index.mu', refresh: true);
    final sent = backend.requests.last;
    expect(sent.path, '/nomad/browse');
    expect(sent.body, contains('"refresh":true'));
  });

  group('identify', () {
    test('loading records the node state', () async {
      backend.routes['GET /nomad/identify/$_node'] = {
        'node_hash': _node,
        'enabled': true,
        'identified': false,
        'identity_hash': 'ab' * 16,
      };
      final status = await state.loadNomadIdentify(_node);
      expect(status!.enabled, isTrue);
      expect(status.identified, isFalse);
      expect(state.nomadIdentify[_node]!.identityHash, 'ab' * 16);
    });

    test('setting sends the node hash and the choice', () async {
      backend.routes['POST /nomad/identify'] = {
        'ok': true,
        'node_hash': _node,
        'enabled': true,
        'identified': true,
        'identity_hash': 'ab' * 16,
      };
      final status = await state.setNomadIdentify(_node, true);
      expect(status!.identified, isTrue);
      final sent = backend.requests.last;
      expect(sent.path, '/nomad/identify');
      expect(sent.body, contains('"node_hash":"$_node"'));
      expect(sent.body, contains('"enabled":true'));
    });

    test('a refused change surfaces an error and stores nothing', () async {
      final status = await state.setNomadIdentify(_node, true);
      expect(status, isNull);
      expect(state.nomadIdentify[_node], isNull);
      expect(state.takeActionError(), isNotNull);
    });
  });

  group('partials', () {
    setUp(() {
      backend.routes['POST /nomad/browse'] = {
        'ok': true,
        'fetch_id': 'p1',
        'node_hash': _node,
        'path': '/page/side.mu',
        'kind': 'page',
        'cached': false,
      };
      backend.routes['GET /nomad/fetch/p1'] = {
        'ok': true,
        'fetch_id': 'p1',
        'node_hash': _node,
        'path': '/page/side.mu',
        'kind': 'page',
        'status': 'done',
        'progress': 1.0,
        'reason': null,
      };
      backend.routes['GET /nomad/page/$_node'] = {
        'ok': true,
        'content_b64': base64Encode(utf8.encode('side content')),
        'fetched_at': 1.0,
      };
    });

    test('a partial is fetched and read back as micron source', () async {
      final source = await state.loadNomadPartial(':/page/side.mu',
          currentNode: _node,
          interval: const Duration(milliseconds: 1));
      expect(source, 'side content');
      final browse = backend.requests
          .firstWhere((r) => r.path == '/nomad/browse');
      // The refresh interval is the page author's; the page cache must not
      // hold a partial at its last value.
      expect(browse.body, contains('"refresh":true'));
    });

    test('a partial the node never answers yields nothing', () async {
      backend.routes['GET /nomad/fetch/p1'] = {
        'ok': true,
        'fetch_id': 'p1',
        'node_hash': _node,
        'path': '/page/side.mu',
        'kind': 'page',
        'status': 'failed',
        'progress': 0.0,
        'reason': 'timeout',
      };
      final source = await state.loadNomadPartial(':/page/side.mu',
          currentNode: _node,
          interval: const Duration(milliseconds: 1));
      expect(source, isNull);
    });

    test('a partial answered from cache needs no fetch at all', () async {
      backend.routes['POST /nomad/browse'] = {
        'ok': true,
        'fetch_id': null,
        'node_hash': _node,
        'path': '/page/side.mu',
        'kind': 'page',
        'cached': true,
      };
      final source = await state.loadNomadPartial(':/page/side.mu',
          currentNode: _node,
          interval: const Duration(milliseconds: 1));
      expect(source, 'side content');
      expect(state.nomadFetches, isEmpty);
    });

    test('awaitNomadFetch gives up rather than waiting forever', () async {
      backend.routes['GET /nomad/fetch/p1'] = {
        'ok': true,
        'fetch_id': 'p1',
        'node_hash': _node,
        'path': '/page/side.mu',
        'kind': 'page',
        'status': 'fetching',
        'progress': 0.5,
        'reason': null,
      };
      final status = await state.awaitNomadFetch('p1',
          timeout: const Duration(milliseconds: 20),
          interval: const Duration(milliseconds: 1));
      expect(status, isNull);
    });
  });
}
