import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/app_state.dart';
import 'package:flutter_ui/screens/dialogs/appearance_dialog.dart';
import 'package:flutter_ui/theme/theme_presets.dart';
import 'package:flutter_ui/theme/theme_spec.dart';

import '../../fake_backend.dart';

Widget _harness(AppState state, void Function(BuildContext) open) {
  return MaterialApp(
    home: Scaffold(
      body: Builder(
        builder: (context) => ElevatedButton(
          onPressed: () => open(context),
          child: const Text('open'),
        ),
      ),
    ),
  );
}

void main() {
  late FakeBackend backend;
  late AppState state;

  setUp(() {
    backend = FakeBackend();
    state = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
  });

  tearDown(() {
    state.dispose();
  });

  Future<void> openEditor(WidgetTester tester) async {
    tester.view.physicalSize = const Size(1200, 2000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    await tester.pumpWidget(_harness(state, (c) => showAppearanceDialog(c, state)));
    await tester.tap(find.text('open'));
    await tester.pump();
    await settle(tester);
  }

  Map<String, dynamic> lastPostedTheme() {
    final post = backend.requests.lastWhere(
      (r) => r.method == 'POST' && r.path == '/ui_theme',
    );
    return (jsonDecode(post.body) as Map<String, dynamic>)['theme'] as Map<String, dynamic>;
  }

  test('the preset list offers the stock look first, then Ember', () {
    expect(themePresets.first.name, 'Trench');
    expect(themePresets.first.spec.isEmpty, isTrue);
    expect(themePresets.map((p) => p.name), contains('Ember'));
    final ember = themePresets.firstWhere((p) => p.name == 'Ember');
    expect(ember.spec.base['textPrimary'], isNotNull);
    expect(ember.spec.base['accentPrimary'], isNotNull);
  });

  testWidgets('applying Ember saves its base palette', (tester) async {
    await openEditor(tester);

    await tester.tap(find.text('EMBER'));
    await tester.pump();
    await tester.tap(find.text('APPLY'));
    await settle(tester);

    final ember = themePresets.firstWhere((p) => p.name == 'Ember');
    final posted = lastPostedTheme();
    expect((posted['base'] as Map)['textPrimary'],
        encodeThemeColor(ember.spec.base['textPrimary']!));
    expect(state.themeSpec.base, ember.spec.base);
  });

  testWidgets('applying Trench after Ember restores the stock palette', (tester) async {
    state.themeSpec = themePresets.firstWhere((p) => p.name == 'Ember').spec;
    await openEditor(tester);

    await tester.tap(find.text('TRENCH'));
    await tester.pump();
    await tester.tap(find.text('APPLY'));
    await settle(tester);

    expect(lastPostedTheme()['base'], isEmpty);
    expect(state.themeSpec.isEmpty, isTrue);
  });

  testWidgets('a preset replaces known overrides and keeps unknown sections', (tester) async {
    state.themeSpec = ThemeSpec.fromJson({
      'version': 1,
      'base': {'bgApp': '#101010'},
      'sections': {
        'content': {'textPrimary': '#00ff00'},
        'holoDeck': {'textPrimary': '#ff00ff'},
      },
    });
    await openEditor(tester);

    await tester.tap(find.text('EMBER'));
    await tester.pump();
    await tester.tap(find.text('APPLY'));
    await settle(tester);

    final posted = lastPostedTheme();
    expect((posted['base'] as Map)['bgApp'], isNot('#101010'));
    expect((posted['sections'] as Map)['content'], isNull);
    expect(((posted['sections'] as Map)['holoDeck'] as Map)['textPrimary'], '#ff00ff');
  });
}
