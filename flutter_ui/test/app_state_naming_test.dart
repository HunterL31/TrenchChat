// Live naming/avatar propagation: avatar_updated must bust the avatar cache
// (re-fetching with a cache-buster, or dropping to the fallback on removal),
// and directory_updated must patch a peer's cached name in the directory and
// in any saved friend record without a reload.
import 'dart:convert';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:flutter_ui/api/events.dart';
import 'package:flutter_ui/api/models/friend.dart';
import 'package:flutter_ui/api/models/invite.dart';
import 'package:flutter_ui/app_state.dart';

import 'fake_backend.dart';

const _peer = 'f3a1c2d4e5b6a798f3a1c2d4e5b6a798';

Friend _friend(String displayName) => Friend(
      identityHash: _peer,
      nickname: '',
      note: '',
      displayName: displayName,
      addedAt: 0,
      lastSeenAt: 0,
      isOnline: false,
    );

void main() {
  test('avatar_updated re-fetches with the version as a cache-buster', () async {
    String? capturedVersion;
    final client = MockClient((req) async {
      if (req.url.path == '/peers/$_peer/avatar') {
        capturedVersion = req.url.queryParameters['v'];
        return http.Response(
          jsonEncode({
            'avatar_data_b64': base64Encode(const [9, 9, 9]),
            'avatar_version': 7,
          }),
          200,
          headers: {'content-type': 'application/json'},
        );
      }
      return http.Response(jsonEncode({}), 404,
          headers: {'content-type': 'application/json'});
    });
    final state = AppState(baseUrl: 'http://fake.test', httpClient: client);
    addTearDown(state.dispose);

    // A stale cached image the change must replace.
    state.avatarCache[_peer] = Uint8List.fromList(const [1, 2, 3]);

    state.applyEvent(const AvatarUpdatedEvent(_peer, 7));
    await Future<void>.delayed(const Duration(milliseconds: 20));

    expect(capturedVersion, '7');
    expect(state.avatarCache[_peer], const [9, 9, 9]);
  });

  test('avatar_updated with a null version drops the cached avatar', () async {
    final backend = FakeBackend();
    final state = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
    addTearDown(state.dispose);
    state.avatarCache[_peer] = Uint8List.fromList(const [1, 2, 3]);

    state.applyEvent(const AvatarUpdatedEvent(_peer, null));
    await Future<void>.delayed(const Duration(milliseconds: 10));

    // Present but null: the fallback (initials) shows and nothing re-fetches.
    expect(state.avatarCache.containsKey(_peer), isTrue);
    expect(state.avatarCache[_peer], isNull);
    expect(
      backend.requests.where((r) => r.path == '/peers/$_peer/avatar'),
      isEmpty,
    );
  });

  test('directory_updated patches a friend name live', () async {
    final backend = FakeBackend();
    final state = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
    addTearDown(state.dispose);
    state.friends = [_friend('Alice')];

    state.applyEvent(const DirectoryUpdatedEvent(_peer, 'Alicia'));

    expect(state.friends.single.displayName, 'Alicia');
    // No refetch needed -- the entry is patched in place.
    expect(backend.requests.where((r) => r.path == '/friends'), isEmpty);
  });

  test('directory_updated patches the cached directory entry live', () async {
    final backend = FakeBackend();
    final state = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
    addTearDown(state.dispose);
    state.directory = [
      const DirectoryEntry(identityHash: _peer, displayName: 'Alice', isOnline: true),
    ];

    state.applyEvent(const DirectoryUpdatedEvent(_peer, 'Alicia'));

    expect(state.directory.single.displayName, 'Alicia');
    expect(state.directory.single.isOnline, isTrue);
  });
}
