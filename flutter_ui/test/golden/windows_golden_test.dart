// Goldens for the windows ported from the Qt client: Settings, Members,
// Invite, incoming invite, and the IFACE and MAP tabs. Data comes from the
// canned MockClient transport in fake_backend.dart, so the suite stays
// offline like the rest of the golden tests.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/models/invite.dart';
import 'package:flutter_ui/api/models/member.dart';
import 'package:flutter_ui/api/models/permissions.dart';
import 'package:flutter_ui/app_state.dart';
import 'package:flutter_ui/screens/dialogs/incoming_invite_dialog.dart';
import 'package:flutter_ui/screens/dialogs/invite_dialog.dart';
import 'package:flutter_ui/screens/dialogs/members_dialog.dart';
import 'package:flutter_ui/screens/dialogs/settings_dialog.dart';
import 'package:flutter_ui/screens/main_window/iface_tab.dart';
import 'package:flutter_ui/screens/main_window/map_tab.dart';
import 'package:flutter_ui/theme/app_theme.dart';

import '../fake_backend.dart';
import 'fixtures.dart';
import 'test_fonts.dart';

Widget _harness(Widget home) => MaterialApp(
      theme: buildAppTheme(),
      debugShowCheckedModeBanner: false,
      home: home,
    );

Widget _dialogOpener(AppState state,
    void Function(BuildContext context, AppState state) show) {
  return _harness(Scaffold(
    body: Builder(
      builder: (context) => Center(
        child: ElevatedButton(
          onPressed: () => show(context, state),
          child: const Text('open'),
        ),
      ),
    ),
  ));
}

void main() {
  setUpAll(loadTestFonts);

  late FakeBackend backend;
  late AppState state;

  setUp(() {
    backend = FakeBackend();
    state = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
    state.meHashHex = kSelfHash;
    state.meDisplayName = 'operator';
  });

  tearDown(() {
    state.dispose();
  });

  Future<void> openAndSnap(WidgetTester tester, String golden,
      void Function(BuildContext context, AppState state) show) async {
    await tester.pumpWidget(_dialogOpener(state, show));
    await tester.tap(find.text('open'));
    await tester.pump();
    await tester.pumpAndSettle();
    await expectLater(find.byType(MaterialApp), matchesGoldenFile(golden));
  }

  testWidgets('settings dialog', (tester) async {
    backend.routes['GET /settings'] = {
      'propagation_enabled': true,
      'propagation_node_name': 'ridge-relay',
      'propagation_storage_limit_mb': 512,
      'channel_filter_mode': 'allowlist',
      'channel_filter_hashes': [kGeneralHash],
      'outbound_propagation_node': '',
    };
    state.standaloneChannels = fixtureDirectChannels();
    state.channelsByServer[kServerHash] = fixtureServerChannels();
    await openAndSnap(tester, 'goldens/settings_dialog.png',
        (context, state) => showSettingsDialog(context, state));
  });

  testWidgets('members dialog', (tester) async {
    backend.routes['GET /channels/$kGeneralHash/members'] = [];
    state.membersByChannel[kGeneralHash] = [
      Member(channelHash: kGeneralHash, identityHash: kAliceHash,
          displayName: 'Alice', role: 'owner', addedAt: 0),
      Member(channelHash: kGeneralHash, identityHash: kSelfHash,
          displayName: 'operator', role: 'admin', addedAt: 0),
      Member(channelHash: kGeneralHash, identityHash: kBobHash,
          displayName: 'Bob', role: 'member', addedAt: 0),
    ];
    state.presenceByChannel[kGeneralHash] = fixturePresence();
    state.permissionsByChannel[kGeneralHash] = const ChannelPermissions(
        kick: true, manageRoles: true, manageChannel: true, sendMessage: true);
    await openAndSnap(tester, 'goldens/members_dialog.png',
        (context, state) => showMembersDialog(context, state,
            channelHashHex: kGeneralHash, channelName: 'general'));
  });

  testWidgets('invite dialog', (tester) async {
    backend.routes['GET /directory'] = [
      {'identity_hash': kAliceHash, 'display_name': 'Alice', 'is_online': true},
      {'identity_hash': kBobHash, 'display_name': '', 'is_online': false},
    ];
    await openAndSnap(tester, 'goldens/invite_dialog.png',
        (context, state) => showInviteDialog(context, state,
            channelHashHex: kOpsHash, channelName: 'ops'));
  });

  testWidgets('incoming invite dialog', (tester) async {
    final invite = PendingInvite(
      channelHashHex: kOpsHash,
      channelName: 'ops',
      // Fixed far-future expiry so the "expires in N days" label is stable.
      expiry: DateTime.now().millisecondsSinceEpoch / 1000 + 3600 * 24 * 6,
      adminHex: kAliceHash,
      scopeKind: 'channel',
    );
    await openAndSnap(tester, 'goldens/incoming_invite_dialog.png',
        (context, state) => showIncomingInviteDialog(context, state, invite));
  });

  testWidgets('iface tab', (tester) async {
    backend.routes['GET /reticulum/interfaces'] = [
      {'name': 'TrenchChat Hub', 'type': 'TCPClientInterface', 'enabled': true,
       'editable': true, 'status': true, 'rxb': 152400, 'txb': 38200},
      {'name': 'Default Interface', 'type': 'AutoInterface', 'enabled': true,
       'editable': true, 'status': false, 'rxb': 0, 'txb': 0},
      {'name': 'RNode LoRa', 'type': 'RNodeInterface', 'enabled': false,
       'editable': true, 'status': null, 'rxb': null, 'txb': null},
    ];
    await tester.pumpWidget(_harness(Scaffold(body: IfaceTab(state: state))));
    await tester.pumpAndSettle();
    await expectLater(
        find.byType(MaterialApp), matchesGoldenFile('goldens/iface_tab.png'));
  });

  testWidgets('map tab', (tester) async {
    backend.routes['GET /network/map'] = {
      'nodes': [
        {'id': kSelfHash, 'label': 'This device', 'kind': 'self', 'hops': 0},
        {'id': '__iface__TrenchChat Hub', 'label': '● TrenchChat Hub (TCP)',
         'kind': 'interface', 'hops': 0},
        {'id': kAliceHash, 'label': 'Alice', 'kind': 'peer', 'hops': 1},
        {'id': kBobHash, 'label': '7b8d…41aa', 'kind': 'peer', 'hops': 2},
        {'id': kCarolHash, 'label': 'relay', 'kind': 'transport', 'hops': 1},
      ],
      'edges': [
        {'src': kSelfHash, 'dst': '__iface__TrenchChat Hub', 'direct': true},
        {'src': kSelfHash, 'dst': kAliceHash, 'direct': true},
        {'src': kSelfHash, 'dst': kCarolHash, 'direct': true},
        {'src': kCarolHash, 'dst': kBobHash, 'direct': false},
      ],
      'interfaces': [
        {'name': 'TrenchChat Hub', 'type': 'TCPClientInterface', 'status': true,
         'rxb': 152400, 'txb': 38200},
      ],
      'stats': {'node_count': 5, 'path_count': 3, 'interface_count': 1},
    };
    await tester.pumpWidget(_harness(Scaffold(body: MapTab(state: state))));
    await tester.pumpAndSettle();
    await expectLater(
        find.byType(MaterialApp), matchesGoldenFile('goldens/map_tab.png'));
  });
}
