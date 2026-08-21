// AppState.actionError has exactly one app-wide surface: the snackbar in
// main_window.dart. It is for failures with no UI of their own -- a dialog
// that shows the reason itself claims it with takeActionError(), and the
// snackbar must then stay away rather than repeating it over the dialog and
// lingering into whatever the reader does next.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/app_state.dart';
import 'package:flutter_ui/screens/main_window/main_window.dart';

import '../fake_backend.dart';

Future<AppState> _shell(WidgetTester tester, FakeBackend backend) async {
  tester.view.physicalSize = const Size(1280, 800);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.reset);

  final state = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
  addTearDown(state.dispose);
  state.loading = false;
  await tester.pumpWidget(MaterialApp(
    home: Scaffold(body: MainWindow(state: state)),
  ));
  await tester.pumpAndSettle();
  return state;
}

void main() {
  testWidgets('a background failure surfaces in the snackbar', (tester) async {
    final state = await _shell(tester, FakeBackend());

    state.actionError = 'Message was not sent.';
    state.notifyListeners();
    await tester.pumpAndSettle();

    expect(find.text('Message was not sent.'), findsOneWidget);
  });

  testWidgets('the snackbar is bounded, floating and TC-styled', (tester) async {
    final state = await _shell(tester, FakeBackend());

    state.actionError = 'Message was not sent.';
    state.notifyListeners();
    await tester.pumpAndSettle();

    final bar = tester.widget<SnackBar>(find.byType(SnackBar));
    expect(bar.behavior, SnackBarBehavior.floating);
    expect(bar.duration, const Duration(seconds: 4));
    expect(bar.backgroundColor, state.themeSpec.resolveBase().bgSurfaceRaised);
    final shape = bar.shape! as RoundedRectangleBorder;
    expect(shape.borderRadius, BorderRadius.zero);
    expect(shape.side.color, state.themeSpec.resolveBase().statusDanger);
  });

  testWidgets('an error a dialog claimed never reaches the snackbar', (tester) async {
    final state = await _shell(tester, FakeBackend());

    state.actionError = 'Could not create channel.';
    state.notifyListeners();
    // What a dialog does in the continuation of its own failed await, before
    // the frame the snackbar would be shown in.
    expect(state.takeActionError(), 'Could not create channel.');
    await tester.pumpAndSettle();

    expect(find.byType(SnackBar), findsNothing);
  });

  testWidgets('a claimed error does not mute the next real one', (tester) async {
    final state = await _shell(tester, FakeBackend());

    state.actionError = 'Could not create channel.';
    state.notifyListeners();
    state.takeActionError();
    await tester.pumpAndSettle();
    expect(find.byType(SnackBar), findsNothing);

    state.actionError = 'Message was not sent.';
    state.notifyListeners();
    await tester.pumpAndSettle();

    expect(find.text('Message was not sent.'), findsOneWidget);
  });
}
