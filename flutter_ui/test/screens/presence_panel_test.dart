// The right-hand presence panel: the channel's roster, online first, self
// filtered out, offline peers dimmed into their own section -- and, unlike
// the pinned sidebar section it replaced, scrollable on its own so a busy
// channel can never squeeze the channel/DM list.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/models/member.dart';
import 'package:flutter_ui/screens/main_window/presence_panel.dart';

const _meHash = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';

Widget _harness(List<PresenceEntry> presence,
        {Set<String> friendHashes = const {},
        void Function(String)? onAddFriend}) =>
    MaterialApp(
      home: Scaffold(
        body: Row(children: [
          PresencePanel(
            presence: presence,
            meHashHex: _meHash,
            friendHashes: friendHashes,
            onAddFriend: onAddFriend,
          ),
        ]),
      ),
    );

void main() {
  testWidgets('shows display names, not raw hashes, and hides self', (tester) async {
    await tester.pumpWidget(_harness(const [
      PresenceEntry(identityHash: _meHash, isOnline: true, displayName: 'me'),
      PresenceEntry(
          identityHash: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
          isOnline: true,
          displayName: 'Alice'),
      PresenceEntry(identityHash: 'cccccccccccccccccccccccccccccccc', isOnline: true),
    ]));

    // The local user is filtered out; two peers remain.
    expect(find.text('ONLINE — 2'), findsOneWidget);
    expect(find.text('me'), findsNothing);
    // A named peer renders its name; an unnamed one falls back to a short hash.
    expect(find.text('Alice'), findsOneWidget);
    expect(find.text('cccc…cccc'), findsOneWidget);
  });

  testWidgets('offline peers get their own section; none means no section',
      (tester) async {
    await tester.pumpWidget(_harness(const [
      PresenceEntry(
          identityHash: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
          isOnline: true,
          displayName: 'Alice'),
      PresenceEntry(
          identityHash: 'cccccccccccccccccccccccccccccccc',
          isOnline: false,
          displayName: 'Bob'),
    ]));

    expect(find.text('ONLINE — 1'), findsOneWidget);
    expect(find.text('OFFLINE — 1'), findsOneWidget);
    expect(find.text('Bob'), findsOneWidget);

    await tester.pumpWidget(_harness(const [
      PresenceEntry(
          identityHash: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
          isOnline: true,
          displayName: 'Alice'),
    ]));
    expect(find.textContaining('OFFLINE'), findsNothing);
  });

  testWidgets('a long roster scrolls inside the panel', (tester) async {
    await tester.pumpWidget(_harness([
      for (var i = 0; i < 60; i++)
        PresenceEntry(
            identityHash: i.toString().padLeft(2, '0') * 16,
            isOnline: true,
            displayName: 'peer$i'),
    ]));

    expect(find.text('peer0'), findsOneWidget);
    expect(find.text('peer59'), findsNothing);
    await tester.drag(find.byType(ListView), const Offset(0, -3000));
    await tester.pump();
    expect(find.text('peer59'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });
}
