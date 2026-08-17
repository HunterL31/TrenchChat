// Full-window golden at the mockup's own card size (1440x900), so it can be
// compared directly against Main Window Directions.dc.html options 1a/1b.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/app_state.dart';
import 'package:flutter_ui/screens/main_window/main_window.dart';
import 'package:flutter_ui/theme/app_theme.dart';

import 'fixtures.dart';
import 'test_fonts.dart';

void main() {
  setUpAll(loadTestFonts);

  testWidgets('main window matches the 1a/1b composite at 1440x900', (tester) async {
    tester.view.physicalSize = const Size(1440, 900);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    final state = AppState(baseUrl: 'http://127.0.0.1:65500');
    populateFixtureState(state);
    addTearDown(state.dispose);

    await tester.pumpWidget(MaterialApp(
      theme: buildAppTheme(),
      debugShowCheckedModeBanner: false,
      home: Scaffold(body: MainWindow(state: state)),
    ));
    await tester.pump();
    await tester.pump();

    await expectLater(
      find.byType(MainWindow),
      matchesGoldenFile('goldens/main_window_1440x900.png'),
    );
  });

  testWidgets('compact shell at phone size, closed and with the drawer open',
      (tester) async {
    tester.view.physicalSize = const Size(390, 844);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);

    final state = AppState(baseUrl: 'http://127.0.0.1:65500');
    populateFixtureState(state);
    addTearDown(state.dispose);

    await tester.pumpWidget(MaterialApp(
      theme: buildAppTheme(),
      debugShowCheckedModeBanner: false,
      home: Scaffold(body: MainWindow(state: state)),
    ));
    await tester.pump();
    await tester.pump();

    await expectLater(
      find.byType(MainWindow),
      matchesGoldenFile('goldens/main_window_390x844.png'),
    );

    await tester.tap(find.byTooltip('Channels'));
    await tester.pumpAndSettle();
    await expectLater(
      find.byType(MainWindow),
      matchesGoldenFile('goldens/main_window_390x844_drawer.png'),
    );
  });
}
