import 'package:flutter/painting.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/events.dart';
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

  test('the named library is loaded and parsed', () async {
    backend.routes['GET /ui_theme_library'] = {
      'themes': {
        'Deep': {
          'version': 1,
          'base': {'bgApp': '#ff0000'},
        },
        'Broken': 'not a document',
      },
    };

    await state.loadThemeLibrary();

    expect(state.themeLibrary.keys, ['Deep']);
    expect(state.themeLibrary['Deep']!.base['bgApp'], _red);
  });

  test('a failed library load is non-fatal', () async {
    backend.routes.remove('GET /ui_theme_library');

    await state.loadThemeLibrary();

    expect(state.themeLibrary, isEmpty);
    expect(state.error, isNull);
  });

  test('saveThemeAs posts the named document and upserts it', () async {
    final spec = ThemeSpec.empty.withBaseOverride('bgApp', _red);

    expect(await state.saveThemeAs('Deep', spec), isTrue);
    expect(await state.saveThemeAs('Deep', ThemeSpec.empty), isTrue);

    final posted = backend.requests.where((r) => r.path == '/ui_theme_library').toList();
    expect(posted.length, 2);
    expect(posted.first.body, contains('"name":"Deep"'));
    expect(state.themeLibrary.keys, ['Deep']);
    expect(state.themeLibrary['Deep']!.isEmpty, isTrue);
    expect(state.actionError, isNull);
  });

  test('a failed saveThemeAs leaves the library alone and reports the error', () async {
    backend.routes.remove('POST /ui_theme_library');

    expect(await state.saveThemeAs('Deep', ThemeSpec.empty), isFalse);

    expect(state.themeLibrary, isEmpty);
    expect(state.actionError, isNotNull);
  });

  test('deleteSavedTheme drops the entry', () async {
    backend.routes['POST /ui_theme_library/delete'] = {'ok': true};
    await state.saveThemeAs('Deep', ThemeSpec.empty);

    expect(await state.deleteSavedTheme('Deep'), isTrue);

    expect(state.themeLibrary, isEmpty);
  });

  test('deleting sends the name in the body, so a slashed one still goes', () async {
    // A name with a '/' in it cannot be addressed as a path segment: the
    // backend's router splits on it however the client encodes it.
    backend.routes['POST /ui_theme_library/delete'] = {'ok': true};
    await state.saveThemeAs('a/b', ThemeSpec.empty);

    expect(await state.deleteSavedTheme('a/b'), isTrue);

    final sent = backend.requests.last;
    expect(sent.method, 'POST');
    expect(sent.path, '/ui_theme_library/delete');
    expect(sent.body, contains('"name":"a/b"'));
    expect(state.themeLibrary, isEmpty);
  });

  test('a ui_theme event adopts the theme another client applied', () async {
    final spec = ThemeSpec.empty.withBaseOverride('bgApp', _red);
    var notified = 0;
    state.addListener(() => notified++);

    state.applyEvent(UiThemeEvent(spec));

    expect(state.themeSpec, spec);
    expect(notified, 1);
  });

  test('a ui_theme event matching what is in force changes nothing', () async {
    final spec = ThemeSpec.empty.withBaseOverride('bgApp', _red);
    await state.saveTheme(spec);
    var notified = 0;
    state.addListener(() => notified++);

    // The event this client's own save produced comes back to it.
    state.applyEvent(UiThemeEvent(ThemeSpec.empty.withBaseOverride('bgApp', _red)));

    expect(state.themeSpec, spec);
    expect(notified, 0);
  });

  test('a ui_theme_library event replaces the library', () async {
    final spec = ThemeSpec.empty.withBaseOverride('bgApp', _red);
    state.themeLibrary = {'Gone': ThemeSpec.empty};
    var notified = 0;
    state.addListener(() => notified++);

    state.applyEvent(UiThemeLibraryEvent({'Deep': spec}));

    expect(state.themeLibrary, {'Deep': spec});
    expect(notified, 1);

    state.applyEvent(UiThemeLibraryEvent({'Deep': spec}));
    expect(notified, 1);
  });

  test('deleting a theme the backend does not have reports the error', () async {
    await state.saveThemeAs('Deep', ThemeSpec.empty);

    expect(await state.deleteSavedTheme('Deep'), isFalse);

    expect(state.themeLibrary.keys, ['Deep']);
    expect(state.actionError, isNotNull);
  });
}
