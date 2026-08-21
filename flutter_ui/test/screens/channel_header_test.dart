// The backend-socket connection indicator (#113): distinct from the mesh
// link pill, shown only when live updates are down.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/models/link_quality.dart';
import 'package:flutter_ui/api/ws.dart';
import 'package:flutter_ui/screens/main_window/channel_header.dart';

Widget _harness(TcConnState state) => MaterialApp(
      home: Scaffold(
        body: ChannelHeader(
          channelName: 'general',
          topic: '',
          linkQuality: const ChannelLinkQuality(level: LinkQualityLevel.excellent, hops: 1),
          connectionState: state,
          activeTab: ChannelTab.chat,
          onTabSelected: (_) {},
        ),
      ),
    );

void main() {
  testWidgets('a healthy socket shows no connection pill', (tester) async {
    await tester.pumpWidget(_harness(TcConnState.connected));
    expect(find.text('OFFLINE'), findsNothing);
    expect(find.text('RECONNECTING…'), findsNothing);
    // The mesh link pill is unaffected.
    expect(find.textContaining('EXCELLENT'), findsOneWidget);
  });

  testWidgets('a reconnecting socket shows the reconnecting pill', (tester) async {
    await tester.pumpWidget(_harness(TcConnState.reconnecting));
    expect(find.text('RECONNECTING…'), findsOneWidget);
    // Still not conflated with the mesh link pill.
    expect(find.textContaining('EXCELLENT'), findsOneWidget);
  });

  testWidgets('a dropped socket shows the offline pill', (tester) async {
    await tester.pumpWidget(_harness(TcConnState.disconnected));
    expect(find.text('OFFLINE'), findsOneWidget);
  });
}
