// The PUBLIC tab: what it shows before a session exists, what connecting to
// a hub asks first, and that a line reaches the room the user is looking at.
//
// The confirmation before connecting is the one gate that matters here: a hub
// learns this node's identity and everything it does in the session, which no
// TrenchChat channel does, so nothing may connect without being asked.
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/events.dart';
import 'package:flutter_ui/api/models/rrc.dart';
import 'package:flutter_ui/app_state.dart';
import 'package:flutter_ui/screens/main_window/public_tab.dart';

import '../fake_backend.dart';

const _hub = 'aabbccddeeff00112233445566778899';
const _peer = '00112233445566778899aabbccddeeff';

Widget _harness(AppState state) =>
    MaterialApp(home: Scaffold(body: PublicTab(state: state)));

Map<String, dynamic> _surface({
  String? hub,
  String sessionState = 'idle',
  Map<String, dynamic> rooms = const {},
  bool heardHub = true,
}) =>
    {
      'session': {
        'hub': hub,
        'state': sessionState,
        'name': 'testhub',
        'version': '1',
        'rooms': rooms,
        'limits': <String, dynamic>{},
      },
      'hubs': heardHub
          ? [
              {'hash': _hub, 'name': 'testhub', 'heard_at': 5.0,
               'bookmarked': false, 'connected': hub == _hub},
            ]
          : <Map<String, dynamic>>[],
      'nickname': '',
      'bookmarks': <String>[],
      'rosters': rooms.isEmpty ? <String, dynamic>{} : {rooms.keys.first: [_peer]},
    };

