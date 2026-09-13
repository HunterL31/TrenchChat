// The path this node reaches each peer over, and the diagnostics behind it.
// Member rows and path_changed events feed one map, because a member row, a
// voice row and a diagnostics row are the same peer: a path that moved must
// move in all of them at once.
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/events.dart';
import 'package:flutter_ui/api/models/upgrade.dart';
import 'package:flutter_ui/app_state.dart';

import 'fake_backend.dart';

const _channel = 'cc11cc22cc33cc44cc55cc66cc77cc88';
const _alice = 'aa11bb22cc33dd44ee55ff6600112233';
const _bob = 'bb11bb22bb33bb44bb55bb66bb77bb88';

Map<String, dynamic> _member(String hash, String path) => {
      'channel_hash': _channel,
      'identity_hash': hash,
      'display_name': '',
      'role': 'member',
      'added_at': 1.0,
      'path': path,
    };

AppState _state(FakeBackend backend) =>
    AppState(baseUrl: backend.baseUrl, httpClient: backend.client());

void main() {
  test('member rows fill the path map, and a path_changed event moves it', () async {
    final backend = FakeBackend();
    backend.routes['GET /channels/$_channel/members'] = [
      _member(_alice, 'direct'),
      _member(_bob, 'reticulum'),
    ];
    backend.routes['GET /channels/$_channel/messages'] = <dynamic>[];
    backend.routes['GET /channels/$_channel/presence'] = <dynamic>[];
    backend.routes['GET /channels/$_channel/link_quality'] = <dynamic>[];
    backend.routes['GET /channels/$_channel/my_permissions'] = <String, dynamic>{};
    backend.routes['GET /channels/$_channel/voice/roster'] = <dynamic>[];
    backend.routes['GET /channels/$_channel/sync_status'] = {'state': 'synced'};
    final state = _state(backend);
    addTearDown(state.dispose);

    await state.loadChannel(_channel);
    expect(state.pathFor(_alice), PeerPath.direct);
    expect(state.pathFor(_bob), PeerPath.reticulum);
    // Nobody has said anything about this peer; unknown draws no badge.
    expect(state.pathFor('ff' * 16), PeerPath.unknown);

    state.applyEvent(const PathChangedEvent(_bob, PeerPath.direct, 1700.0));
    expect(state.pathFor(_bob), PeerPath.direct);

    state.applyEvent(const PathChangedEvent(_alice, PeerPath.reticulum, 1700.0));
    expect(state.pathFor(_alice), PeerPath.reticulum);
  });

  test('a path_changed event re-reads the sessions only once they are on screen',
      () async {
    final backend = FakeBackend();
    backend.routes['GET /upgrade/sessions'] = {
      'sessions': <dynamic>[],
      'last_failure': <String, dynamic>{},
      'listening': true,
      'listen_port': 42420,
    };
    final state = _state(backend);
    addTearDown(state.dispose);

    state.applyEvent(const PathChangedEvent(_alice, PeerPath.direct, 1700.0));
    await Future<void>.delayed(Duration.zero);
    expect(backend.requests.where((r) => r.path == '/upgrade/sessions'), isEmpty);

    await state.loadDirectSessions();
    state.applyEvent(const PathChangedEvent(_alice, PeerPath.reticulum, 1700.0));
    await Future<void>.delayed(Duration.zero);
    expect(backend.requests.where((r) => r.path == '/upgrade/sessions').length, 2);
  });

  test('the sessions listing carries the sessions, the failures and the port',
      () async {
    final backend = FakeBackend();
    backend.routes['GET /upgrade/sessions'] = {
      'sessions': [
        {
          'peer': _alice,
          'display_name': 'Alice',
          'since': 1700.0,
          'round_trip_secs': 0.012,
          'bytes_in': 4096,
          'bytes_out': 2048,
        }
      ],
      'last_failure': {
        _bob: {'reason': 'punch_failed', 'at': 1700.0, 'next_attempt': 1760.0},
      },
      'listening': true,
      'listen_port': 42420,
    };
    final state = _state(backend);
    addTearDown(state.dispose);

    await state.loadDirectSessions();

    expect(state.directSessions.listening, isTrue);
    expect(state.directSessions.listenPort, 42420);
    final session = state.directSessions.sessions.single;
    expect(session.peer, _alice);
    expect(session.displayName, 'Alice');
    expect(session.bytesIn, 4096);
    final failure = state.directSessions.failures.single;
    expect(failure.peer, _bob);
    expect(failure.reason, 'punch_failed');
    expect(failure.nextAttempt, 1760.0);
  });

  test('a backend with no direct path leaves the panel empty, not broken',
      () async {
    final backend = FakeBackend();
    final state = _state(backend);
    addTearDown(state.dispose);

    await state.loadDirectSessions();

    expect(state.directSessions.sessions, isEmpty);
    expect(state.directSessions.listening, isFalse);
    expect(state.error, isNull);
  });

  test('the switch posts to the backend and re-reads the sessions', () async {
    final backend = FakeBackend();
    backend.routes['POST /upgrade/enabled'] = {'ok': true, 'enabled': false};
    backend.routes['GET /upgrade/sessions'] = {
      'sessions': <dynamic>[],
      'last_failure': <String, dynamic>{},
      'listening': false,
      'listen_port': 0,
    };
    final state = _state(backend);
    addTearDown(state.dispose);

    expect(await state.setDirectConnections(false), isTrue);

    final post = backend.requests
        .singleWhere((r) => r.path == '/upgrade/enabled' && r.method == 'POST');
    expect(jsonDecode(post.body), {'enabled': false});
    expect(state.directConnections.enabled, isFalse);
    // Off closes the sessions it held, so what is listed must be re-read.
    expect(backend.requests.any((r) => r.path == '/upgrade/sessions'), isTrue);
  });

  test('try now and drop call their own endpoints', () async {
    final backend = FakeBackend();
    backend.routes['POST /upgrade/try/$_bob'] = {'ok': true, 'reason': null};
    backend.routes['POST /upgrade/close/$_alice'] = {'ok': true};
    backend.routes['GET /upgrade/sessions'] = {
      'sessions': <dynamic>[],
      'last_failure': <String, dynamic>{},
      'listening': true,
      'listen_port': 42420,
    };
    final state = _state(backend);
    addTearDown(state.dispose);

    expect(await state.tryDirectSession(_bob), isTrue);
    expect(await state.closeDirectSession(_alice), isTrue);

    expect(backend.requests.any((r) => r.path == '/upgrade/try/$_bob'), isTrue);
    expect(backend.requests.any((r) => r.path == '/upgrade/close/$_alice'), isTrue);
  });

  test('the sessions listing says when a pair is stuck for want of an address',
      () async {
    final backend = FakeBackend();
    backend.routes['GET /upgrade/sessions'] = {
      'sessions': <dynamic>[],
      'last_failure': {
        _bob: {
          'reason': 'no_public_address',
          'at': 1700.0,
          'next_attempt': 1760.0,
        },
      },
      'listening': true,
      'listen_port': 42420,
      'needs_public_address': true,
    };
    final state = _state(backend);
    addTearDown(state.dispose);

    await state.loadDirectSessions();

    expect(state.directSessions.needsPublicAddress, isTrue);
    expect(state.needsPublicAddress, isTrue);
    expect(state.shouldAskForPublicAddress, isTrue);
    expect(directFailureReason(state.directSessions.failures.single.reason),
        'No way to learn our public address yet');
  });

  test('the event moves it without the panel ever being opened', () async {
    final state = _state(FakeBackend());
    addTearDown(state.dispose);

    expect(state.shouldAskForPublicAddress, isFalse);
    state.applyEvent(const DirectAddressNeededEvent(true));

    expect(state.needsPublicAddress, isTrue);
    expect(state.shouldAskForPublicAddress, isTrue);

    state.applyEvent(const DirectAddressNeededEvent(false));
    expect(state.shouldAskForPublicAddress, isFalse);
  });

  test('once asked, this run does not ask again', () async {
    final state = _state(FakeBackend());
    addTearDown(state.dispose);
    state.applyEvent(const DirectAddressNeededEvent(true));

    state.markPublicAddressAsked();

    expect(state.needsPublicAddress, isTrue);
    expect(state.shouldAskForPublicAddress, isFalse,
        reason: 'the user was asked once and answered');
  });

  test('the address echo reads back and turns on through its own endpoint',
      () async {
    final backend = FakeBackend();
    backend.routes['GET /upgrade/stun'] = {
      'enabled': false,
      'servers': ['stun.example.com:3478'],
    };
    backend.routes['POST /upgrade/stun'] = {
      'ok': true,
      'enabled': true,
      'servers': ['stun.example.com:3478'],
    };
    final state = _state(backend);
    addTearDown(state.dispose);
    state.applyEvent(const DirectAddressNeededEvent(true));

    await state.loadStun();
    expect(state.stun.enabled, isFalse);
    expect(state.stun.servers, ['stun.example.com:3478']);

    expect(await state.setStun(enabled: true), isTrue);

    final post = backend.requests
        .singleWhere((r) => r.path == '/upgrade/stun' && r.method == 'POST');
    expect(jsonDecode(post.body), {'enabled': true});
    expect(state.stun.enabled, isTrue);
    // With the echo on there is nothing left to ask the user about.
    expect(state.shouldAskForPublicAddress, isFalse);
  });

  test('editing the server list leaves the switch alone', () async {
    final backend = FakeBackend();
    backend.routes['POST /upgrade/stun'] = {
      'ok': true,
      'enabled': false,
      'servers': ['stun.example.org:3478'],
    };
    final state = _state(backend);
    addTearDown(state.dispose);

    expect(await state.setStun(servers: ['stun.example.org:3478']), isTrue);

    final post = backend.requests
        .singleWhere((r) => r.path == '/upgrade/stun' && r.method == 'POST');
    expect(jsonDecode(post.body), {
      'servers': ['stun.example.org:3478']
    });
    expect(state.stun.servers, ['stun.example.org:3478']);
  });

  test('a backend with no echo endpoint reads as off rather than failing',
      () async {
    final state = _state(FakeBackend());
    addTearDown(state.dispose);

    await state.loadStun();

    expect(state.stun.enabled, isFalse);
    expect(state.error, isNull);
  });

  test('a refused try reports the backend reason in plain words', () async {
    final backend = FakeBackend();
    backend.routes['POST /upgrade/try/$_bob'] = {
      'ok': false,
      'reason': 'ineligible',
    };
    final state = _state(backend);
    addTearDown(state.dispose);

    expect(await state.tryDirectSession(_bob), isFalse);
    expect(state.takeActionError(), contains('Not eligible'));
  });
}
