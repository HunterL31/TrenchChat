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
          onlinePresence: const [],
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

    await tester.tap(find.text('CHANNEL'));
    expect(serverCreates, 1);
    expect(directCreates, 1);
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
          onlinePresence: const [],
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
          onlinePresence: const [],
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
          onlinePresence: const [],
          syncStates: const {'hash-general': 'synced'},
        ),
      ),
    ));

    expect(find.text('INCOMPLETE'), findsNothing);
  });
}
