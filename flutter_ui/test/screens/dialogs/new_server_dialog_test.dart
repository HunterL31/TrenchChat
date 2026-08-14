// Points AppState at a closed local port so mutating calls fail fast with a
// connection error instead of hanging -- enough to exercise validation and
// the dialog's inline error path without a live backend.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/app_state.dart';
import 'package:flutter_ui/screens/dialogs/new_server_dialog.dart';
import 'package:flutter_ui/widgets/tc_button.dart';

Widget _harness(AppState state) {
  return MaterialApp(
    home: Scaffold(
      body: Builder(
        builder: (context) => ElevatedButton(
          onPressed: () => showNewServerDialog(context, state),
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

  testWidgets('shows name and description fields with Create/Cancel actions', (tester) async {
    await tester.pumpWidget(_harness(state));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    expect(find.text('New Server'), findsOneWidget);
    expect(find.byType(TextField), findsNWidgets(2));
    expect(find.text('CANCEL'), findsOneWidget);
    expect(find.text('CREATE'), findsOneWidget);
  });

  testWidgets('rejects an empty name without calling the API', (tester) async {
    await tester.pumpWidget(_harness(state));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    await tester.tap(find.text('CREATE'));
    await tester.pump();

    expect(find.text('Server name cannot be empty.'), findsOneWidget);
    expect(find.text('New Server'), findsOneWidget); // dialog stayed open
  });

  testWidgets('cancel dismisses without creating anything', (tester) async {
    await tester.pumpWidget(_harness(state));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    await tester.tap(find.text('CANCEL'));
    await tester.pumpAndSettle();

    expect(find.text('New Server'), findsNothing);
  });

  testWidgets('a failed create surfaces an inline error and keeps the dialog open',
      (tester) async {
    await tester.pumpWidget(_harness(state));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    await tester.enterText(find.byType(TextField).first, 'mesh-crew');
    await tester.tap(find.text('CREATE'));
    await tester.pumpAndSettle();

    expect(find.text('New Server'), findsOneWidget);
    expect(find.byType(TcPrimaryButton), findsOneWidget);
    expect(state.actionError, isNotNull);
  });
}
