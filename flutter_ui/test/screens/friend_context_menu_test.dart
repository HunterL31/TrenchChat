// Right-click "Add/Edit friend…" is the only place ChannelColumn and
// MessageList reach outside their own plain-value + callback contract, so
// it gets its own coverage: both leaves must fire onAddFriend with the
// tapped row's identity hash, and switch label when that hash is already
// a friend. Long-press must reach the same menu -- a touch device has no
// secondary tap, so it is the only path to these actions there.
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

Future<void> _longPress(WidgetTester tester, Finder finder) async {
  await tester.longPress(finder);
  await tester.pump();
}

Message _message() => const Message(
      messageId: 'm1',
      senderHash: kAliceHash,
      senderName: 'alice',
      content: 'hello',
      timestamp: 1_700_000_000,
      replyTo: null,
      hasImage: false,
      reactions: [],
    );

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
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: SizedBox(
          width: 800,
          height: 600,
          child: MessageList(
            messages: [_message()],
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

  testWidgets('long-pressing an online roster row opens the same menu', (tester) async {
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

    await _longPress(tester, find.textContaining('…').first);

    expect(find.text('Add friend…'), findsOneWidget);
    await tester.tap(find.text('Add friend…'));
    await tester.pump();

    expect(fired, kAliceHash);
  });

  testWidgets('long-pressing a message row opens the same menu', (tester) async {
    String? fired;
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: SizedBox(
          width: 800,
          height: 600,
          child: MessageList(
            messages: [_message()],
            meHashHex: 'me',
            displayNameFor: (hash, fallback) => fallback,
            onAddFriend: (hash) => fired = hash,
          ),
        ),
      ),
    ));
    await tester.pumpAndSettle();

    await _longPress(tester, find.text('hello'));

    expect(find.text('Add friend…'), findsOneWidget);
    await tester.tap(find.text('Add friend…'));
    await tester.pump();

    expect(fired, kAliceHash);
  });

  testWidgets('a message row offers React… so touch users can reach the hover button',
      (tester) async {
    String? reacted;
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: SizedBox(
          width: 800,
          height: 600,
          child: MessageList(
            messages: [_message()],
            meHashHex: 'me',
            displayNameFor: (hash, fallback) => fallback,
            onReact: (messageId) => reacted = messageId,
          ),
        ),
      ),
    ));
    await tester.pumpAndSettle();

    await _longPress(tester, find.text('hello'));

    expect(find.text('React…'), findsOneWidget);
    await tester.tap(find.text('React…'));
    await tester.pump();

    expect(reacted, 'm1');
  });

  testWidgets('your own message offers no Add friend, since you cannot befriend yourself',
      (tester) async {
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: SizedBox(
          width: 800,
          height: 600,
          child: MessageList(
            messages: [_message()],
            meHashHex: kAliceHash, // the sender is the local identity
            displayNameFor: (hash, fallback) => fallback,
            onAddFriend: (_) {},
            onReact: (_) {},
          ),
        ),
      ),
    ));
    await tester.pumpAndSettle();

    await _rightClick(tester, find.text('hello'));
    await tester.pump();

    expect(find.text('React…'), findsOneWidget); // the menu did open
    expect(find.text('Add friend…'), findsNothing);
    expect(find.text('Edit friend…'), findsNothing);
  });
}
