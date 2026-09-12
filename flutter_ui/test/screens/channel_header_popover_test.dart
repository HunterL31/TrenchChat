// Pointing at the header's link pill explains it: who is reachable, how far
// away, over which next hop, and how long the path is good for. The pill
// itself only has room for the summary.
import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/models/link_quality.dart';
import 'package:flutter_ui/screens/main_window/channel_header.dart';
import 'package:flutter_ui/screens/main_window/link_quality_popover.dart';

const _quality = ChannelLinkQuality(
  level: LinkQualityLevel.good,
  medianHops: 2,
  reachable: 2,
  total: 3,
  bestIdentityHash: 'aa',
  bestHops: 1,
  peers: [
    PeerLinkQuality(
      identityHash: 'aa',
      displayName: 'ada',
      level: LinkQualityLevel.excellent,
      hops: 1,
      via: 'abcdef0123456789',
      rttMs: 42.4,
      pathExpiresIn: 300,
      isOnline: true,
    ),
    PeerLinkQuality(
      identityHash: 'bb',
      displayName: 'grace',
      level: LinkQualityLevel.fair,
      hops: 3,
      pathExpiresIn: 90,
    ),
    PeerLinkQuality(
      identityHash: 'cc',
      displayName: 'hopper',
      level: LinkQualityLevel.unknown,
    ),
  ],
);

Widget _harness({bool compact = false}) => MaterialApp(
      home: Scaffold(
        body: Align(
          alignment: Alignment.topLeft,
          child: SizedBox(
            width: compact ? 400 : 780,
            child: ChannelHeader(
              channelName: 'general',
              topic: '',
              linkQuality: _quality,
              activeTab: ChannelTab.chat,
              onTabSelected: (_) {},
              compact: compact,
            ),
          ),
        ),
      ),
    );

Finder get _summary => find.textContaining('2 of 3 reachable');

Future<TestGesture> _hoverPill(WidgetTester tester) async {
  final gesture = await tester.createGesture(kind: PointerDeviceKind.mouse);
  await gesture.addPointer(location: Offset.zero);
  addTearDown(gesture.removePointer);
  await tester.pump();
  await gesture.moveTo(tester.getCenter(find.byType(LinkQualityPopover)));
  await tester.pump(linkPopoverOpenDelay + const Duration(milliseconds: 50));
  return gesture;
}

void main() {
  testWidgets('the pill alone shows only the summary', (tester) async {
    await tester.pumpWidget(_harness());

    expect(find.text('2/3 · ~2 HOPS'), findsOneWidget);
    expect(_summary, findsNothing);
  });

  testWidgets('hovering the pill breaks the reading down per peer',
      (tester) async {
    await tester.pumpWidget(_harness());
    await _hoverPill(tester);

    expect(_summary, findsOneWidget);
    expect(find.textContaining('median 2 hops'), findsOneWidget);
    expect(find.textContaining('closest ada (1 hop)'), findsOneWidget);

    // Reachable peers, closest first, with what the path table knows.
    expect(find.text('ada'), findsOneWidget);
    expect(find.text('1 hop'), findsOneWidget);
    expect(find.textContaining('RTT 42 ms'), findsOneWidget);
    expect(find.textContaining('via abcdef…6789'), findsOneWidget);
    expect(find.textContaining('path expires in 5m'), findsOneWidget);
    expect(find.text('grace'), findsOneWidget);
    expect(find.text('3 hops'), findsOneWidget);

    // A member with no path is named rather than dropped: a send to them is
    // queued, not delivered, and the pill's 2 of 3 has to be accountable.
    expect(find.text('hopper · no path'), findsOneWidget);

    expect(
      find.textContaining('From the Reticulum path table'),
      findsOneWidget,
      reason: 'the reading says where it came from and how often it moves',
    );
  });

  testWidgets('moving the pointer away closes it', (tester) async {
    await tester.pumpWidget(_harness());
    final gesture = await _hoverPill(tester);
    expect(_summary, findsOneWidget);

    await gesture.moveTo(const Offset(5, 300));
    await tester.pump(const Duration(milliseconds: 400));

    expect(_summary, findsNothing);
  });

  testWidgets('a narrow header toggles the panel on tap', (tester) async {
    await tester.pumpWidget(_harness(compact: true));
    expect(_summary, findsNothing);

    await tester.tap(find.byType(LinkQualityPopover));
    await tester.pump();
    expect(_summary, findsOneWidget);

    await tester.tapAt(const Offset(5, 300));
    await tester.pump();
    expect(_summary, findsNothing);
  });
}
