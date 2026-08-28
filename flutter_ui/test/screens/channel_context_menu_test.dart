// The channel row's right-click menu, ported from the Qt client's channel
// menu: what it offers depends on what this reader may actually do there, and
// every entry hands back the channel the menu was opened on -- the rows are
// otherwise plain values and callbacks, like the rest of ChannelColumn.
import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/models/permissions.dart';
import 'package:flutter_ui/api/models/server.dart';
import 'package:flutter_ui/screens/main_window/channel_column.dart';

Channel _channel(String name, {String? serverHash}) => Channel.fromJson({
      'hash': 'hash-$name',
      'name': name,
      'description': '',
      'creator_hash': 'creator',
      'open_join': true,
      'created_at': 0,
      'server_hash': serverHash,
    });

const _noPermissions = ChannelPermissions(
  invite: false,
  kick: false,
  manageRoles: false,
  manageChannel: false,
  sendMessage: true,
  voiceChat: false,
);

const _allPermissions = ChannelPermissions(
  invite: true,
  kick: true,
  manageRoles: true,
  manageChannel: true,
  sendMessage: true,
  voiceChat: true,
);

Future<void> _rightClick(WidgetTester tester, Finder finder) async {
  final gesture = await tester.startGesture(
    tester.getCenter(finder),
    kind: PointerDeviceKind.mouse,
    buttons: kSecondaryButton,
  );
  await gesture.up();
  await tester.pump();
}

Widget _harness({
  required List<Channel> directChannels,
  List<Channel> channels = const [],
  Map<String, ChannelPermissions> permissions = const {},
  void Function(Channel)? onViewMembers,
  void Function(Channel)? onInviteToChannel,
  void Function(Channel)? onEditPermissions,
  void Function(Channel)? onLeaveChannel,
}) =>
    MaterialApp(
      home: Scaffold(
        body: ChannelColumn(
          serverName: channels.isEmpty ? null : 'mesh-crew',
          serverMemberCount: null,
          channels: channels,
          directChannels: directChannels,
          selectedChannelHash: null,
          onSelectChannel: (_) {},
          channelPermissions: permissions,
          onViewMembers: onViewMembers,
          onInviteToChannel: onInviteToChannel,
          onEditPermissions: onEditPermissions,
          onLeaveChannel: onLeaveChannel,
        ),
      ),
    );

void main() {
  testWidgets('a channel row offers Members… and fires it with that channel',
      (tester) async {
    Channel? opened;
    await tester.pumpWidget(_harness(
      directChannels: [_channel('general')],
      onViewMembers: (c) => opened = c,
    ));
    await tester.pump();

    await _rightClick(tester, find.text('general'));

    expect(find.text('Members…'), findsOneWidget);
    await tester.tap(find.text('Members…'));
    await tester.pump();

    expect(opened?.hash, 'hash-general');
  });

  testWidgets('Invite… and Edit permissions… appear only with the permission',
      (tester) async {
    await tester.pumpWidget(_harness(
      directChannels: [_channel('general')],
      permissions: const {'hash-general': _noPermissions},
      onViewMembers: (_) {},
      onInviteToChannel: (_) {},
      onEditPermissions: (_) {},
    ));
    await tester.pump();

    await _rightClick(tester, find.text('general'));

    expect(find.text('Members…'), findsOneWidget);
    expect(find.text('Invite…'), findsNothing);
    expect(find.text('Edit permissions…'), findsNothing);
  });

  testWidgets('with the permissions granted both entries are offered', (tester) async {
    Channel? invited;
    await tester.pumpWidget(_harness(
      directChannels: [_channel('general')],
      permissions: const {'hash-general': _allPermissions},
      onViewMembers: (_) {},
      onInviteToChannel: (c) => invited = c,
      onEditPermissions: (_) {},
    ));
    await tester.pump();

    await _rightClick(tester, find.text('general'));

    expect(find.text('Edit permissions…'), findsOneWidget);
    await tester.tap(find.text('Invite…'));
    await tester.pump();

    expect(invited?.hash, 'hash-general');
  });

  testWidgets('Leave channel is offered for a standalone channel', (tester) async {
    Channel? left;
    await tester.pumpWidget(_harness(
      directChannels: [_channel('general')],
      onViewMembers: (_) {},
      onLeaveChannel: (c) => left = c,
    ));
    await tester.pump();

    await _rightClick(tester, find.text('general'));
    await tester.tap(find.text('Leave channel'));
    await tester.pump();

    expect(left?.hash, 'hash-general');
  });

  testWidgets("a server's channel offers no Leave: membership belongs to the server",
      (tester) async {
    await tester.pumpWidget(_harness(
      channels: [_channel('general', serverHash: 'server-1')],
      directChannels: const [],
      onViewMembers: (_) {},
      onLeaveChannel: (_) {},
    ));
    await tester.pump();

    await _rightClick(tester, find.text('general'));

    expect(find.text('Members…'), findsOneWidget);
    expect(find.text('Leave channel'), findsNothing);
  });

  testWidgets('with no callbacks wired the row stays menu-free', (tester) async {
    await tester.pumpWidget(_harness(directChannels: [_channel('general')]));
    await tester.pump();

    await _rightClick(tester, find.text('general'));

    expect(find.text('Members…'), findsNothing);
    expect(find.text('Leave channel'), findsNothing);
  });
}
