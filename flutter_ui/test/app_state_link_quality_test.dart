// The header's link reading has to follow the mesh, not the last channel
// open: a topology change, a roster change, or a minute of nothing all re-read
// it. It is the one part of a channel refetched on its own, because reading
// the path table is local and free while messages and members are not.
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/events.dart';
import 'package:flutter_ui/app_state.dart';

import 'fake_backend.dart';

const _hash = 'chan-1';
const _other = 'chan-2';

Map<String, Object?> _quality({int reachable = 1, int total = 2}) => {
      'summary': {
        'level': 3,
        'level_label': 'Good',
        'reachable': reachable,
        'total': total,
        'median_hops': 2,
        'best_identity_hash': 'aa',
        'best_hops': 1,
      },
      'peers': [
        {'identity_hash': 'aa', 'display_name': 'ada', 'quality': 3,
         'quality_label': 'Good', 'hops': 2, 'via': null, 'rtt_ms': null,
         'path_expires_in': 300.0, 'is_online': true, 'last_seen': 1.0},
      ],
    };

/// Everything loadChannel reads, so opening a channel adds no 404s of its own.
void _seedChannelReads(FakeBackend backend, String hash) {
  backend.routes['GET /channels/$hash/members'] = <Object>[];
  backend.routes['GET /channels/$hash/messages'] = <Object>[];
  backend.routes['GET /channels/$hash/presence'] = <Object>[];
  backend.routes['GET /channels/$hash/link_quality'] = _quality();
  backend.routes['GET /channels/$hash/my_permissions'] = {'invite': false};
  backend.routes['GET /channels/$hash/voice/roster'] = <Object>[];
  backend.routes['GET /channels/$hash/sync_status'] = {'state': 'synced'};
}

int _hits(FakeBackend backend, [String hash = _hash]) => backend.requests
    .where((r) => r.path == '/channels/$hash/link_quality')
    .length;

void main() {
  late FakeBackend backend;
  late AppState state;

  setUp(() {
    backend = FakeBackend();
    state = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
    backend.routes['GET /channels/$_hash/link_quality'] = _quality();
  });

  tearDown(() => state.dispose());

  test('a network map change re-reads the open channel and nothing else',
      () async {
    state.selectedChannelHash = _hash;

    state.applyEvent(const NetworkMapChangedEvent());
    await Future<void>.delayed(const Duration(milliseconds: 20));

    expect(_hits(backend), 1);
    expect(state.linkQualityByChannel[_hash]!.reachable, 1);
    expect(state.linkQualityByChannel[_hash]!.total, 2);
    // Messages and members stay where they are: a moved path changes neither,
    // and refetching them on every announce is exactly the traffic this
    // indicator is meant to describe rather than cause.
    expect(backend.requests.any((r) => r.path.endsWith('/messages')), isFalse);
    expect(backend.requests.any((r) => r.path.endsWith('/members')), isFalse);
  });

  test('a network map change with no channel open reads nothing', () async {
    state.applyEvent(const NetworkMapChangedEvent());
    await Future<void>.delayed(const Duration(milliseconds: 20));

    expect(_hits(backend), 0);
  });

  test('a network map change while a conversation is open reads nothing',
      () async {
    // A direct conversation has two ends and no roster, so it has no reach to
    // report; its channels row exists only to hang messages off.
    state.selectedChannelHash = 'dm-hash';
    state.selectedDmHash = 'dm-hash';

    state.applyEvent(const NetworkMapChangedEvent());
    await Future<void>.delayed(const Duration(milliseconds: 20));

    expect(backend.requests, isEmpty);
  });

  test('a member list update re-reads the open channel only', () async {
    state.selectedChannelHash = _hash;
    backend.routes['GET /channels/$_other/link_quality'] = _quality();

    state.applyEvent(const MemberListUpdatedEvent(_other));
    state.applyEvent(const MemberListUpdatedEvent(_hash));
    await Future<void>.delayed(const Duration(milliseconds: 20));

    expect(_hits(backend, _other), 0);
    expect(_hits(backend), 1);
  });

  test('events arriving mid-fetch collapse into one more read', () async {
    state.selectedChannelHash = _hash;

    // Three events in a row, the first still in flight when the rest land.
    state.applyEvent(const NetworkMapChangedEvent());
    state.applyEvent(const NetworkMapChangedEvent());
    state.applyEvent(const NetworkMapChangedEvent());
    await Future<void>.delayed(const Duration(milliseconds: 20));

    expect(_hits(backend), 2, reason: 'one in flight, one for everything after');
  });

  testWidgets('an open channel is re-read on a timer, and stops when closed',
      (tester) async {
    _seedChannelReads(backend, _hash);
    state.selectedChannelHash = _hash;
    await state.loadChannel(_hash);
    expect(_hits(backend), 1, reason: 'the read loadChannel itself does');

    await tester.pump(const Duration(seconds: 61));
    await tester.pump();
    expect(_hits(backend), 2, reason: 'path entries expire with no event');

    // Closing the channel takes the timer with it rather than polling a
    // backend for a reading nothing is showing.
    state.selectedChannelHash = null;
    await tester.pump(const Duration(seconds: 61));
    await tester.pump(const Duration(seconds: 61));
    expect(_hits(backend), 2);
  });
}
