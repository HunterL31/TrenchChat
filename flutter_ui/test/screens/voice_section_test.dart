import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/models/voice.dart';
import 'package:flutter_ui/screens/main_window/channel_column.dart';
import 'package:flutter_ui/widgets/tc_icon.dart';

const _kAlice = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
const _kBob = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb';

VoiceParticipant _participant(
  String hash, {
  String name = '',
  bool muted = false,
  bool speaking = false,
  VoiceLinkState linkState = VoiceLinkState.streaming,
}) =>
    VoiceParticipant(
      identityHash: hash,
      displayName: name,
      muted: muted,
      joinedAt: 0,
      linkState: linkState,
      speaking: speaking,
    );

Widget _harness({
  List<VoiceParticipant> voiceParticipants = const [],
  VoidCallback? onJoinVoice,
}) =>
    MaterialApp(
      home: Scaffold(
        body: ChannelColumn(
          serverName: null,
          serverMemberCount: null,
          channels: const [],
          directChannels: const [],
          selectedChannelHash: null,
          onSelectChannel: (_) {},
          voiceParticipants: voiceParticipants,
          onJoinVoice: onJoinVoice,
        ),
      ),
    );

void main() {
  testWidgets('renders no voice section with an empty roster and no join',
      (tester) async {
    // Golden-stability regression: the unused section must add nothing.
    await tester.pumpWidget(_harness());
    expect(find.textContaining('VOICE'), findsNothing);
  });

  testWidgets('JOIN VOICE fires the callback', (tester) async {
    var fired = false;
    await tester.pumpWidget(_harness(onJoinVoice: () => fired = true));

    expect(find.text('▾ VOICE — 0'), findsOneWidget);
    await tester.tap(find.text('JOIN VOICE'));
    expect(fired, isTrue);
  });

  testWidgets('roster shows names, count, and a muted icon', (tester) async {
    await tester.pumpWidget(_harness(
      voiceParticipants: [
        _participant(_kAlice, name: 'Alice', muted: true),
        _participant(_kBob),
      ],
    ));

    expect(find.text('▾ VOICE — 2'), findsOneWidget);
    expect(find.text('Alice'), findsOneWidget);
    expect(find.text('bbbb…bbbb'), findsOneWidget); // short hash fallback
    expect(
      find.byWidgetPredicate((w) => w is TcIcon && w.icon == TcIcons.micMuted),
      findsOneWidget,
    );
    expect(find.text('JOIN VOICE'), findsNothing);
  });

  testWidgets('an unreachable participant is dimmed, a streaming one is not',
      (tester) async {
    await tester.pumpWidget(_harness(
      voiceParticipants: [
        _participant(_kAlice, name: 'Alice', linkState: VoiceLinkState.unreachable),
        _participant(_kBob, name: 'Bob'),
      ],
    ));

    final aliceOpacity = tester.widget<Opacity>(
      find.ancestor(of: find.text('Alice'), matching: find.byType(Opacity)).first,
    );
    final bobOpacity = tester.widget<Opacity>(
      find.ancestor(of: find.text('Bob'), matching: find.byType(Opacity)).first,
    );
    expect(aliceOpacity.opacity, 0.45);
    expect(bobOpacity.opacity, 1.0);
  });
}
