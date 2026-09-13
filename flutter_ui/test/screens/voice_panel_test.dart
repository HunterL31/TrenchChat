import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/models/link_quality.dart';
import 'package:flutter_ui/screens/main_window/voice_panel.dart';
import 'package:flutter_ui/widgets/signal_meter.dart';
import 'package:flutter_ui/widgets/tc_icon.dart';

Widget _harness({
  bool muted = false,
  bool audioError = false,
  String audioWarning = '',
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
          audioWarning: audioWarning,
          audioReason: audioReason,
          onToggleMute: onToggleMute ?? () {},
          onLeave: onLeave ?? () {},
        ),
      ),
    );

void main() {
  group('share screen', shareScreenTests);

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

  testWidgets('a specific warning headline replaces the generic one',
      (tester) async {
    await tester.pumpWidget(_harness(
      audioError: true,
      audioWarning: 'MIC UNAVAILABLE — LISTENING ONLY',
    ));

    expect(find.text('MIC UNAVAILABLE — LISTENING ONLY'), findsOneWidget);
    expect(find.text('NO AUDIO DEVICE — LISTENING ONLY'), findsNothing);
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

Widget _shareHarness({
  VoidCallback? onShareScreen,
  String? disabledReason,
  String sharingSource = '',
  int viewerCount = 0,
  VoidCallback? onStopShare,
}) =>
    MaterialApp(
      home: Scaffold(
        body: VoicePanel(
          channelName: 'general',
          quality: LinkQualityLevel.excellent,
          muted: false,
          audioError: false,
          onToggleMute: () {},
          onLeave: () {},
          onShareScreen: onShareScreen,
          shareScreenDisabledReason: disabledReason,
          sharingSource: sharingSource,
          viewerCount: viewerCount,
          onStopShare: onStopShare,
        ),
      ),
    );

void shareScreenTests() {
  testWidgets('no share control when the caller has no opinion', (tester) async {
    await tester.pumpWidget(_shareHarness());
    expect(find.byTooltip('Share screen'), findsNothing);
    expect(find.byWidgetPredicate((w) => w is TcIcon && w.icon == TcIcons.screen),
        findsNothing);
  });

  testWidgets('the share control fires its callback', (tester) async {
    var fired = false;
    await tester.pumpWidget(_shareHarness(onShareScreen: () => fired = true));
    await tester.tap(find.byTooltip('Share screen'));
    expect(fired, isTrue);
  });

  testWidgets('a disabled share control names its reason', (tester) async {
    await tester.pumpWidget(_shareHarness(
        disabledReason: 'Screen share needs direct connections, which are off.'));
    expect(find.byTooltip('Screen share needs direct connections, which are off.'),
        findsOneWidget);
    expect(find.byTooltip('Share screen'), findsNothing);
  });

  testWidgets('while sharing the panel says so and offers stop', (tester) async {
    var stopped = false;
    await tester.pumpWidget(_shareHarness(
        sharingSource: 'Monitor 2', viewerCount: 3, onStopShare: () => stopped = true));
    expect(find.text('SHARING Monitor 2 · 3 WATCHING'), findsOneWidget);
    await tester.tap(find.byTooltip('Stop sharing'));
    expect(stopped, isTrue);
    expect(find.byTooltip('Share screen'), findsNothing);
  });
}
