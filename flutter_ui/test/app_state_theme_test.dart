import 'package:flutter/painting.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/app_state.dart';
import 'package:flutter_ui/theme/theme_spec.dart';

import 'fake_backend.dart';

const _red = Color(0xFFFF0000);

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

  test('the saved theme is loaded into themeSpec', () async {
    backend.routes['GET /ui_theme'] = {
      'theme': {
        'version': 1,
        'sections': {
          'topBar': {'bgApp': '#ff0000'},
        },
      },
    };

    await state.loadTheme();

    expect(state.themeSpec.resolve(TCSection.topBar).bgApp, _red);
  });

  test('a backend with no theme leaves the stock one in place', () async {
    backend.routes['GET /ui_theme'] = {'theme': <String, dynamic>{}};

    await state.loadTheme();

    expect(state.themeSpec.isEmpty, isTrue);
  });

  test('a failed load is non-fatal', () async {
    backend.routes.remove('GET /ui_theme');

    await state.loadTheme();

    expect(state.themeSpec.isEmpty, isTrue);
    expect(state.error, isNull);
  });

  test('saveTheme posts the document and adopts it', () async {
    final spec = ThemeSpec.empty.withBaseOverride('bgApp', _red);

    await state.saveTheme(spec);

    final posted = backend.requests.where((r) => r.path == '/ui_theme').single;
    expect(posted.method, 'POST');
    expect(posted.body, contains('"bgApp":"#ff0000"'));
    expect(state.themeSpec, spec);
    expect(state.actionError, isNull);
  });

  test('a failed save keeps the previous theme and reports the error', () async {
    backend.routes.remove('POST /ui_theme');

    await state.saveTheme(ThemeSpec.empty.withBaseOverride('bgApp', _red));

    expect(state.themeSpec.isEmpty, isTrue);
    expect(state.actionError, isNotNull);
  });
}
