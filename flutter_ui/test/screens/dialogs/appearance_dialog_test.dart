import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/models/permissions.dart';
import 'package:flutter_ui/app_state.dart';
import 'package:flutter_ui/screens/dialogs/appearance_dialog.dart';
import 'package:flutter_ui/screens/dialogs/settings_dialog.dart';
import 'package:flutter_ui/theme/theme_code.dart';
import 'package:flutter_ui/theme/theme_presets.dart';
import 'package:flutter_ui/theme/theme_spec.dart';
import 'package:flutter_ui/widgets/tc_button.dart';
import 'package:flutter_ui/widgets/tc_checkbox.dart';
import 'package:flutter_ui/widgets/tc_color_field.dart';
import 'package:flutter_ui/widgets/tc_color_picker.dart';

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

  testWidgets('every color row reads as its human label, not its wire key', (tester) async {
    await openEditor(tester);

    for (final entry in tokenLabels.entries) {
      expect(find.text(entry.value), findsOneWidget, reason: entry.key);
    }
    expect(tokenLabels.keys.toSet(), TCSectionColors.tokenKeys.toSet());
    expect(find.text('App background'), findsOneWidget);
    expect(find.text('Accent (pressed)'), findsOneWidget);
    // The wire key is still what the field is keyed by, but never what it says.
    expect(find.text('bgPressed'), findsNothing);
    expect(find.text('accentPrimaryActive'), findsNothing);
    expect(find.byKey(tcColorInputKey('bgPressed')), findsOneWidget);
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
    // The editor shows the backend's own reason, and claims it: leaving it in
    // AppState.actionError would raise the app-wide snackbar over the top of
    // the dialog already saying it.
    expect(find.text('not found'), findsOneWidget);
    expect(state.actionError, isNull);
  });

  testWidgets('the settings dialog opens the appearance editor', (tester) async {
    backend.routes['GET /settings'] = {
      'propagation_enabled': false,
      'propagation_node_name': '',
      'propagation_storage_limit_mb': 512,
    };
    useTallSurface(tester);
    await tester.pumpWidget(_harness(state, (c) => showSettingsDialog(c, state)));
    await tester.tap(find.text('open'));
    await tester.pump();
    await settle(tester);

    await tester.dragUntilVisible(
      find.text('EDIT THEME…'),
      find.byType(ListView),
      const Offset(0, -80),
    );
    expect(find.text('APPEARANCE'), findsOneWidget);
    expect(find.text('Using the stock palette.'), findsOneWidget);

    await tester.tap(find.text('EDIT THEME…'));
    await settle(tester);

    expect(find.text('SCOPE'), findsOneWidget);
    expect(find.byKey(tcColorInputKey('accentPrimary')), findsOneWidget);
  });

  testWidgets('the settings summary counts style overrides', (tester) async {
    backend.routes['GET /settings'] = {
      'propagation_enabled': false,
      'propagation_node_name': '',
      'propagation_storage_limit_mb': 512,
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
      find.text('EDIT THEME…'),
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
    await tester.tap(find.descendant(
      of: find.byKey(appearanceDisplayFontRowKey),
      matching: find.text('INHERIT'),
    ));
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

  testWidgets('the shape rows write corners, avatars and the panel edge', (tester) async {
    await openEditor(tester);

    await tester.tap(find.descendant(
      of: find.byKey(appearanceCornerRowKey),
      matching: find.text('ROUND'),
    ));
    await tester.pump();
    await tester.tap(find.descendant(
      of: find.byKey(appearanceAvatarShapeRowKey),
      matching: find.text('CIRCLE'),
    ));
    await tester.pump();
    await tester.tap(find.descendant(
      of: find.byKey(appearancePanelEdgeRowKey),
      matching: find.text('PLAIN'),
    ));
    await tester.pump();
    await tester.tap(find.text('APPLY'));
    await settle(tester);

    expect((lastPostedTheme()['styles'] as Map)['base'], {
      'cornerRadius': 8.0,
      'avatarShape': 'circle',
      'panelEdge': 'plain',
    });

    final style = state.themeSpec.resolveStyle(TCSection.content);
    expect(style.cornerRadius, 8.0);
    expect(style.avatarShape, 'circle');
    expect(style.panelEdge, 'plain');
  });

  testWidgets('a section can round more than the base scope does', (tester) async {
    await openEditor(tester);

    await tester.tap(find.descendant(
      of: find.byKey(appearanceCornerRowKey),
      matching: find.text('SOFT'),
    ));
    await tester.pump();
    await tester.tap(find.text('SERVER RAIL'));
    await tester.pump();
    await tester.tap(find.descendant(
      of: find.byKey(appearanceCornerRowKey),
      matching: find.text('PILL'),
    ));
    await tester.pump();
    await tester.tap(find.text('APPLY'));
    await settle(tester);

    expect(state.themeSpec.resolveStyle(TCSection.serverRail).cornerRadius, 14.0);
    expect(state.themeSpec.resolveStyle(TCSection.content).cornerRadius, 4.0);
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

  testWidgets('SAVE AS… stores the current draft under a fresh name in one click',
      (tester) async {
    await openEditor(tester);

    await tester.enterText(find.byKey(tcColorInputKey('bgApp')), '#102030');
    await tester.pump();
    await tester.enterText(find.byKey(appearanceSaveAsFieldKey), 'Deep');
    await tester.pump();
    expect(find.text('Nothing saved yet — name the current draft below to keep it.'),
        findsOneWidget);
    expect(find.textContaining('Overwrites existing'), findsNothing);

    await tester.tap(find.text('SAVE AS…'));
    await settle(tester);

    final post = backend.requests.lastWhere((r) => r.path == '/ui_theme_library');
    final body = jsonDecode(post.body) as Map<String, dynamic>;
    expect(post.method, 'POST');
    expect(body['name'], 'Deep');
    expect(((body['theme'] as Map)['base'] as Map)['bgApp'], '#102030');
    expect(state.themeLibrary['Deep']!.base['bgApp'], const Color(0xFF102030));
    expect(find.text('Saved as Deep.'), findsOneWidget);
    // Saving under a name is not the same as wearing it.
    expect(state.themeSpec.isEmpty, isTrue);
  });

  testWidgets('SAVE AS… is disabled until the name field has something in it',
      (tester) async {
    await openEditor(tester);

    expect(tester.widget<TcGhostButton>(find.widgetWithText(TcGhostButton, 'SAVE AS…')).onPressed,
        isNull);

    await tester.enterText(find.byKey(appearanceSaveAsFieldKey), '  ');
    await tester.pump();
    expect(tester.widget<TcGhostButton>(find.widgetWithText(TcGhostButton, 'SAVE AS…')).onPressed,
        isNull);
  });

  testWidgets('a name already in the library warns and offers OVERWRITE', (tester) async {
    state.themeLibrary = {'Deep': ThemeSpec(base: {'bgApp': const Color(0xFF221100)})};
    await openEditor(tester);

    await tester.enterText(find.byKey(appearanceSaveAsFieldKey), ' Deep ');
    await tester.pump();

    expect(find.text('Overwrites existing "Deep".'), findsOneWidget);
    expect(find.text('OVERWRITE'), findsOneWidget);
    expect(find.text('SAVE AS…'), findsNothing);
  });

  testWidgets('OVERWRITE replaces the saved theme on the first click', (tester) async {
    state.themeLibrary = {'Deep': ThemeSpec(base: {'bgApp': const Color(0xFF221100)})};
    await openEditor(tester);

    await tester.enterText(find.byKey(tcColorInputKey('bgApp')), '#102030');
    await tester.pump();
    await tester.enterText(find.byKey(appearanceSaveAsFieldKey), 'Deep');
    await tester.pump();
    await tester.tap(find.text('OVERWRITE'));
    await settle(tester);

    final post = backend.requests.lastWhere((r) => r.path == '/ui_theme_library');
    final body = jsonDecode(post.body) as Map<String, dynamic>;
    expect(post.method, 'POST');
    expect(body['name'], 'Deep');
    expect(((body['theme'] as Map)['base'] as Map)['bgApp'], '#102030');
    expect(state.themeLibrary['Deep']!.base['bgApp'], const Color(0xFF102030));
    expect(state.themeLibrary.keys, ['Deep']);
    expect(find.text('Replaced Deep.'), findsOneWidget);
    // The name field is cleared, so the warning goes with it.
    expect(find.textContaining('Overwrites existing'), findsNothing);
  });

  testWidgets('applying a saved theme puts it in force without closing',
      (tester) async {
    final deep = ThemeSpec(
      base: {'bgApp': const Color(0xFF221100)},
      styles: {
        'base': {'glow': false},
      },
    );
    state.themeLibrary = {'Deep': deep};
    await openEditor(tester);

    await tester.tap(find.byKey(appearanceApplySavedKey('Deep')));
    await settle(tester);

    // Saved there and then -- no second click on the dialog's own APPLY.
    final theme = lastPostedTheme();
    expect((theme['base'] as Map)['bgApp'], '#221100');
    expect((theme['styles'] as Map)['base'], {'glow': false});
    expect(state.themeSpec, deep);
    // The draft follows it, and the editor is still open on it.
    expect(fieldText(tester, 'bgApp'), '#221100');
    expect(find.text('Appearance'), findsOneWidget);
    expect(find.text('Applied Deep.'), findsOneWidget);
  });

  testWidgets('a saved theme that will not save says so and stays undone',
      (tester) async {
    backend.routes.remove('POST /ui_theme');
    state.themeLibrary = {'Deep': ThemeSpec(base: {'bgApp': const Color(0xFF221100)})};
    await openEditor(tester);

    await tester.tap(find.byKey(appearanceApplySavedKey('Deep')));
    await settle(tester);

    expect(state.themeSpec.isEmpty, isTrue);
    expect(find.text('Appearance'), findsOneWidget);
    expect(find.text('Applied Deep.'), findsNothing);
    expect(find.text('not found'), findsOneWidget);
    expect(state.actionError, isNull);
  });

  testWidgets('deleting a saved theme takes a confirming second click', (tester) async {
    state.themeLibrary = {'Deep': ThemeSpec(base: {'bgApp': const Color(0xFF221100)})};
    backend.routes['POST /ui_theme_library/delete'] = {'ok': true};
    await openEditor(tester);

    await tester.tap(find.byKey(appearanceDeleteSavedKey('Deep')));
    await tester.pump();

    // The first click only arms it -- nothing has been asked of the backend.
    expect(find.text('SURE?'), findsOneWidget);
    expect(backend.requests.where((r) => r.path.endsWith('/delete')), isEmpty);
    expect(state.themeLibrary.keys, ['Deep']);

    await tester.tap(find.byKey(appearanceDeleteSavedKey('Deep')));
    await settle(tester);

    expect(backend.requests.last.path, '/ui_theme_library/delete');
    expect(state.themeLibrary, isEmpty);
    expect(find.text('Deleted Deep.'), findsOneWidget);
  });

  testWidgets('an armed delete reverts on its own after the confirm window',
      (tester) async {
    state.themeLibrary = {'Deep': ThemeSpec(base: {'bgApp': const Color(0xFF221100)})};
    await openEditor(tester);

    await tester.tap(find.byKey(appearanceDeleteSavedKey('Deep')));
    await tester.pump();
    expect(find.text('SURE?'), findsOneWidget);
    expect(
      tester.widget<TcGhostButton>(find.byKey(appearanceDeleteSavedKey('Deep'))).accent,
      isNotNull,
    );

    await tester.pump(appearanceDeleteConfirmWindow + const Duration(milliseconds: 100));
    await tester.pump();

    expect(find.text('SURE?'), findsNothing);
    expect(find.byKey(appearanceDeleteSavedKey('Deep')), findsOneWidget);
    expect(state.themeLibrary.keys, ['Deep']);
  });

  testWidgets('clicking anything else in the editor disarms the delete', (tester) async {
    state.themeLibrary = {'Deep': ThemeSpec(base: {'bgApp': const Color(0xFF221100)})};
    await openEditor(tester);

    await tester.tap(find.byKey(appearanceDeleteSavedKey('Deep')));
    await tester.pump();
    expect(find.text('SURE?'), findsOneWidget);

    await tester.tap(find.text('CONTENT'));
    await tester.pump();

    expect(find.text('SURE?'), findsNothing);
    expect(state.themeLibrary.keys, ['Deep']);
  });

  testWidgets('arming one row disarms the other', (tester) async {
    final spec = ThemeSpec(base: {'bgApp': const Color(0xFF221100)});
    state.themeLibrary = {'Deep': spec, 'Shallow': spec};
    await openEditor(tester);

    await tester.tap(find.byKey(appearanceDeleteSavedKey('Deep')));
    await tester.pump();
    await tester.tap(find.byKey(appearanceDeleteSavedKey('Shallow')));
    await tester.pump();

    expect(find.text('SURE?'), findsOneWidget);
    expect(
      tester.widget<TcGhostButton>(find.byKey(appearanceDeleteSavedKey('Shallow'))).label,
      'SURE?',
    );

    await tester.pump(appearanceDeleteConfirmWindow + const Duration(milliseconds: 100));
  });

  testWidgets('SHARE stages the theme for the compose box and sends nothing',
      (tester) async {
    final spec = ThemeSpec(base: {'bgApp': const Color(0xFF221100)});
    state.themeLibrary = {'Deep': spec};
    state.selectedChannelHash = 'chan1';
    backend.routes['POST /channels/chan1/messages'] = {'ok': true};
    backend.routes['GET /channels/chan1/messages'] = <dynamic>[];
    await openEditor(tester);

    await tester.tap(find.byKey(appearanceShareSavedKey('Deep')));
    await settle(tester);

    final staged = state.pendingThemeShare;
    expect(staged, isNotNull);
    expect(staged!.name, 'Deep');
    final decoded = decodeThemeCode(staged.code);
    expect(decoded!.name, 'Deep');
    expect(decoded.spec, spec);
    // Nothing goes out until the user sends the draft themselves.
    expect(backend.requests.where((r) => r.path == '/channels/chan1/messages'), isEmpty);
    // The editor is out of the way, and the staged share survives one read.
    expect(find.text('Appearance'), findsNothing);
    expect(state.consumePendingThemeShare()!.name, 'Deep');
    expect(state.pendingThemeShare, isNull);
  });

  testWidgets('SHARE stays available with no channel open and no send permission',
      (tester) async {
    state.themeLibrary = {'Deep': ThemeSpec.empty};
    state.permissionsByChannel['chan1'] = const ChannelPermissions(
      invite: false,
      kick: false,
      manageRoles: false,
      manageChannel: false,
      sendMessage: false,
      voiceChat: false,
    );
    await openEditor(tester);

    expect(
      tester.widget<TcGhostButton>(find.byKey(appearanceShareSavedKey('Deep'))).onPressed,
      isNotNull,
    );

    await tester.tap(find.byKey(appearanceShareSavedKey('Deep')));
    await settle(tester);

    expect(state.pendingThemeShare!.name, 'Deep');
  });

  testWidgets('SHARE closes the settings dialog under the editor too', (tester) async {
    backend.routes['GET /settings'] = {
      'propagation_enabled': false,
      'propagation_node_name': '',
      'propagation_storage_limit_mb': 512,
    };
    state.themeLibrary = {'Deep': ThemeSpec.empty};
    useTallSurface(tester);
    await tester.pumpWidget(_harness(state, (c) => showSettingsDialog(c, state)));
    await tester.tap(find.text('open'));
    await tester.pump();
    await settle(tester);

    await tester.dragUntilVisible(
      find.text('EDIT THEME…'),
      find.byType(ListView),
      const Offset(0, -80),
    );
    await tester.tap(find.text('EDIT THEME…'));
    await settle(tester);

    await tester.tap(find.byKey(appearanceShareSavedKey('Deep')));
    await settle(tester);

    expect(find.text('Appearance'), findsNothing);
    expect(find.text('Settings'), findsNothing);
    expect(state.pendingThemeShare!.name, 'Deep');
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

  testWidgets('the preset matching the draft is highlighted', (tester) async {
    state.themeSpec = themePresets.firstWhere((p) => p.name == 'Ember').spec;
    await openEditor(tester);

    TcChoiceRow presetRow() =>
        tester.widget<TcChoiceRow>(find.byKey(appearancePresetRowKey));
    expect(presetRow().value, 'Ember');

    await tester.tap(find.text('TRENCH'));
    await tester.pump();
    expect(presetRow().value, 'Trench');

    await tester.enterText(find.byKey(tcColorInputKey('bgApp')), '#102030');
    await tester.pump();
    expect(presetRow().value, '');
  });

  testWidgets('a row swatch opens the picker and USE commits the color', (tester) async {
    await openEditor(tester);

    await tester.tap(find.byKey(tcColorSwatchKey('bgApp')));
    await settle(tester);
    expect(find.text('App background'), findsNWidgets(2));
    expect(
      tester.widget<TextField>(find.byKey(tcPickerHexKey)).controller!.text,
      encodeThemeColor(TCSectionColors.stock.bgApp),
    );

    await tester.enterText(find.byKey(tcPickerHexKey), '#102030');
    await tester.pump();
    await tester.tap(find.byKey(tcPickerUseKey));
    await settle(tester);

    expect(fieldText(tester, 'bgApp'), '#102030');

    await tester.tap(find.text('APPLY'));
    await settle(tester);
    expect((lastPostedTheme()['base'] as Map)['bgApp'], '#102030');
  });

  testWidgets('cancelling the picker leaves the row alone', (tester) async {
    await openEditor(tester);

    await tester.tap(find.byKey(tcColorSwatchKey('bgApp')));
    await settle(tester);
    await tester.enterText(find.byKey(tcPickerHexKey), '#102030');
    await tester.pump();
    await tester.tap(find.byKey(tcPickerCancelKey));
    await settle(tester);

    expect(find.byKey(tcPickerHexKey), findsNothing);
    expect(fieldText(tester, 'bgApp'), encodeThemeColor(TCSectionColors.stock.bgApp));

    await tester.tap(find.text('APPLY'));
    await settle(tester);
    expect(lastPostedTheme()['base'], isEmpty);
  });

  testWidgets('a saved theme matching the draft is marked ACTIVE', (tester) async {
    final mine = ThemeSpec(base: {'bgApp': const Color(0xFF102030)});
    backend.routes['GET /ui_theme_library'] = {
      'themes': {'mine': mine.toJson()},
    };
    await state.loadThemeLibrary();
    state.themeSpec = mine;
    await openEditor(tester);

    expect(find.text('ACTIVE'), findsOneWidget);

    await tester.enterText(find.byKey(tcColorInputKey('bgApp')), '#445566');
    await tester.pump();
    expect(find.text('ACTIVE'), findsNothing);
  });

  testWidgets('only one saved row wears the ACTIVE tag when specs are duplicated',
      (tester) async {
    final mine = ThemeSpec(base: {'bgApp': const Color(0xFF102030)});
    state.themeLibrary = {'alpha': mine, 'beta': mine};
    state.themeSpec = mine;
    await openEditor(tester);

    Finder rowOf(String name) =>
        find.ancestor(of: find.text(name), matching: find.byType(Row)).first;

    expect(find.text('ACTIVE'), findsOneWidget);
    expect(find.descendant(of: rowOf('alpha'), matching: find.text('ACTIVE')),
        findsOneWidget);

    // Applying the duplicate moves the tag rather than lighting both rows.
    await tester.tap(find.byKey(appearanceApplySavedKey('beta')));
    await settle(tester);
    expect(find.text('ACTIVE'), findsOneWidget);
    expect(find.descendant(of: rowOf('beta'), matching: find.text('ACTIVE')),
        findsOneWidget);

    // Editing the draft still clears every tag.
    await tester.enterText(find.byKey(tcColorInputKey('bgApp')), '#445566');
    await tester.pump();
    expect(find.text('ACTIVE'), findsNothing);
  });

  testWidgets('SAVE AS… makes the name it wrote the active one', (tester) async {
    // A theme already holding what the draft is about to become: the tag has
    // to follow the name just saved, not every row that matches.
    state.themeLibrary = {'zzz': ThemeSpec(base: {'bgApp': const Color(0xFF102030)})};
    await openEditor(tester);

    await tester.enterText(find.byKey(tcColorInputKey('bgApp')), '#102030');
    await tester.pump();
    await tester.enterText(find.byKey(appearanceSaveAsFieldKey), 'Deep');
    await tester.pump();
    await tester.tap(find.text('SAVE AS…'));
    await settle(tester);

    expect(find.text('ACTIVE'), findsOneWidget);
    expect(
      find.descendant(
        of: find.ancestor(of: find.text('Deep'), matching: find.byType(Row)).first,
        matching: find.text('ACTIVE'),
      ),
      findsOneWidget,
    );
  });

  testWidgets('the name field stops at the length the library accepts', (tester) async {
    await openEditor(tester);

    await tester.enterText(find.byKey(appearanceSaveAsFieldKey), 'x' * 200);
    await tester.pump();

    final field = tester.widget<TextField>(
      find.descendant(of: find.byKey(appearanceSaveAsFieldKey), matching: find.byType(TextField)),
    );
    expect(field.controller!.text.length, maxThemeNameLength);
  });

  testWidgets('a refused save shows the reason in the editor, not just a snackbar',
      (tester) async {
    backend.routes.remove('POST /ui_theme_library');
    await openEditor(tester);

    await tester.enterText(find.byKey(appearanceSaveAsFieldKey), 'Deep');
    await tester.pump();
    await tester.tap(find.text('SAVE AS…'));
    await settle(tester);

    expect(find.text('not found'), findsOneWidget);
    expect(state.actionError, isNull);
    expect(find.text('Saved as Deep.'), findsNothing);
    expect(state.themeLibrary, isEmpty);
    // The name is kept so the save can be retried.
    expect(find.text('Appearance'), findsOneWidget);
  });

  testWidgets('re-saving a name that already holds this exact draft is not an overwrite',
      (tester) async {
    state.themeLibrary = {'Deep': ThemeSpec.empty};
    await openEditor(tester);

    await tester.enterText(find.byKey(appearanceSaveAsFieldKey), 'Deep');
    await tester.pump();

    expect(find.textContaining('Overwrites existing'), findsNothing);
    expect(find.text('SAVE AS…'), findsOneWidget);
    expect(find.text('OVERWRITE'), findsNothing);

    await tester.tap(find.text('SAVE AS…'));
    await settle(tester);
    expect(find.text('Saved as Deep.'), findsOneWidget);
    expect(state.themeLibrary.keys, ['Deep']);

    // Once the draft differs from what is stored, the warning is back.
    await tester.enterText(find.byKey(appearanceSaveAsFieldKey), 'Deep');
    await tester.pump();
    await tester.enterText(find.byKey(tcColorInputKey('bgApp')), '#102030');
    await tester.pump();
    expect(find.text('Overwrites existing "Deep".'), findsOneWidget);
    expect(find.text('OVERWRITE'), findsOneWidget);
  });

  testWidgets('a saved theme name renders in its own colors', (tester) async {
    final blue = ThemeSpec(base: {
      'textPrimary': const Color(0xFFAADDFF),
      'bgApp': const Color(0xFF001020),
    });
    backend.routes['GET /ui_theme_library'] = {
      'themes': {'sky': blue.toJson()},
    };
    await state.loadThemeLibrary();
    await openEditor(tester);

    final nameText = tester.widget<Text>(find.text('sky'));
    expect(nameText.style?.color, const Color(0xFFAADDFF));
  });
}
