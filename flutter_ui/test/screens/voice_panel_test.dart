import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/models/link_quality.dart';
import 'package:flutter_ui/screens/main_window/voice_panel.dart';
import 'package:flutter_ui/widgets/signal_meter.dart';
import 'package:flutter_ui/widgets/tc_icon.dart';

Widget _harness({
  bool muted = false,
  bool audioError = false,
  String audioReason = '',
  VoidCallback? onToggleMute,
  VoidCallback? onLeave,
}) =>
    MaterialApp(
      home: Scaffold(
        body: VoicePanel(
          channelName: 'general',
          quality: LinkQualityLevel.excellent,
          muted: muted,
          audioError: audioError,
          audioReason: audioReason,
          onToggleMute: onToggleMute ?? () {},
          onLeave: onLeave ?? () {},
        ),
      ),
    );

void main() {
  testWidgets('shows channel, meter, and LIVE when unmuted', (tester) async {
    await tester.pumpWidget(_harness());

    expect(find.text('VOICE · #general'), findsOneWidget);
    expect(find.byType(SignalMeter), findsOneWidget);
    expect(find.text('LIVE'), findsOneWidget);
    expect(find.byTooltip('Mute'), findsOneWidget);
    expect(
      find.byWidgetPredicate((w) => w is TcIcon && w.icon == TcIcons.mic),
      findsOneWidget,
    );
    expect(find.textContaining('NO AUDIO DEVICE'), findsNothing);
  });

  testWidgets('muted swaps the icon, tooltip and tag', (tester) async {
    await tester.pumpWidget(_harness(muted: true));

    expect(find.text('MUTED'), findsOneWidget);
    expect(find.byTooltip('Unmute'), findsOneWidget);
    expect(
      find.byWidgetPredicate((w) => w is TcIcon && w.icon == TcIcons.micMuted),
      findsOneWidget,
    );
  });

  testWidgets('audio error surfaces the listening-only warning', (tester) async {
    await tester.pumpWidget(_harness(audioError: true));
    expect(find.text('NO AUDIO DEVICE — LISTENING ONLY'), findsOneWidget);
  });

  testWidgets("the backend's audio failure reason is shown under the warning",
      (tester) async {
    const reason = 'input device USB Headset failed to open';
    await tester.pumpWidget(_harness(audioError: true, audioReason: reason));

    expect(find.text('NO AUDIO DEVICE — LISTENING ONLY'), findsOneWidget);
    expect(find.text(reason), findsOneWidget);
  });

  testWidgets('no reason line without an audio error', (tester) async {
    await tester.pumpWidget(_harness(audioReason: 'stale reason'));
    expect(find.text('stale reason'), findsNothing);
  });

  testWidgets('mute and leave callbacks fire', (tester) async {
    var muted = false;
    var left = false;
    await tester.pumpWidget(_harness(
      onToggleMute: () => muted = true,
      onLeave: () => left = true,
    ));

    await tester.tap(find.byTooltip('Mute'));
    await tester.tap(find.byTooltip('Leave voice'));
    expect(muted, isTrue);
    expect(left, isTrue);
  });
}
