// AppState's version load: the client learns which build the backend is
// running, and a backend that cannot say leaves it unknown rather than
// failing startup.
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/models/app_version.dart';
import 'package:flutter_ui/app_state.dart';

import 'fake_backend.dart';

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

  test('a downgrade is carried through to the client', () async {
    backend.routes['GET /version'] = {
      'version': '1.4.0',
      'previous': '1.5.0',
      'transition': 'downgrade',
      'changed_at': 1700000000.0,
      'history': <dynamic>[],
    };

    await state.loadVersion();

    expect(state.appVersion.version, '1.4.0');
    expect(state.appVersion.transition, VersionTransition.downgrade);
  });

  test('a backend without the endpoint leaves the version unknown', () async {
    await state.loadVersion();

    expect(state.appVersion.isKnown, isFalse);
    expect(state.appVersion.transition, VersionTransition.unknown);
  });
}
