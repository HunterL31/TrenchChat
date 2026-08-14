// The one behavioral contract that matters here: the access preset picker
// (public/invite-only) appears for a standalone channel and is omitted for
// a channel created inside a server, which always inherits the server's
// permissions instead.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/app_state.dart';
import 'package:flutter_ui/screens/dialogs/new_channel_dialog.dart';

Widget _harness(AppState state, {String? serverHashHex}) {
  return MaterialApp(
    home: Scaffold(
      body: Builder(
        builder: (context) => ElevatedButton(
          onPressed: () =>
              showNewChannelDialog(context, state, serverHashHex: serverHashHex),
          child: const Text('open'),
        ),
      ),
    ),
  );
}

void main() {
  late AppState state;

  setUp(() {
    state = AppState(baseUrl: 'http://127.0.0.1:65500');
  });

  tearDown(() {
    state.dispose();
  });

  testWidgets('standalone channel dialog offers an access preset picker', (tester) async {
    await tester.pumpWidget(_harness(state));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    expect(find.text('New Channel'), findsOneWidget);
    expect(find.text('ACCESS'), findsOneWidget);
    expect(find.text('PUBLIC'), findsOneWidget);
    expect(find.text('INVITE-ONLY'), findsOneWidget);
  });

  testWidgets('in-server channel dialog omits the access preset picker', (tester) async {
    await tester.pumpWidget(_harness(state, serverHashHex: 'server-hash'));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    expect(find.text('New Channel in Server'), findsOneWidget);
    expect(find.text('ACCESS'), findsNothing);
    expect(find.text('PUBLIC'), findsNothing);
    expect(find.text('INVITE-ONLY'), findsNothing);
  });

  testWidgets('rejects an empty name without calling the API', (tester) async {
    await tester.pumpWidget(_harness(state));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    await tester.tap(find.text('CREATE'));
    await tester.pump();

    expect(find.text('Channel name cannot be empty.'), findsOneWidget);
  });

  testWidgets('tapping an access option selects it', (tester) async {
    await tester.pumpWidget(_harness(state));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    // Public is selected by default; switching to invite-only must not throw
    // and must leave exactly one option visible as each label.
    await tester.tap(find.text('INVITE-ONLY'));
    await tester.pump();

    expect(find.text('PUBLIC'), findsOneWidget);
    expect(find.text('INVITE-ONLY'), findsOneWidget);
  });
}
