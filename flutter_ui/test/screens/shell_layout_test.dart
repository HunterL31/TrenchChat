// Verifies the three-column shell lays out at the widths the mockup (1b)
// specifies: 60px server rail, 206px channel column, remainder to content.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/screens/main_window/channel_column.dart';
import 'package:flutter_ui/screens/main_window/server_rail.dart';

void main() {
  testWidgets('server rail is exactly 60px wide', (tester) async {
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: Row(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              ServerRail(
                servers: const [ServerRailEntry(hash: 'a', name: 'Mesh Crew')],
                selectedHash: 'a',
                onSelect: (_) {},
              ),
            ],
          ),
        ),
      ),
    );

    final size = tester.getSize(find.byType(ServerRail));
    expect(size.width, 60);
  });

  testWidgets('channel column is exactly 206px wide', (tester) async {
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: Row(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              ChannelColumn(
                serverName: 'mesh-crew',
                serverMemberCount: 12,
                channels: const [],
                directChannels: const [],
                selectedChannelHash: null,
                onSelectChannel: (_) {},
                onlinePresence: const [],
              ),
            ],
          ),
        ),
      ),
    );

    final size = tester.getSize(find.byType(ChannelColumn));
    expect(size.width, 206);
  });

  testWidgets('rail + column + content fill the window left to right', (tester) async {
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: Row(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              ServerRail(
                servers: const [ServerRailEntry(hash: 'a', name: 'Mesh Crew')],
                selectedHash: 'a',
                onSelect: (_) {},
              ),
              ChannelColumn(
                serverName: 'mesh-crew',
                serverMemberCount: 12,
                channels: const [],
                directChannels: const [],
                selectedChannelHash: null,
                onSelectChannel: (_) {},
                onlinePresence: const [],
              ),
              const Expanded(child: SizedBox()),
            ],
          ),
        ),
      ),
    );

    final railRight = tester.getTopRight(find.byType(ServerRail)).dx;
    final columnLeft = tester.getTopLeft(find.byType(ChannelColumn)).dx;
    final columnRight = tester.getTopRight(find.byType(ChannelColumn)).dx;

    expect(railRight, 60);
    expect(columnLeft, 60);
    expect(columnRight, 60 + 206);
  });
}
