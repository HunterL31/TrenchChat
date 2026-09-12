// The "direct" badge: on a member row and a voice roster row this node holds
// a direct IP session with, and on nothing else. A peer on the mesh looks
// exactly as it did before, which is what keeps the badge meaning something.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/events.dart';
import 'package:flutter_ui/api/models/link_quality.dart';
import 'package:flutter_ui/api/models/member.dart';
import 'package:flutter_ui/api/models/upgrade.dart';
import 'package:flutter_ui/api/models/voice.dart';
import 'package:flutter_ui/app_state.dart';
import 'package:flutter_ui/screens/dialogs/members_dialog.dart';
import 'package:flutter_ui/screens/main_window/channel_column.dart';
import 'package:flutter_ui/screens/main_window/presence_panel.dart';
import 'package:flutter_ui/screens/main_window/voice_panel.dart';
import 'package:flutter_ui/widgets/badge.dart';

import '../fake_backend.dart';

const _me = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
const _alice = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb';
const _bob = 'cccccccccccccccccccccccccccccccc';
const _channel = 'dddddddddddddddddddddddddddddddd';

VoiceParticipant _participant(String hash, String name, PeerPath path) =>
    VoiceParticipant(
      identityHash: hash,
      displayName: name,
      muted: false,
      joinedAt: 0,
      linkState: VoiceLinkState.streaming,
      speaking: false,
      path: path,
    );

Member _member(String hash, String name) => Member(
      channelHash: _channel,
      identityHash: hash,
      displayName: name,
      role: 'member',
      addedAt: 0,
    );

