import 'package:flutter/material.dart';

import 'app_state.dart';
import 'screens/main_window/main_window.dart';
import 'theme/app_theme.dart';
import 'theme/tokens.dart';

/// Base URL of the testenv tester's FastAPI backend (devtools/testenv/api.py).
/// Tester A defaults to :8801 when running orchestrator.py --testers 2.
const String defaultBaseUrl = 'http://127.0.0.1:8801';

void main() {
  runApp(const TrenchChatApp());
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
