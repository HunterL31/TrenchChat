import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/main.dart';

void main() {
  test('desktop falls back to the tester-A default', () {
    expect(resolveBaseUrl(isWeb: false, environment: const {}), defaultBaseUrl);
  });

  test('desktop honors the TC_API_URL process environment variable', () {
    expect(
      resolveBaseUrl(
        isWeb: false,
        environment: const {'TC_API_URL': 'http://127.0.0.1:8810'},
      ),
      'http://127.0.0.1:8810',
    );
  });

  test('web ignores the process environment', () {
    expect(
      resolveBaseUrl(
        isWeb: true,
        pageUri: Uri.parse('http://box.local:8801/'),
        environment: const {'TC_API_URL': 'http://127.0.0.1:8810'},
      ),
      'http://box.local:8801',
    );
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