void main() {
  group('the presence roster', () {
    Widget harness(Map<String, PeerPath> paths) => MaterialApp(
          home: Scaffold(
            body: Row(children: [
              PresencePanel(
                presence: const [
                  PresenceEntry(
                      identityHash: _alice, isOnline: true, displayName: 'Alice'),
                  PresenceEntry(
                      identityHash: _bob, isOnline: true, displayName: 'Bob'),
                ],
                meHashHex: _me,
                paths: paths,
              ),
            ]),
          ),
        );

    testWidgets('marks the direct member and leaves the mesh one alone',
        (tester) async {
      await tester.pumpWidget(harness(const {
        _alice: PeerPath.direct,
        _bob: PeerPath.reticulum,
      }));

      expect(find.byType(DirectBadge), findsOneWidget);
      expect(
        find.descendant(
            of: find.ancestor(
                of: find.text('Alice'), matching: find.byType(Row)),
            matching: find.byType(DirectBadge)),
        findsOneWidget,
      );
    });

    testWidgets('an offline or unknown path marks nothing', (tester) async {
      await tester.pumpWidget(harness(const {
        _alice: PeerPath.offline,
      }));

      expect(find.byType(DirectBadge), findsNothing);
    });
  });

  group('the members dialog', () {
    Future<void> open(WidgetTester tester, AppState state) async {
      await tester.pumpWidget(MaterialApp(
        home: Scaffold(
          body: Builder(
            builder: (context) => ElevatedButton(
              onPressed: () => showMembersDialog(context, state,
                  channelHashHex: _channel, channelName: 'general'),
              child: const Text('open'),
            ),
          ),
        ),
      ));
      await tester.tap(find.text('open'));
      await tester.pump();
      await settle(tester);
    }

    testWidgets('marks a member this node holds a session with', (tester) async {
      final backend = FakeBackend();
      backend.routes['GET /channels/$_channel/members'] = [
        {
          'channel_hash': _channel,
          'identity_hash': _alice,
          'display_name': 'Alice',
          'role': 'member',
          'added_at': 0.0,
          'path': 'direct',
        },
        {
          'channel_hash': _channel,
          'identity_hash': _bob,
          'display_name': 'Bob',
          'role': 'member',
          'added_at': 0.0,
          'path': 'reticulum',
        },
      ];
      backend.routes['GET /channels/$_channel/messages'] = <dynamic>[];
      backend.routes['GET /channels/$_channel/presence'] = <dynamic>[];
      backend.routes['GET /channels/$_channel/link_quality'] = <dynamic>[];
      backend.routes['GET /channels/$_channel/my_permissions'] =
          <String, dynamic>{};
      backend.routes['GET /channels/$_channel/voice/roster'] = <dynamic>[];
      backend.routes['GET /channels/$_channel/sync_status'] = {'state': 'synced'};
      final state =
          AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
      addTearDown(state.dispose);
      state.meHashHex = _me;
      state.membersByChannel[_channel] = [
        _member(_alice, 'Alice'),
        _member(_bob, 'Bob'),
      ];

      await open(tester, state);

      expect(find.byType(DirectBadge), findsOneWidget);
    });

    testWidgets('a path_changed event lights the badge without a reload',
        (tester) async {
      final backend = FakeBackend();
      backend.routes['GET /channels/$_channel/members'] = [
        {
          'channel_hash': _channel,
          'identity_hash': _alice,
          'display_name': 'Alice',
          'role': 'member',
          'added_at': 0.0,
          'path': 'reticulum',
        },
      ];
      backend.routes['GET /channels/$_channel/messages'] = <dynamic>[];
      backend.routes['GET /channels/$_channel/presence'] = <dynamic>[];
      backend.routes['GET /channels/$_channel/link_quality'] = <dynamic>[];
      backend.routes['GET /channels/$_channel/my_permissions'] =
          <String, dynamic>{};
      backend.routes['GET /channels/$_channel/voice/roster'] = <dynamic>[];
      backend.routes['GET /channels/$_channel/sync_status'] = {'state': 'synced'};
      final state =
          AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
      addTearDown(state.dispose);
      state.meHashHex = _me;
      state.membersByChannel[_channel] = [_member(_alice, 'Alice')];

      await open(tester, state);
      expect(find.byType(DirectBadge), findsNothing);

      state.applyEvent(const PathChangedEvent(_alice, PeerPath.direct, 1700.0));
      await tester.pump();
      expect(find.byType(DirectBadge), findsOneWidget);

      state.applyEvent(const PathChangedEvent(_alice, PeerPath.reticulum, 1700.0));
      await tester.pump();
      expect(find.byType(DirectBadge), findsNothing);
    });
  });

  group('the voice roster', () {
    Widget harness(List<VoiceParticipant> participants) => MaterialApp(
          home: Scaffold(
            body: ChannelColumn(
              serverName: null,
              serverMemberCount: null,
              channels: const [],
              directChannels: const [],
              selectedChannelHash: null,
              onSelectChannel: (_) {},
              voiceParticipants: participants,
            ),
          ),
        );

    testWidgets('marks the direct pair and nobody else', (tester) async {
      await tester.pumpWidget(harness([
        _participant(_alice, 'Alice', PeerPath.direct),
        _participant(_bob, 'Bob', PeerPath.reticulum),
      ]));

      expect(find.byType(DirectBadge), findsOneWidget);
    });

    testWidgets('this node\'s own row carries no path and no badge',
        (tester) async {
      await tester.pumpWidget(harness([
        _participant(_me, 'operator', PeerPath.unknown),
      ]));

      expect(find.byType(DirectBadge), findsNothing);
    });
  });

  group('the voice session panel', () {
    Widget harness({required bool allDirect}) => MaterialApp(
          home: Scaffold(
            body: VoicePanel(
              channelName: 'general',
              quality: LinkQualityLevel.good,
              muted: false,
              audioError: false,
              allDirect: allDirect,
              onToggleMute: () {},
              onLeave: () {},
            ),
          ),
        );

    testWidgets('marks a call whose every pair is direct', (tester) async {
      await tester.pumpWidget(harness(allDirect: true));
      expect(find.byType(DirectBadge), findsOneWidget);
    });

    testWidgets('a mixed call is not marked: the session runs at the mesh rate',
        (tester) async {
      await tester.pumpWidget(harness(allDirect: false));
      expect(find.byType(DirectBadge), findsNothing);
    });
  });

  testWidgets('the badge explains itself in the words the plan settled on',
      (tester) async {
    await tester.pumpWidget(const MaterialApp(
      home: Scaffold(body: Center(child: DirectBadge())),
    ));

    expect(find.text('DIRECT'), findsOneWidget);
    final tooltip = tester.widget<Tooltip>(find.byType(Tooltip));
    expect(tooltip.message,
        'Connected directly over IP; files and voice with this member take '
        'the fast path');
  });
}
