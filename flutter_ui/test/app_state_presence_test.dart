// A presence event must move every surface that shows the peer's dot: the
// channel presence list, the friends list, and the DM sidebar all read the
// same backend presence, so none of them may keep a stale snapshot.
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/events.dart';
import 'package:flutter_ui/api/models/dm.dart';
import 'package:flutter_ui/api/models/friend.dart';
import 'package:flutter_ui/app_state.dart';

import 'fake_backend.dart';

const _peer = 'aa11bb22cc33dd44ee55ff6600112233';

void main() {
  test('a presence event flips the friends list and the DM sidebar together',
      () {
    final backend = FakeBackend();
    final state =
        AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
    addTearDown(state.dispose);

    state.friends = [
      Friend.fromJson(const {
        'identity_hash': _peer,
        'display_name': 'cryptorekt',
        'added_at': 100.0,
        'last_seen_at': 200.0,
        'is_online': false,
        'state': 'pending_out',
        'nomad_node_hash': 'dddddddddddddddddddddddddddddddd',
      }),
    ];
    state.dms = [
      DmConversation.fromJson(const {
        'hash': 'dm-1',
        'peer_hash': _peer,
        'display_name': 'cryptorekt',
        'is_online': false,
      }),
    ];

    state.applyEvent(const PresenceEvent(_peer, true));
    expect(state.friends.single.isOnline, isTrue);
    expect(state.dms.single.isOnline, isTrue);
    // The rebuild must not shed the fields presence does not carry.
    expect(state.friends.single.state, 'pending_out');
    expect(state.friends.single.nomadNodeHash, 'dddddddddddddddddddddddddddddddd');
    expect(state.dms.single.hash, 'dm-1');

    state.applyEvent(const PresenceEvent(_peer, false));
    expect(state.friends.single.isOnline, isFalse);
    expect(state.dms.single.isOnline, isFalse);
  });

  test('a presence event for an unknown peer changes nothing', () {
    final backend = FakeBackend();
    final state =
        AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
    addTearDown(state.dispose);

    state.dms = [
      DmConversation.fromJson(const {
        'hash': 'dm-1',
        'peer_hash': _peer,
        'display_name': 'cryptorekt',
        'is_online': true,
      }),
    ];

    state.applyEvent(const PresenceEvent('ffffffffffffffffffffffffffffffff', false));
    expect(state.dms.single.isOnline, isTrue);
  });
}
