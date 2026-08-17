// Right-click "Add/Edit friend…" is the only place ChannelColumn and
// MessageList reach outside their own plain-value + callback contract, so
// it gets its own coverage: both leaves must fire onAddFriend with the
// tapped row's identity hash, and switch label when that hash is already
// a friend.
import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/models/member.dart';
import 'package:flutter_ui/api/models/message.dart';
import 'package:flutter_ui/screens/main_window/channel_column.dart';
import 'package:flutter_ui/screens/main_window/message_list.dart';

const String kAliceHash = 'f3a1c2d4e5b6a798f3a1c2d4e5b6a798';

Future<void> _rightClick(WidgetTester tester, Finder finder) async {
  final gesture = await tester.startGesture(
    tester.getCenter(finder),
    kind: PointerDeviceKind.mouse,
    buttons: kSecondaryButton,
  );
  await gesture.up();
  await tester.pump();
}

void main() {
  testWidgets('right-clicking an online roster row offers Add friend and fires onAddFriend',
      (tester) async {
    String? fired;
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: ChannelColumn(
          serverName: 'mesh-crew',
          serverMemberCount: 1,
          channels: const [],
          directChannels: const [],
          selectedChannelHash: null,
          onSelectChannel: (_) {},
          onlinePresence: const [PresenceEntry(identityHash: kAliceHash, isOnline: true)],
          onAddFriend: (hash) => fired = hash,
        ),
      ),
    ));
    await tester.pump();

    await _rightClick(tester, find.textContaining('…').first);
    await tester.pump();

    expect(find.text('Add friend…'), findsOneWidget);
    await tester.tap(find.text('Add friend…'));
    await tester.pump();

    expect(fired, kAliceHash);
  });

  testWidgets('an already-saved hash offers Edit friend instead of Add friend', (tester) async {
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: ChannelColumn(
          serverName: 'mesh-crew',
          serverMemberCount: 1,
          channels: const [],
          directChannels: const [],
          selectedChannelHash: null,
          onSelectChannel: (_) {},
          onlinePresence: const [PresenceEntry(identityHash: kAliceHash, isOnline: true)],
          friendHashes: const {kAliceHash},
          onAddFriend: (_) {},
        ),
      ),
    ));
    await tester.pump();

    await _rightClick(tester, find.textContaining('…').first);
    await tester.pump();

    expect(find.text('Edit friend…'), findsOneWidget);
    expect(find.text('Add friend…'), findsNothing);
  });

  testWidgets('right-clicking a message row fires onAddFriend with the sender hash',
      (tester) async {
    String? fired;
    final message = Message(
      messageId: 'm1',
      senderHash: kAliceHash,
      senderName: 'alice',
      content: 'hello',
      timestamp: 1_700_000_000,
      replyTo: null,
      hasImage: false,
      reactions: const [],
    );

    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: SizedBox(
          width: 800,
          height: 600,
          child: MessageList(
            messages: [message],
            meHashHex: 'me',
            displayNameFor: (hash, fallback) => fallback,
            onAddFriend: (hash) => fired = hash,
          ),
        ),
      ),
    ));
    await tester.pumpAndSettle();

    await _rightClick(tester, find.text('hello'));
    await tester.pump();

    expect(find.text('Add friend…'), findsOneWidget);
    await tester.tap(find.text('Add friend…'));
    await tester.pump();

    expect(fired, kAliceHash);
  });
}
