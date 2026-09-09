// AppState's RRC half: reading the surface, the session lifecycle, and the
// events that carry a room's lines. Nothing here is stored anywhere, so the
// tests hold the state to exactly what a session produced and to it going
// when the session does.
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/events.dart';
import 'package:flutter_ui/api/models/rrc.dart';
import 'package:flutter_ui/app_state.dart';

import 'fake_backend.dart';

const _hub = 'aabbccddeeff00112233445566778899';
const _other = '00112233445566778899aabbccddeeff';

Map<String, dynamic> _state({
  String? hub,
  String sessionState = 'idle',
  Map<String, dynamic> rooms = const {},
  List<Map<String, dynamic>> hubs = const [],
  List<String> bookmarks = const [],
  Map<String, dynamic> rosters = const {},
}) =>
    {
      'session': {
        'hub': hub,
        'state': sessionState,
        'name': 'testhub',
        'version': '1',
        'rooms': rooms,
        'limits': {'max_msg_body_bytes': 312},
      },
      'hubs': hubs,
      'nickname': 'nick',
      'bookmarks': bookmarks,
      'rosters': rosters,
    };

Map<String, dynamic> _line(String text, {String room = '#general'}) => {
      'room': room,
      'type': rrcTypeMsg,
      'source': _other,
      'nick': 'them',
      'text': text,
      'at': 100.0,
      'id': 'ab' * 4,
      'own': false,
    };

void main() {
  late FakeBackend backend;
  late AppState state;

  setUp(() {
    backend = FakeBackend();
    state = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
  });

  tearDown(() => state.dispose());

  group('reading the surface', () {
    test('refreshRrc fills hubs, session and limits', () async {
      backend.routes['GET /rrc'] = _state(
        hub: _hub,
        sessionState: 'active',
        rooms: {'#general': 'joined'},
        hubs: [
          {'hash': _hub, 'name': 'testhub', 'heard_at': 5.0,
           'bookmarked': true, 'connected': true},
        ],
        bookmarks: [_hub],
        rosters: {'#general': [_other]},
      );
      await state.refreshRrc();

      expect(state.rrcState.session.isActive, isTrue);
      expect(state.rrcState.session.maxMessageBytes, 312);
      expect(state.rrcState.hubs.single.bookmarked, isTrue);
      expect(state.rrcState.rosters['#general'], [_other]);
    });

    test('an empty surface is the idle session, not a crash', () async {
      backend.routes['GET /rrc'] = <String, dynamic>{};
      await state.refreshRrc();
      expect(state.rrcState.session.hub, isNull);
      expect(state.rrcState.session.state, 'idle');
    });
  });

  group('session lifecycle', () {
    test('connecting clears any transcript the last session left', () async {
      state.rrcLinesByRoom['#stale'] = [RRCLine.fromJson(_line('old'))];
      backend.routes['POST /rrc/connect'] = {'ok': true, 'session': {}};
      backend.routes['GET /rrc'] = _state(hub: _hub, sessionState: 'active');

      await state.connectRrcHub(_hub);

      expect(state.rrcLinesByRoom, isEmpty);
      expect(state.rrcState.session.hub, _hub);
    });

    test('a session going idle drops every transcript', () async {
      state.rrcLinesByRoom['#general'] = [RRCLine.fromJson(_line('gone'))];
      backend.routes['GET /rrc'] = _state();

      state.applyEvent(TcEvent.tryParse(jsonEncode({
        'type': 'rrc_session',
        'hub_hash': _hub,
        'state': 'idle',
        'reason': 'link closed',
      }))!);

      expect(state.rrcLinesByRoom, isEmpty);
      expect(state.rrcSessionReason, 'link closed');
      // The event kicks off a re-read; let it land before the state is torn
      // down, or the refresh notifies a disposed notifier.
      await Future<void>.delayed(Duration.zero);
    });

    test('parting a room forgets its lines', () async {
      state.rrcLinesByRoom['#general'] = [RRCLine.fromJson(_line('bye'))];
      backend.routes['POST /rrc/rooms/part'] = {'ok': true};
      backend.routes['GET /rrc'] = _state(hub: _hub, sessionState: 'active');

      await state.partRrcRoom('#general');

      expect(state.rrcLinesByRoom.containsKey('#general'), isFalse);
    });
  });

  group('events', () {
    test('an rrc_message appends to its room', () {
      state.applyEvent(TcEvent.tryParse(jsonEncode({
        'type': 'rrc_message',
        'room': '#general',
        'line': _line('hello'),
      }))!);
      state.applyEvent(TcEvent.tryParse(jsonEncode({
        'type': 'rrc_message',
        'room': '#general',
        'line': _line('again'),
      }))!);

      expect(state.rrcLinesByRoom['#general']!.map((l) => l.text),
          ['hello', 'again']);
    });

    test('an rrc_hub event upserts and keeps a name a later announce omits', () {
      state.applyEvent(TcEvent.tryParse(jsonEncode({
        'type': 'rrc_hub',
        'hub_hash': _hub,
        'name': 'named',
      }))!);
      state.applyEvent(TcEvent.tryParse(jsonEncode({
        'type': 'rrc_hub',
        'hub_hash': _hub,
        'name': '',
      }))!);

      expect(state.rrcState.hubs, hasLength(1));
      expect(state.rrcState.hubs.single.name, 'named');
    });
  });

  group('rrc:// links', () {
    test('a hub-only link parses to the hub', () {
      final link = parseRrcLink('rrc://$_hub');
      expect(link!.hubHash, _hub);
      expect(link.room, isNull);
    });

    test('a room is carried and given its hash', () {
      expect(parseRrcLink('rrc://$_hub/general')!.room, '#general');
      expect(parseRrcLink('rrc://$_hub/#general')!.room, '#general');
    });

    test('anything that is not a destination hash is not a link', () {
      // A link is an invitation to dial and hand a hub this identity, so what
      // it names has to be a destination before anything opens.
      expect(parseRrcLink('rrc://not-a-hash/general'), isNull);
      expect(parseRrcLink('rrc://'), isNull);
      expect(parseRrcLink('nnn@$_hub'), isNull);
      expect(parseRrcLink('https://example.com'), isNull);
    });

    test('a pending link is taken exactly once', () {
      state.openRrcLink(const RRCLink(_hub, '#general'));
      expect(state.takeRrcPendingLink()!.hubHash, _hub);
      expect(state.takeRrcPendingLink(), isNull);
    });
  });

  group('hosting', () {
    test('switching hosting on reports the hub others would dial', () async {
      backend.routes['POST /rrc/hosting'] = {
        'ok': true,
        'enabled': true,
        'hub_hash': _hub,
        'name': 'mine',
        'clients': 0,
        'rooms': <String, dynamic>{},
      };
      await state.setRrcHosting(enabled: true, hubName: 'mine');

      expect(state.rrcHosting.enabled, isTrue);
      expect(state.rrcHosting.hubHash, _hub);
    });
  });
}
