// The Join dialog lists only channels it can actually join: an invite-only
// channel the user already left surfaces in discovery but must not be offered
// here, where the public join flow would always refuse it.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/models/server.dart';
import 'package:flutter_ui/app_state.dart';
import 'package:flutter_ui/screens/dialogs/join_channel_dialog.dart';

import '../../fake_backend.dart';

Map<String, Object?> _channelJson(String name, {bool openJoin = true}) => {
      'hash': 'hash-$name',
      'name': name,
      'description': '',
      'creator_hash': 'creator',
      'open_join': openJoin,
      'created_at': 0,
      'server_hash': null,
    };

Widget _harness(AppState state) => MaterialApp(
      home: Scaffold(
        body: Builder(
          builder: (context) => ElevatedButton(
            onPressed: () => showJoinChannelDialog(context, state),
            child: const Text('open'),
          ),
        ),
      ),
    );

void main() {
  late FakeBackend backend;
  late AppState state;

  setUp(() {
    backend = FakeBackend();
    state = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
  });

  tearDown(() => state.dispose());

  testWidgets('offers only joinable channels, not invite-only left ones',
      (tester) async {
    state.standaloneChannels = [Channel.fromJson(_channelJson('joined'))];
    backend.routes['GET /channels/discovered'] = [
      _channelJson('joined'),
      _channelJson('secret', openJoin: false),
      _channelJson('townsquare'),
    ];

    await tester.pumpWidget(_harness(state));
    await tester.tap(find.text('open'));
    await tester.pump();
    await settle(tester);

    expect(find.text('townsquare'), findsOneWidget);
    expect(find.text('secret'), findsNothing);
    expect(find.text('joined'), findsNothing);
  });
}