void main() {
  late FakeBackend backend;
  late AppState state;

  setUp(() {
    backend = FakeBackend();
    backend.routes['GET /rrc/hosting'] = {
      'enabled': false,
      'hub_hash': '',
      'name': '',
      'clients': 0,
      'rooms': <String, dynamic>{},
    };
  });

  tearDown(() => state.dispose());

  AppState build() =>
      state = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());

  testWidgets('with no hub heard, it says hubs are heard and not looked up',
      (tester) async {
    backend.routes['GET /rrc'] = _surface(heardHub: false);
    await tester.pumpWidget(_harness(build()));
    await settle(tester);

    expect(find.byKey(const Key('rrc-no-hubs')), findsOneWidget);
    expect(find.byKey(const Key('rrc-empty')), findsOneWidget);
  });

  testWidgets('connecting is refused until the identity warning is accepted',
      (tester) async {
    backend.routes['GET /rrc'] = _surface();
    backend.routes['POST /rrc/connect'] = {'ok': true, 'session': {}};
    await tester.pumpWidget(_harness(build()));
    await settle(tester);

    await tester.tap(find.byKey(const Key('rrc-connect-$_hub')));
    await settle(tester);
    expect(find.text('CANCEL'), findsOneWidget);

    await tester.tap(find.text('CANCEL'));
    await settle(tester);
    expect(
      backend.requests.where((r) => r.path == '/rrc/connect'),
      isEmpty,
      reason: 'a hub must never be dialled without being asked',
    );
  });

  testWidgets('accepting the warning connects', (tester) async {
    backend.routes['GET /rrc'] = _surface();
    backend.routes['POST /rrc/connect'] = {'ok': true, 'session': {}};
    await tester.pumpWidget(_harness(build()));
    await settle(tester);

    await tester.tap(find.byKey(const Key('rrc-connect-$_hub')));
    await settle(tester);
    await tester.tap(find.text('CONNECT'));
    await settle(tester);

    expect(backend.requests.where((r) => r.path == '/rrc/connect'), hasLength(1));
  });

  testWidgets('an active session lists its rooms and their transcripts',
      (tester) async {
    backend.routes['GET /rrc'] = _surface(
        hub: _hub, sessionState: 'active', rooms: {'#general': 'joined'});
    await tester.pumpWidget(_harness(build()));
    await settle(tester);

    state.applyEvent(TcEvent.tryParse(jsonEncode({
      'type': 'rrc_message',
      'room': '#general',
      'line': {
        'room': '#general',
        'type': rrcTypeMsg,
        'source': _peer,
        'nick': 'them',
        'text': 'hello room',
        'at': 100.0,
        'id': '',
        'own': false,
      },
    }))!);
    await settle(tester);

    expect(find.byKey(const Key('rrc-room-#general')), findsOneWidget);
    expect(find.textContaining('hello room'), findsOneWidget);
    expect(find.textContaining('1 present'), findsOneWidget);
  });

  testWidgets('an action renders as an emote, not as speech', (tester) async {
    backend.routes['GET /rrc'] = _surface(
        hub: _hub, sessionState: 'active', rooms: {'#general': 'joined'});
    await tester.pumpWidget(_harness(build()));
    await settle(tester);

    state.applyEvent(TcEvent.tryParse(jsonEncode({
      'type': 'rrc_message',
      'room': '#general',
      'line': {
        'room': '#general',
        'type': rrcTypeAction,
        'source': _peer,
        'nick': 'them',
        'text': 'waves',
        'at': 100.0,
        'id': '',
        'own': false,
      },
    }))!);
    await settle(tester);

    expect(find.text('* them waves'), findsOneWidget);
  });

  testWidgets('a line with no nick falls back to the identity hash',
      (tester) async {
    backend.routes['GET /rrc'] = _surface(
        hub: _hub, sessionState: 'active', rooms: {'#general': 'joined'});
    await tester.pumpWidget(_harness(build()));
    await settle(tester);

    state.applyEvent(TcEvent.tryParse(jsonEncode({
      'type': 'rrc_message',
      'room': '#general',
      'line': {
        'room': '#general',
        'type': rrcTypeMsg,
        'source': _peer,
        'nick': '',
        'text': 'anonymous',
        'at': 100.0,
        'id': '',
        'own': false,
      },
    }))!);
    await settle(tester);

    expect(find.text('<${_peer.substring(0, 12)}> anonymous'), findsOneWidget);
  });

  testWidgets('sending posts to the room on screen', (tester) async {
    backend.routes['GET /rrc'] = _surface(
        hub: _hub, sessionState: 'active', rooms: {'#general': 'joined'});
    backend.routes['POST /rrc/rooms/general/messages'] = {'ok': true};
    await tester.pumpWidget(_harness(build()));
    await settle(tester);

    await tester.enterText(find.byKey(const Key('rrc-compose')), 'from me');
    await tester.tap(find.byKey(const Key('rrc-send')));
    await settle(tester);

    final sent = backend.requests
        .where((r) => r.path == '/rrc/rooms/general/messages')
        .toList();
    expect(sent, hasLength(1));
    expect(jsonDecode(sent.single.body)['text'], 'from me');
  });

  testWidgets('joining a room asks the backend and selects it', (tester) async {
    backend.routes['GET /rrc'] = _surface(
        hub: _hub, sessionState: 'active', rooms: {'#general': 'joined'});
    backend.routes['POST /rrc/rooms'] = {'ok': true};
    backend.routes['GET /rrc/rooms/other/messages'] = <Map<String, dynamic>>[];
    await tester.pumpWidget(_harness(build()));
    await settle(tester);

    await tester.enterText(find.byKey(const Key('rrc-room-field')), '#other');
    await tester.tap(find.byKey(const Key('rrc-join-room')));
    await settle(tester);

    final joins = backend.requests.where((r) => r.path == '/rrc/rooms').toList();
    expect(joins, hasLength(1));
    expect(jsonDecode(joins.single.body)['room'], '#other');
  });

  testWidgets('an rrc:// link opens its hub and room once', (tester) async {
    backend.routes['GET /rrc'] = _surface(
        hub: _hub, sessionState: 'active', rooms: {'#general': 'joined'});
    backend.routes['POST /rrc/connect'] = {'ok': true, 'session': {}};
    backend.routes['POST /rrc/rooms'] = {'ok': true};
    backend.routes['GET /rrc/rooms/general/messages'] = <Map<String, dynamic>>[];
    build();
    // Already connected to this hub, so the link only has the room to join
    // and no second identity warning is owed.
    state.openRrcLink(const RRCLink(_hub, '#general'));

    await tester.pumpWidget(_harness(state));
    await settle(tester);

    expect(backend.requests.where((r) => r.path == '/rrc/rooms'), hasLength(1));
    expect(state.rrcPendingLink, isNull);
  });

  testWidgets('hosting is off until switched on, and then names its hub',
      (tester) async {
    backend.routes['GET /rrc'] = _surface();
    backend.routes['POST /rrc/hosting'] = {
      'ok': true,
      'enabled': true,
      'hub_hash': _hub,
      'name': 'mine',
      'clients': 0,
      'rooms': <String, dynamic>{},
    };
    await tester.pumpWidget(_harness(build()));
    await settle(tester);
    expect(find.text('Not hosting a hub'), findsOneWidget);

    await tester.tap(find.byKey(const Key('rrc-hosting-toggle')));
    await settle(tester);
    await tester.enterText(find.byKey(const Key('rrc-hosting-name')), 'mine');
    await tester.tap(find.byKey(const Key('rrc-hosting-confirm')));
    await settle(tester);

    expect(find.byKey(const Key('rrc-hosting-hash')), findsOneWidget);
  });
}
