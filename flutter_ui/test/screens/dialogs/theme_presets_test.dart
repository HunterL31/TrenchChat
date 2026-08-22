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

  /// The presets that imitate a chat client, as opposed to the stock look
  /// and its phosphor variant.
  const clientPresets = ['Discord', 'Slack', 'Telegram', 'WhatsApp'];

  test('the preset list offers the stock look first, then Ember', () {
    expect(themePresets.first.name, 'Trench');
    expect(themePresets.first.spec.isEmpty, isTrue);
    expect(themePresets.map((p) => p.name), contains('Ember'));
    final ember = themePresets.firstWhere((p) => p.name == 'Ember');
    expect(ember.spec.base['textPrimary'], isNotNull);
    expect(ember.spec.base['accentPrimary'], isNotNull);
  });

  test('every preset name is offered once', () {
    final names = themePresets.map((p) => p.name).toList();
    expect(names.toSet().length, names.length);
    expect(names, containsAll(clientPresets));
  });

  test('a client preset leaves no stock token showing through', () {
    for (final name in clientPresets) {
      final spec = themePresets.firstWhere((p) => p.name == name).spec;
      for (final token in TCSectionColors.tokenKeys) {
        expect(spec.base[token], isNotNull, reason: '$name is missing $token');
      }
    }
  });

  test('a client preset re-shapes the app, not just its colors', () {
    for (final name in clientPresets) {
      final spec = themePresets.firstWhere((p) => p.name == name).spec;
      final style = spec.resolveBaseStyle();
      expect(style.cornerRadius, greaterThan(0), reason: '$name rounds nothing');
      expect(style.panelEdge, TCSectionStyle.panelPlain, reason: '$name keeps the notch');
      expect(style.glow, isFalse, reason: '$name still glows');
      expect(style.displayFont, 'IBM Plex Mono', reason: '$name still uses VT323');
    }
  });

  test('Discord is round, blurple and circular', () {
    final discord = themePresets.firstWhere((p) => p.name == 'Discord').spec;
    expect(discord.base['accentPrimary'], const Color(0xFF5865F2));

    final base = discord.resolveBaseStyle();
    expect(base.cornerRadius, 8.0);
    expect(base.avatarShape, TCSectionStyle.avatarCircle);

    expect(discord.resolveStyle(TCSection.serverRail).cornerRadius, 12.0);
    expect(discord.resolveStyle(TCSection.serverRail).avatarShape,
        TCSectionStyle.avatarCircle,
        reason: 'the rail inherits the base avatar cut');
    expect(discord.resolve(TCSection.serverRail).bgApp, const Color(0xFF1E1F22));
    expect(discord.resolve(TCSection.channelList).bgSurface, const Color(0xFF2B2D31));
    expect(discord.resolveBase().bgApp, const Color(0xFF313338));
  });

  test('every preset keeps its text and accents legible on their own surface', () {
    // The background each section actually paints, so a preset that recolors
    // one cannot leave a foreground token invisible against it.
    final surfaces = <TCSection, Color Function(TCSectionColors)>{
      TCSection.serverRail: (c) => c.bgApp,
      TCSection.channelList: (c) => c.bgSurface,
      TCSection.presence: (c) => c.bgSurface,
      TCSection.topBar: (c) => c.bgSurfaceRaised,
      TCSection.content: (c) => c.bgApp,
      TCSection.dialogs: (c) => c.bgSurfaceRaised,
    };

    for (final preset in themePresets) {
      for (final entry in surfaces.entries) {
        final colors = preset.spec.resolve(entry.key);
        final bg = entry.value(colors);
        final foregrounds = {
          'textPrimary': colors.textPrimary,
          'textEmphasis': colors.textEmphasis,
          'textSecondary': colors.textSecondary,
          'accentPrimary': colors.accentPrimary,
        };
        for (final fg in foregrounds.entries) {
          expect(
            _contrast(fg.value, bg),
            greaterThanOrEqualTo(_minContrast),
            reason: '${preset.name}: ${fg.key} is unreadable on '
                '${entry.key.wireId}\'s background',
          );
        }
      }
    }
  });

  testWidgets('applying Discord saves its shape styles as well as its colors',
      (tester) async {
    await openEditor(tester);

    await tester.tap(find.text('DISCORD'));
    await tester.pump();
    await tester.tap(find.text('APPLY'));
    await settle(tester);

    final discord = themePresets.firstWhere((p) => p.name == 'Discord').spec;
    final posted = lastPostedTheme();
    expect((posted['styles'] as Map)['base'], {
      'glow': false,
      'displayFont': 'IBM Plex Mono',
      'panelEdge': 'plain',
      'cornerRadius': 8.0,
      'avatarShape': 'circle',
    });
    expect(((posted['sections'] as Map)['serverRail'] as Map)['bgApp'], '#1e1f22');
    expect(state.themeSpec, discord);
  });

  testWidgets('switching from Discord back to Trench drops its styles too',
      (tester) async {
    state.themeSpec = themePresets.firstWhere((p) => p.name == 'Discord').spec;
    await openEditor(tester);

    await tester.tap(find.text('TRENCH'));
    await tester.pump();
    await tester.tap(find.text('APPLY'));
    await settle(tester);

    expect(state.themeSpec.isEmpty, isTrue);
    expect(state.themeSpec.resolveBaseStyle(), TCSectionStyle.stock);
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

/// The contrast floor every preset's foreground tokens must clear. Low
/// enough to accept a real brand pairing -- Discord's blurple on its channel
/// sidebar sits just under 3.0 -- and high enough to catch what this guards:
/// a foreground left the same color as the surface behind it.
const double _minContrast = 2.5;

/// WCAG relative-contrast ratio, 1.0 for two identical colors.
double _contrast(Color a, Color b) {
  final first = a.computeLuminance();
  final second = b.computeLuminance();
  final lighter = first > second ? first : second;
  final darker = first > second ? second : first;
  return (lighter + 0.05) / (darker + 0.05);
}
