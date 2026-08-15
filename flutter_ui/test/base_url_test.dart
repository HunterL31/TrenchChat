import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/main.dart';

void main() {
  test('desktop falls back to the tester-A default', () {
    expect(resolveBaseUrl(isWeb: false), defaultBaseUrl);
  });

  test('web uses the ?api= query parameter when present', () {
    expect(
      resolveBaseUrl(
        isWeb: true,
        pageUri: Uri.parse('http://box.local:8080/?api=http://box.local:8802'),
      ),
      'http://box.local:8802',
    );
  });

  test('web falls back to the page origin (same-origin hosting)', () {
    expect(
      resolveBaseUrl(isWeb: true, pageUri: Uri.parse('http://box.local:8801/#/')),
      'http://box.local:8801',
    );
  });
}
