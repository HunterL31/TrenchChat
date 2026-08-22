import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/screens/main_window/compose_bar.dart';

Widget _harness({required bool enabled}) => MaterialApp(
      home: Scaffold(
        body: ComposeBar(
          channelHash: 'hash-a',
          channelName: 'alpha',
          enabled: enabled,
          onSend: (_, _) async => true,
          compact: true,
        ),
      ),
    );

void main() {
  testWidgets('compose is disabled when send_message is denied', (tester) async {
    await tester.pumpWidget(_harness(enabled: false));

    expect(tester.widget<TextField>(find.byType(TextField)).enabled, isFalse);
  });

  testWidgets('compose is enabled when send_message is allowed', (tester) async {
    await tester.pumpWidget(_harness(enabled: true));

    expect(tester.widget<TextField>(find.byType(TextField)).enabled, isTrue);
  });
}
