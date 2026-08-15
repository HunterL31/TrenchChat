import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import 'app_state.dart';
import 'screens/main_window/main_window.dart';
import 'theme/app_theme.dart';
import 'theme/tokens.dart';

/// Base URL of the testenv tester's FastAPI backend (devtools/testenv/api.py).
/// Tester A defaults to :8801 when running orchestrator.py --testers 2.
const String defaultBaseUrl = 'http://127.0.0.1:8801';

/// Backend address resolution, in priority order:
/// 1. `--dart-define=TC_API_URL=...` baked in at build time.
/// 2. On web, the page's `?api=` query parameter.
/// 3. On web, the page origin -- covers serve_profile.py (and anything else)
///    hosting the client and the API on one port, which is what makes a
///    remotely tunnelled or LAN-served page work with no configuration.
/// 4. The tester-A default.
String resolveBaseUrl({Uri? pageUri, bool isWeb = kIsWeb}) {
  const fromEnv = String.fromEnvironment('TC_API_URL');
  if (fromEnv.isNotEmpty) return fromEnv;
  if (isWeb) {
    final uri = pageUri ?? Uri.base;
    final api = uri.queryParameters['api'];
    if (api != null && api.isNotEmpty) return api;
    return uri.origin;
  }
  return defaultBaseUrl;
}

void main() {
  runApp(TrenchChatApp(baseUrl: resolveBaseUrl()));
}

class TrenchChatApp extends StatefulWidget {
  const TrenchChatApp({super.key, this.baseUrl = defaultBaseUrl});

  final String baseUrl;

  @override
  State<TrenchChatApp> createState() => _TrenchChatAppState();
}

class _TrenchChatAppState extends State<TrenchChatApp> {
  late final AppState _state;

  @override
  void initState() {
    super.initState();
    _state = AppState(baseUrl: widget.baseUrl);
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
