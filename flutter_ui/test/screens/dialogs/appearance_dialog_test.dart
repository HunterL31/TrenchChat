import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/app_state.dart';
import 'package:flutter_ui/screens/dialogs/appearance_dialog.dart';
import 'package:flutter_ui/screens/dialogs/settings_dialog.dart';
import 'package:flutter_ui/theme/theme_spec.dart';
import 'package:flutter_ui/widgets/tc_color_field.dart';

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
    state.meHashHex = 'a9f13c02e7d84b119876543210fedcba';
    state.meDisplayName = 'operator';
  });

  tearDown(() {
    state.dispose();
  });

  /// A viewport tall enough for the whole editor, so every control is
  /// hit-testable without scrolling the modal itself.
  void useTallSurface(WidgetTester tester) {
    tester.view.physicalSize = const Size(1200, 2000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
  }

  Future<void> openEditor(WidgetTester tester) async {
    useTallSurface(tester);
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

  String fieldText(WidgetTester tester, String token) =>
      tester.widget<TextField>(find.byKey(tcColorInputKey(token))).controller!.text;

  testWidgets('renders every token for the base scope', (tester) async {
    await openEditor(tester);

    expect(find.text('Appearance'), findsOneWidget);
    expect(find.text('BASE'), findsOneWidget);
    expect(find.text('SERVER RAIL'), findsOneWidget);
    expect(find.text('DIALOGS'), findsOneWidget);
    for (final token in TCSectionColors.tokenKeys) {
      expect(find.byKey(tcColorInputKey(token)), findsOneWidget, reason: token);
    }
    expect(find.text('No overrides in this scope — every color is inherited.'), findsOneWidget);
  });

  testWidgets('editing a base token saves it and adopts the new spec', (tester) async {
    await openEditor(tester);

    await tester.enterText(find.byKey(tcColorInputKey('bgApp')), '#102030');
    await tester.pump();
    await tester.tap(find.text('APPLY'));
    await settle(tester);

    final theme = lastPostedTheme();
    expect((theme['base'] as Map)['bgApp'], '#102030');
    expect(state.themeSpec.base['bgApp'], const Color(0xFF102030));
    expect(find.text('Appearance'), findsNothing);
  });

  testWidgets('a section scope writes into that section only', (tester) async {
    await openEditor(tester);

    await tester.tap(find.text('CONTENT'));
    await tester.pump();
    await tester.enterText(find.byKey(tcColorInputKey('bgSurface')), '#00ff88');
    await tester.pump();
    await tester.tap(find.text('APPLY'));
    await settle(tester);

    final theme = lastPostedTheme();
    expect(theme['base'], isEmpty);
    expect(((theme['sections'] as Map)['content'] as Map)['bgSurface'], '#00ff88');
    expect(state.themeSpec.resolve(TCSection.content).bgSurface, const Color(0xFF00FF88));
    expect(state.themeSpec.resolve(TCSection.topBar).bgSurface,
        TCSectionColors.stock.bgSurface);
  });

  testWidgets('clearing an override reverts the token to inherited', (tester) async {
    state.themeSpec = ThemeSpec(base: {'bgApp': const Color(0xFF102030)});
    await openEditor(tester);

    expect(find.text('1 override in this scope.'), findsOneWidget);
    expect(find.byTooltip('Clear override'), findsOneWidget);

    await tester.tap(find.byTooltip('Clear override'));
    await tester.pump();
    expect(fieldText(tester, 'bgApp'), encodeThemeColor(TCSectionColors.stock.bgApp));

    await tester.tap(find.text('APPLY'));
    await settle(tester);

    expect(lastPostedTheme()['base'], isEmpty);
    expect(state.themeSpec.base, isEmpty);
  });

  testWidgets('invalid hex is ignored, reverts on blur, and saves nothing', (tester) async {
    await openEditor(tester);

    await tester.enterText(find.byKey(tcColorInputKey('bgApp')), 'not-a-color');
    await tester.pump();
    await tester.tap(find.byKey(tcColorInputKey('bgSurface')));
    await tester.pump();

    expect(fieldText(tester, 'bgApp'), encodeThemeColor(TCSectionColors.stock.bgApp));

    await tester.tap(find.text('APPLY'));
    await settle(tester);

    expect(lastPostedTheme()['base'], isEmpty);
    expect(state.themeSpec.base, isEmpty);
  });

  testWidgets('reset all clears known scopes and keeps unknown ones', (tester) async {
    state.themeSpec = ThemeSpec.fromJson({
      'version': 1,
      'base': {'bgApp': '#101010'},
      'sections': {
        'content': {'textPrimary': '#00ff00'},
        'holoDeck': {'textPrimary': '#ff00ff'},
      },
    });
    await openEditor(tester);

    await tester.tap(find.text('RESET ALL'));
    await tester.pump();
    await tester.tap(find.text('APPLY'));
    await settle(tester);

    final theme = lastPostedTheme();
    expect(theme['base'], isEmpty);
    expect((theme['sections'] as Map)['content'], isNull);
    expect(((theme['sections'] as Map)['holoDeck'] as Map)['textPrimary'], '#ff00ff');
  });

  testWidgets('a save failure keeps the old theme and shows the error', (tester) async {
    backend.routes.remove('POST /ui_theme');
    await openEditor(tester);

    await tester.enterText(find.byKey(tcColorInputKey('bgApp')), '#102030');
    await tester.pump();
    await tester.tap(find.text('APPLY'));
    await settle(tester);

    expect(find.text('Appearance'), findsOneWidget);
    expect(state.themeSpec.base, isEmpty);
    expect(state.actionError, isNotNull);
    expect(find.text(state.actionError!), findsOneWidget);
  });

  testWidgets('the settings dialog opens the appearance editor', (tester) async {
    backend.routes['GET /settings'] = {
      'propagation_enabled': false,
      'propagation_node_name': '',
      'propagation_storage_limit_mb': 512,
      'channel_filter_mode': 'all',
      'channel_filter_hashes': <String>[],
      'outbound_propagation_node': '',
    };
    useTallSurface(tester);
    await tester.pumpWidget(_harness(state, (c) => showSettingsDialog(c, state)));
    await tester.tap(find.text('open'));
    await tester.pump();
    await settle(tester);

    await tester.dragUntilVisible(
      find.text('EDIT COLORS…'),
      find.byType(ListView),
      const Offset(0, -80),
    );
    expect(find.text('APPEARANCE'), findsOneWidget);
    expect(find.text('Using the stock palette.'), findsOneWidget);

    await tester.tap(find.text('EDIT COLORS…'));
    await settle(tester);

    expect(find.text('SCOPE'), findsOneWidget);
    expect(find.byKey(tcColorInputKey('accentPrimary')), findsOneWidget);
  });

  testWidgets('the settings summary counts style overrides', (tester) async {
    backend.routes['GET /settings'] = {
      'propagation_enabled': false,
      'propagation_node_name': '',
      'propagation_storage_limit_mb': 512,
      'channel_filter_mode': 'all',
      'channel_filter_hashes': <String>[],
      'outbound_propagation_node': '',
    };
    state.themeSpec = ThemeSpec.fromJson({
      'version': 1,
      'styles': {
        'base': {'glow': false},
      },
    });
    useTallSurface(tester);
    await tester.pumpWidget(_harness(state, (c) => showSettingsDialog(c, state)));
    await tester.tap(find.text('open'));
    await tester.pump();
    await settle(tester);

    await tester.dragUntilVisible(
      find.text('EDIT COLORS…'),
      find.byType(ListView),
      const Offset(0, -80),
    );
    expect(find.text('1 style customized across 1 scope.'), findsOneWidget);
  });

  testWidgets('a section scope writes its style keys into styles', (tester) async {
    await openEditor(tester);

    await tester.tap(find.text('CONTENT'));
    await tester.pump();
    await tester.tap(find.text('110%'));
    await tester.pump();
    await tester.tap(find.text('Accent glow'));
    await tester.pump();
    await tester.tap(find.text('APPLY'));
    await settle(tester);

    final theme = lastPostedTheme();
    expect(theme['base'], isEmpty);
    expect((theme['styles'] as Map)['content'], {'textScale': 1.1, 'glow': false});

    final content = state.themeSpec.resolveStyle(TCSection.content);
    expect(content.textScale, 1.1);
    expect(content.glow, isFalse);
    expect(state.themeSpec.resolveStyle(TCSection.topBar), TCSectionStyle.stock);
  });

  testWidgets('the base scope writes a display font every section inherits', (tester) async {
    await openEditor(tester);

    await tester.tap(find.text('PLEX MONO'));
    await tester.pump();
    await tester.tap(find.text('APPLY'));
    await settle(tester);

    expect((lastPostedTheme()['styles'] as Map)['base'], {'displayFont': 'IBM Plex Mono'});
    expect(state.themeSpec.resolveStyle(TCSection.dialogs).displayFont, 'IBM Plex Mono');
  });

  testWidgets('inheriting again clears the scope style, reset section clears the rest',
      (tester) async {
    state.themeSpec = ThemeSpec.fromJson({
      'version': 1,
      'styles': {
        'base': {'glow': false},
        'content': {'textScale': 1.25, 'displayFont': 'IBM Plex Mono'},
      },
    });
    await openEditor(tester);

    await tester.tap(find.text('CONTENT'));
    await tester.pump();
    // The scope's own font override goes back to the inherited value...
    await tester.tap(find.text('INHERIT').last);
    await tester.pump();
    expect(find.text('RESET SECTION'), findsOneWidget);

    await tester.tap(find.text('RESET SECTION'));
    await tester.pump();
    await tester.tap(find.text('APPLY'));
    await settle(tester);

    final styles = lastPostedTheme()['styles'] as Map;
    expect(styles['content'], isNull);
    expect(styles['base'], {'glow': false}, reason: 'the base scope is untouched');
    expect(state.themeSpec.resolveStyle(TCSection.content).textScale, 1.0);
    expect(state.themeSpec.resolveStyle(TCSection.content).glow, isFalse);
  });

  testWidgets('a text scale the editor does not offer selects nothing and survives',
      (tester) async {
    state.themeSpec = ThemeSpec.fromJson({
      'version': 1,
      'styles': {
        'base': {'textScale': 1.35},
      },
    });
    await openEditor(tester);

    expect(find.text('135%'), findsNothing);
    await tester.tap(find.text('APPLY'));
    await settle(tester);

    expect((lastPostedTheme()['styles'] as Map)['base'], {'textScale': 1.35});
  });

  testWidgets('reset all clears style overrides too', (tester) async {
    state.themeSpec = ThemeSpec.fromJson({
      'version': 1,
      'styles': {
        'base': {'glow': false},
        'content': {'textScale': 1.1},
        'holoDeck': {'glow': false},
      },
    });
    await openEditor(tester);

    await tester.tap(find.text('RESET ALL'));
    await tester.pump();
    await tester.tap(find.text('APPLY'));
    await settle(tester);

    final styles = lastPostedTheme()['styles'] as Map;
    expect(styles['base'], isNull);
    expect(styles['content'], isNull);
    expect(styles['holoDeck'], {'glow': false});
  });
}
