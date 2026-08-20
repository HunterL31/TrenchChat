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

  test('web ignores an ?api= pointing at another host', () {
    // A page anywhere can send someone to their own localhost with this set,
    // where the real client loads from their own origin and every request --
    // and everything they type into it -- goes to the attacker instead.
    expect(
      resolveBaseUrl(
        isWeb: true,
        pageUri: Uri.parse('http://127.0.0.1:8810/?api=https://evil.tld'),
      ),
      'http://127.0.0.1:8810',
    );
  });

  test('web ignores an unparseable or schemeless ?api=', () {
    expect(
      resolveBaseUrl(
        isWeb: true,
        pageUri: Uri.parse('http://box.local:8801/?api=%2F%2Fevil.tld'),
      ),
      'http://box.local:8801',
    );
    expect(
      resolveBaseUrl(
        isWeb: true,
        pageUri: Uri.parse('http://box.local:8801/?api=javascript%3Aalert(1)'),
      ),
      'http://box.local:8801',
    );
  });

  test('web falls back to the page origin (same-origin hosting)', () {
    expect(
      resolveBaseUrl(isWeb: true, pageUri: Uri.parse('http://box.local:8801/#/')),
      'http://box.local:8801',
    );
  });
}
