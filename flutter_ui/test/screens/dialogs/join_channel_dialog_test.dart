// Backend is unreachable (dead port), so the discovered-channels fetch that
// fires in initState resolves to an empty list -- enough to exercise the
// empty state and the disabled-until-selected Join button.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/app_state.dart';
import 'package:flutter_ui/screens/dialogs/join_channel_dialog.dart';
import 'package:flutter_ui/widgets/tc_button.dart';

Widget _harness(AppState state) {
  return MaterialApp(
    home: Scaffold(
      body: Builder(
        builder: (context) => ElevatedButton(
          onPressed: () => showJoinChannelDialog(context, state),
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

  testWidgets('shows an empty state and a disabled Join button with nothing discovered',
      (tester) async {
    await tester.pumpWidget(_harness(state));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    expect(find.text('Join Channel'), findsOneWidget);
    expect(find.text('No channels discovered yet.'), findsOneWidget);

    final joinButton =
        tester.widget<TcPrimaryButton>(find.widgetWithText(TcPrimaryButton, 'JOIN'));
    expect(joinButton.onPressed, isNull);
  });

  testWidgets('refresh re-requests discovered channels without throwing', (tester) async {
    await tester.pumpWidget(_harness(state));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    await tester.tap(find.text('↻ REFRESH'));
    await tester.pumpAndSettle();

    expect(find.text('Join Channel'), findsOneWidget);
  });

  testWidgets('cancel dismisses without joining anything', (tester) async {
    await tester.pumpWidget(_harness(state));
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    await tester.tap(find.text('CANCEL'));
    await tester.pumpAndSettle();

    expect(find.text('Join Channel'), findsNothing);
  });
}
