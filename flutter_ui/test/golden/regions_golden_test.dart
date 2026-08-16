// Per-region goldens so a regression is localizable to one panel instead of
// only showing up as a diff on the full-window golden.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/models/link_quality.dart';
import 'package:flutter_ui/screens/main_window/channel_column.dart';
import 'package:flutter_ui/screens/main_window/channel_header.dart';
import 'package:flutter_ui/screens/main_window/compose_bar.dart';
import 'package:flutter_ui/screens/main_window/message_list.dart';
import 'package:flutter_ui/screens/main_window/server_rail.dart';
import 'package:flutter_ui/theme/app_theme.dart';
import 'package:flutter_ui/theme/tokens.dart';

import 'fixtures.dart';
import 'test_fonts.dart';

Widget _harness(Widget child, {double? width, double? height, Color? background}) {
  return MaterialApp(
    theme: buildAppTheme(),
    debugShowCheckedModeBanner: false,
    home: Scaffold(
      backgroundColor: background ?? TCColors.bgApp,
      body: SizedBox(width: width, height: height, child: child),
    ),
  );
}

void main() {
  setUpAll(loadTestFonts);

  testWidgets('server_rail', (tester) async {
    await tester.pumpWidget(_harness(
      ServerRail(
        servers: fixtureRailServers(),
        selectedHash: kServerHash,
        onSelect: (_) {},
      ),
      height: 900,
    ));
    await tester.pump();
    await expectLater(find.byType(ServerRail), matchesGoldenFile('goldens/server_rail.png'));
  });

  testWidgets('channel_column', (tester) async {
    await tester.pumpWidget(_harness(
      ChannelColumn(
        serverName: 'mesh-crew',
        serverMemberCount: 12,
        channels: fixtureServerChannels(),
        directChannels: fixtureDirectChannels(),
        selectedChannelHash: kGeneralHash,
        onSelectChannel: (_) {},
        onlinePresence: fixturePresence(),
      ),
      height: 900,
    ));
    await tester.pump();
    await expectLater(find.byType(ChannelColumn), matchesGoldenFile('goldens/channel_column.png'));
  });

  testWidgets('channel_header', (tester) async {
    await tester.pumpWidget(_harness(
      ChannelHeader(
        channelName: 'general',
        topic: 'relay talk, coast mesh, nothing operational',
        linkQuality: const ChannelLinkQuality(level: LinkQualityLevel.excellent, hops: 2),
        activeTab: ChannelTab.chat,
        onTabSelected: (_) {},
      ),
      width: 900,
    ));
    await tester.pump();
    await expectLater(find.byType(ChannelHeader), matchesGoldenFile('goldens/channel_header.png'));
  });

  testWidgets('message_list', (tester) async {
    await tester.pumpWidget(_harness(
      MessageList(
        messages: fixtureMessages(),
        meHashHex: kSelfHash,
        displayNameFor: (hash, fallback) => fallback,
      ),
      width: 900,
      height: 700,
    ));
    await tester.pump();
    await tester.pump();
    await expectLater(find.byType(MessageList), matchesGoldenFile('goldens/message_list.png'));
  });

  testWidgets('compose_bar', (tester) async {
    await tester.pumpWidget(_harness(
      ComposeBar(channelName: 'general', enabled: true, onSend: (_) async => true),
      width: 900,
    ));
    await tester.pump();
    await expectLater(find.byType(ComposeBar), matchesGoldenFile('goldens/compose_bar.png'));
  });
}
