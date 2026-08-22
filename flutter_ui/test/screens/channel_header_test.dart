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

  testWidgets('a narrow header keeps the name visible and the FRIENDS tab reachable',
      (tester) async {
    ChannelTab? selected;
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: Align(
          alignment: Alignment.topLeft,
          child: SizedBox(
            width: 460,
            child: ChannelHeader(
              channelName: 'a-very-long-channel-name-that-would-collapse',
              topic: 'a topic competing for the same row',
              linkQuality:
                  const ChannelLinkQuality(level: LinkQualityLevel.excellent, hops: 2),
              activeTab: ChannelTab.chat,
              onTabSelected: (t) => selected = t,
            ),
          ),
        ),
      ),
    ));
    await tester.pumpAndSettle();

    // No RenderFlex overflow at this width, and the name is not dropped.
    expect(tester.takeException(), isNull);
    expect(find.text('a-very-long-channel-name-that-would-collapse'), findsOneWidget);

    // The FRIENDS tab is on-screen (as a tooltipped icon) and still fires.
    await tester.tap(find.byTooltip('FRIENDS'));
    expect(selected, ChannelTab.friends);
  });
}
