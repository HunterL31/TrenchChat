// Regression: with a server selected, the DIRECT CHANNELS section must stay
// reachable -- its + creates a standalone channel even when the main CHANNEL
// button is bound to the server, and the section shows even with no direct
// channels yet.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/models/server.dart';
import 'package:flutter_ui/screens/main_window/channel_column.dart';

Channel _channel(String name) => Channel.fromJson({
      'hash': 'hash-$name',
      'name': name,
      'description': '',
      'creator_hash': 'creator',
      'open_join': true,
      'created_at': 0,
    });

void main() {
  testWidgets('direct channel + fires its own callback, not the server one',
      (tester) async {
    var serverCreates = 0;
    var directCreates = 0;
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: ChannelColumn(
          serverName: 'mesh-crew',
          serverMemberCount: 3,
          channels: [_channel('general')],
          directChannels: const [],
          selectedChannelHash: null,
          onSelectChannel: (_) {},
          onCreateChannel: () => serverCreates++,
          onCreateDirectChannel: () => directCreates++,
          onJoinChannel: () {},
        ),
      ),
    ));

    // Section is present even with zero direct channels.
    expect(find.text('DIRECT CHANNELS'), findsOneWidget);

    await tester.tap(find.byTooltip('New direct channel'));
    expect(directCreates, 1);
    expect(serverCreates, 0);

    // The CHANNELS header's + creates in the server.
    await tester.tap(find.byTooltip('New channel'));
    expect(serverCreates, 1);
    expect(directCreates, 1);
  });

  testWidgets('the footer ADD menu consolidates every add action', (tester) async {
    var serverCreates = 0;
    var directCreates = 0;
    var joins = 0;
    var dmStarts = 0;
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: ChannelColumn(
          serverName: 'mesh-crew',
          serverMemberCount: 3,
          channels: [_channel('general')],
          directChannels: const [],
          selectedChannelHash: null,
          onSelectChannel: (_) {},
          onCreateChannel: () => serverCreates++,
          onCreateDirectChannel: () => directCreates++,
          onJoinChannel: () => joins++,
          onStartDm: () => dmStarts++,
        ),
      ),
    ));

    // The old stacked footer buttons are gone.
    expect(find.text('NEW CHANNEL'), findsNothing);
    expect(find.text('JOIN CHANNEL'), findsNothing);

    await tester.tap(find.text('ADD'));
    await tester.pumpAndSettle();
    expect(find.text('New channel in mesh-crew'), findsOneWidget);
    expect(find.text('New direct channel'), findsOneWidget);
    expect(find.text('Message a friend…'), findsOneWidget);

    await tester.tap(find.text('Join channel…'));
    await tester.pumpAndSettle();
    expect(joins, 1);
    expect(serverCreates, 0);
    expect(directCreates, 0);
    expect(dmStarts, 0);
  });

  testWidgets('without a server the ADD menu offers a plain new channel',
      (tester) async {
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: ChannelColumn(
          serverName: null,
          serverMemberCount: null,
          channels: const [],
          directChannels: const [],
          selectedChannelHash: null,
          onSelectChannel: (_) {},
          onCreateChannel: () {},
          onCreateDirectChannel: () {},
          onJoinChannel: () {},
        ),
      ),
    ));

    await tester.tap(find.text('ADD'));
    await tester.pumpAndSettle();
    expect(find.text('New channel'), findsOneWidget);
    // Redundant with "New channel" when there is no server to distinguish.
    expect(find.text('New direct channel'), findsNothing);
  });

  testWidgets('a channel with unread messages carries a count pill', (tester) async {
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: ChannelColumn(
          serverName: 'mesh-crew',
          serverMemberCount: 2,
          channels: [_channel('general'), _channel('ops')],
          directChannels: const [],
          selectedChannelHash: 'hash-general',
          onSelectChannel: (_) {},
          unreadCounts: const {'hash-ops': 4},
        ),
      ),
    ));

    expect(find.text('4'), findsOneWidget);
  });

  testWidgets('the selected channel never shows an unread pill', (tester) async {
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: ChannelColumn(
          serverName: 'mesh-crew',
          serverMemberCount: 2,
          channels: [_channel('general')],
          directChannels: const [],
          selectedChannelHash: 'hash-general',
          onSelectChannel: (_) {},
          unreadCounts: const {'hash-general': 3},
        ),
      ),
    ));

    expect(find.text('3'), findsNothing);
  });

  testWidgets('without the callback the empty section stays hidden', (tester) async {
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: ChannelColumn(
          serverName: null,
          serverMemberCount: null,
          channels: const [],
          directChannels: const [],
          selectedChannelHash: null,
          onSelectChannel: (_) {},
        ),
      ),
    ));

    expect(find.text('DIRECT CHANNELS'), findsNothing);
  });

  testWidgets('only a channel with an incomplete sync is flagged', (tester) async {
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: ChannelColumn(
          serverName: 'mesh-crew',
          serverMemberCount: 2,
          channels: [_channel('general'), _channel('ops')],
          directChannels: const [],
          selectedChannelHash: null,
          onSelectChannel: (_) {},
          syncStates: const {
            'hash-general': 'incomplete',
            'hash-ops': 'synced',
          },
        ),
      ),
    ));

    expect(find.text('INCOMPLETE'), findsOneWidget);
  });

  testWidgets('a fully synced column shows no indicator', (tester) async {
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: ChannelColumn(
          serverName: 'mesh-crew',
          serverMemberCount: 2,
          channels: [_channel('general')],
          directChannels: const [],
          selectedChannelHash: null,
          onSelectChannel: (_) {},
          syncStates: const {'hash-general': 'synced'},
        ),
      ),
    ));

    expect(find.text('INCOMPLETE'), findsNothing);
  });
}
