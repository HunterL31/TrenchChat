// The interface name is the record key, so editing it and saving would PUT a
// name the backend has no record of. The name field must therefore be
// read-only when editing an existing interface, while every other field stays
// editable; on create it is editable.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/models/interface.dart';
import 'package:flutter_ui/app_state.dart';
import 'package:flutter_ui/screens/dialogs/interface_dialog.dart';
import 'package:flutter_ui/widgets/tc_text_field.dart';

import '../../fake_backend.dart';

Widget _harness(AppState state, {RetInterface? existing}) {
  return MaterialApp(
    home: Scaffold(
      body: Builder(
        builder: (context) => ElevatedButton(
          onPressed: () => showInterfaceDialog(context, state, existing: existing),
          child: const Text('open'),
        ),
      ),
    ),
  );
}

TcTextField _field(WidgetTester tester, String label) =>
    tester.widget<TcTextField>(find.widgetWithText(TcTextField, label));

void main() {
  late FakeBackend backend;
  late AppState state;

  setUp(() {
    backend = FakeBackend();
    state = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
  });

  tearDown(() => state.dispose());

  Future<void> open(WidgetTester tester, {RetInterface? existing}) async {
    await tester.pumpWidget(_harness(state, existing: existing));
    await tester.tap(find.text('open'));
    await tester.pump();
    await settle(tester);
  }

  testWidgets('the name field is editable when creating', (tester) async {
    await open(tester);

    expect(find.text('Add Interface'), findsOneWidget);
    expect(_field(tester, 'Interface name').readOnly, isFalse);
  });

  testWidgets('the name field is read-only when editing, other fields stay editable',
      (tester) async {
    const existing = RetInterface(
      name: 'My TCP Hub',
      type: 'TCPClientInterface',
      enabled: true,
      editable: true,
      config: {'target_host': '10.0.0.1', 'target_port': '4965'},
    );

    await open(tester, existing: existing);

    expect(find.text('Edit Interface'), findsOneWidget);
    expect(_field(tester, 'Interface name').readOnly, isTrue);
    // A type-specific field is still editable, so other settings can change.
    expect(_field(tester, 'Target host').readOnly, isFalse);
  });
}
