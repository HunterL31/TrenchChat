// The one-time question about the public address echo. It is a disclosure, so
// the rules it has to keep are: asked only when a pair is actually stuck for
// want of an address, asked once per run whichever way it is answered, and
// nothing sent anywhere until the user says Enable.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/events.dart';
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

FakeBackend _backend() {
  final backend = FakeBackend();
  backend.routes['POST /upgrade/stun'] = {
    'ok': true,
    'enabled': true,
    'servers': ['stun.example.com:3478'],
  };
  return backend;
}

void main() {
  testWidgets('nothing is asked while no pair is stuck', (tester) async {
    await _shell(tester, _backend());

    expect(find.textContaining('address-echo service'), findsNothing);
  });

  testWidgets('the prompt appears on the transition, and says what it costs',
      (tester) async {
    final state = await _shell(tester, _backend());

    state.applyEvent(const DirectAddressNeededEvent(true));
    await tester.pumpAndSettle();

    expect(find.textContaining('address-echo service (STUN)'), findsOneWidget);
    // What the server learns, and what it does not.
    expect(
        find.textContaining(
            'tells that server this machine’s address and that it asked'),
        findsOneWidget);
    expect(find.textContaining('no messages, no channels, and nobody you talk '
        'to'), findsOneWidget);
    // And that declining costs nothing but speed.
    expect(find.textContaining('keeps talking over the mesh'), findsOneWidget);
    expect(find.text('ENABLE'), findsOneWidget);
    expect(find.text('NOT NOW'), findsOneWidget);
  });

  testWidgets('Enable turns it on through its own endpoint', (tester) async {
    final backend = _backend();
    final state = await _shell(tester, backend);
    state.applyEvent(const DirectAddressNeededEvent(true));
    await tester.pumpAndSettle();

    await tester.tap(find.text('ENABLE'));
    await tester.pumpAndSettle();

    expect(backend.requests.where((r) => r.path == '/upgrade/stun'
        && r.method == 'POST').length, 1);
    expect(state.stun.enabled, isTrue);
    expect(find.text('ENABLE'), findsNothing);
  });

  testWidgets('Not now sends nothing and is not asked again this run',
      (tester) async {
    final backend = _backend();
    final state = await _shell(tester, backend);
    state.applyEvent(const DirectAddressNeededEvent(true));
    await tester.pumpAndSettle();

    await tester.tap(find.text('NOT NOW'));
    await tester.pumpAndSettle();

    expect(backend.requests.where((r) => r.path == '/upgrade/stun'), isEmpty);
    expect(find.textContaining('address-echo service'), findsNothing);

    // The same news again, and the answer still stands.
    state.applyEvent(const DirectAddressNeededEvent(true));
    await tester.pumpAndSettle();
    expect(find.textContaining('address-echo service'), findsNothing);
  });

  testWidgets('it is asked once even as the news keeps arriving',
      (tester) async {
    final state = await _shell(tester, _backend());

    state.applyEvent(const DirectAddressNeededEvent(true));
    await tester.pump();
    state.applyEvent(const DirectAddressNeededEvent(true));
    await tester.pumpAndSettle();

    expect(find.text('ENABLE'), findsOneWidget);
  });
}
