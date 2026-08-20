import 'dart:io' show Platform;

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import 'app_state.dart';
import 'screens/main_window/main_window.dart';
import 'theme/app_theme.dart';
import 'theme/tokens.dart';

/// Base URL of the testenv tester's FastAPI backend (devtools/testenv/api.py).
/// Tester A defaults to :8801 when running orchestrator.py --testers 2.
const String defaultBaseUrl = 'http://127.0.0.1:8801';

/// Whether an `?api=` override points at the page's own host.
///
/// Port may differ -- that is the multi-tester layout it exists for -- but the
/// host may not, and the scheme has to be one the client can actually talk.
bool _isSameHost(String api, Uri page) {
  final target = Uri.tryParse(api);
  if (target == null || !target.hasAuthority) return false;
  if (target.scheme != 'http' && target.scheme != 'https') return false;
  return target.host.toLowerCase() == page.host.toLowerCase();
}

/// Backend address resolution, in priority order:
/// 1. `--dart-define=TC_API_URL=...` baked in at build time.
/// 2. On web, the page's `?api=` query parameter, restricted to the page's own
///    host. The orchestrator serves its page and each tester API on different
///    ports of one host, which is the case this exists for; without the
///    restriction any page could send someone to
///    `http://127.0.0.1:8810/?api=https://evil.tld`, where the real client
///    loads from their own origin and sends everything they type elsewhere.
/// 3. On web, the page origin -- covers serve_profile.py (and anything else)
///    hosting the client and the API on one port, which is what makes a
///    remotely tunnelled or LAN-served page work with no configuration.
/// 4. On desktop, the TC_API_URL process environment variable -- how
///    main_flutter.py points the window it spawns at its own backend.
/// 5. The tester-A default.
String resolveBaseUrl({Uri? pageUri, bool isWeb = kIsWeb,
    Map<String, String>? environment}) {
  const fromDefine = String.fromEnvironment('TC_API_URL');
  if (fromDefine.isNotEmpty) return fromDefine;
  if (isWeb) {
    final uri = pageUri ?? Uri.base;
    final api = uri.queryParameters['api'];
    if (api != null && api.isNotEmpty && _isSameHost(api, uri)) return api;
    return uri.origin;
  }
  final fromEnv = (environment ?? Platform.environment)['TC_API_URL'];
  if (fromEnv != null && fromEnv.isNotEmpty) return fromEnv;
  return defaultBaseUrl;
}

/// API token resolution, mirroring [resolveBaseUrl]'s priority order:
/// 1. `--dart-define=TC_API_TOKEN=...` baked in at build time.
/// 2. On web, the page's `?token=` query parameter -- how main_flutter.py and
///    serve_profile.py hand the browser the token they generated.
/// 3. On desktop, the TC_API_TOKEN process environment variable, set on the
///    window main_flutter.py spawns.
///
/// Empty means "send no token", which every backend now rejects; that is the
/// correct outcome for a client pointed at a backend it was not given access
/// to, rather than silently talking to one.
String resolveToken({Uri? pageUri, bool isWeb = kIsWeb,
    Map<String, String>? environment}) {
  const fromDefine = String.fromEnvironment('TC_API_TOKEN');
  if (fromDefine.isNotEmpty) return fromDefine;
  if (isWeb) {
    return (pageUri ?? Uri.base).queryParameters['token'] ?? '';
  }
  return (environment ?? Platform.environment)['TC_API_TOKEN'] ?? '';
}

void main() {
  runApp(TrenchChatApp(baseUrl: resolveBaseUrl(), token: resolveToken()));
}

class TrenchChatApp extends StatefulWidget {
  const TrenchChatApp({super.key, this.baseUrl = defaultBaseUrl, this.token = ''});

  final String baseUrl;
  final String token;

  @override
  State<TrenchChatApp> createState() => _TrenchChatAppState();
}

class _TrenchChatAppState extends State<TrenchChatApp> {
  late final AppState _state;

  @override
  void initState() {
    super.initState();
    _state = AppState(baseUrl: widget.baseUrl, token: widget.token);
    _state.init();
  }

  @override
  void dispose() {
    _state.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'TrenchChat',
      debugShowCheckedModeBanner: false,
      theme: buildAppTheme(),
      home: Scaffold(
        backgroundColor: TCColors.bgApp,
        body: MainWindow(state: _state),
      ),
    );
  }
}
