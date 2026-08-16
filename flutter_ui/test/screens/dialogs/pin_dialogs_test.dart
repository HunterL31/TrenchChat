import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/screens/dialogs/pin_dialogs.dart';

Widget _harness(void Function(BuildContext context) show) {
  return MaterialApp(
    home: Scaffold(
      body: Builder(
        builder: (context) => ElevatedButton(
          onPressed: () => show(context),
          child: const Text('open'),
        ),
      ),
    ),
  );
}

void main() {
  group('SetPinDialog', () {
    testWidgets('rejects short and mismatched PINs, pops the accepted one', (tester) async {
      String? result;
      await tester.pumpWidget(_harness((context) async {
        result = await showSetPinDialog(context);
      }));
      await tester.tap(find.text('open'));
      await tester.pumpAndSettle();

      await tester.enterText(find.byType(TextField).first, '12');
      await tester.tap(find.text('SET PIN'));
      await tester.pump();
      expect(find.text('PIN must be at least $pinMinLen digits.'), findsOneWidget);

      await tester.enterText(find.byType(TextField).first, '1234');
      await tester.enterText(find.byType(TextField).last, '9999');
      await tester.tap(find.text('SET PIN'));
      await tester.pump();
      expect(find.text('PINs do not match.'), findsOneWidget);

      await tester.enterText(find.byType(TextField).first, '1234');
      await tester.enterText(find.byType(TextField).last, '1234');
      await tester.tap(find.text('SET PIN'));
      await tester.pumpAndSettle();
      expect(result, '1234');
    });
  });

  group('ChangePinDialog', () {
    testWidgets('verifies the current PIN and treats blank new fields as removal',
        (tester) async {
      PinChange? result;
      await tester.pumpWidget(_harness((context) async {
        result = await showChangePinDialog(context, verifyPin: (pin) => pin == '4321');
      }));
      await tester.tap(find.text('open'));
      await tester.pumpAndSettle();

      await tester.enterText(find.byType(TextField).first, '1111');
      await tester.tap(find.text('APPLY'));
      await tester.pump();
      expect(find.text('Current PIN is incorrect.'), findsOneWidget);

      await tester.enterText(find.byType(TextField).first, '4321');
      await tester.tap(find.text('APPLY'));
      await tester.pumpAndSettle();
      expect(result, isNotNull);
      expect(result!.newPin, isNull);
    });
  });

  group('UnlockDialog', () {
    testWidgets('wrong attempts count down and trigger the cooldown', (tester) async {
      await tester.pumpWidget(_harness((context) {
        showUnlockDialog(context, verifyPin: (pin) => pin == '2468');
      }));
      await tester.tap(find.text('open'));
      await tester.pumpAndSettle();

      for (int i = 0; i < maxUnlockAttempts - 1; i++) {
        await tester.enterText(find.byType(TextField), '0000');
        await tester.tap(find.text('UNLOCK'));
        await tester.pump();
      }
      expect(find.text('Incorrect PIN. 1 attempt(s) remaining.'), findsOneWidget);

      await tester.enterText(find.byType(TextField), '0000');
      await tester.tap(find.text('UNLOCK'));
      await tester.pump();
      expect(find.textContaining('Too many attempts.'), findsOneWidget);

      // The cooldown ticks down and re-enables the field afterwards.
      await tester.pump(const Duration(seconds: unlockCooldownSecs + 1));
      expect(find.textContaining('Too many attempts.'), findsNothing);

      await tester.enterText(find.byType(TextField), '2468');
      await tester.tap(find.text('UNLOCK'));
      await tester.pumpAndSettle();
      expect(find.text('TrenchChat — Unlock'), findsNothing);
    });
  });
}
