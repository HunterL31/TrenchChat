// AppVersionInfo parsing and the note it derives for the settings dialog.
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/models/app_version.dart';

AppVersionInfo _info(String transition, {String? previous}) =>
    AppVersionInfo.fromJson({
      'version': '1.4.0',
      'previous': previous,
      'transition': transition,
      'changed_at': 1700000000.0,
    });

void main() {
  test('parses the backend payload', () {
    final info = _info('downgrade', previous: '1.5.0');

    expect(info.version, '1.4.0');
    expect(info.previous, '1.5.0');
    expect(info.transition, VersionTransition.downgrade);
    expect(info.changedAt, 1700000000.0);
    expect(info.isKnown, isTrue);
  });

  test('a first run has no previous build', () {
    final info = _info('first_run');

    expect(info.previous, isNull);
    expect(info.transition, VersionTransition.firstRun);
  });

  test('an unrecognised transition reads as unknown rather than throwing', () {
    expect(_info('something-new').transition, VersionTransition.unknown);
  });

  test('a missing payload reads as unknown', () {
    final info = AppVersionInfo.fromJson({});

    expect(info.isKnown, isFalse);
    expect(info.transition, VersionTransition.unknown);
  });
}
