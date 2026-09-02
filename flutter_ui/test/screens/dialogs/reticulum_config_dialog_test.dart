// The node-wide Reticulum settings dialog: the option set, its grouping and
// its tooltips all come from the backend, an unset option reads as DEFAULT,
// and saving must write only what the user actually touched -- sending an
// untouched field back would pin it to a value RNS is currently free to
// choose for itself.
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/app_state.dart';
import 'package:flutter_ui/screens/dialogs/reticulum_config_dialog.dart';
import 'package:flutter_ui/widgets/tc_text_field.dart';

import '../../fake_backend.dart';

Map<String, dynamic> _option(
  String key,
  String kind, {
  String section = 'reticulum',
  String category = 'Transport & routing',
  String? label,
  List<String> choices = const [],
  String defaultValue = 'No',
  String value = '',
  String description = 'What it does. What it costs.',
}) =>
    {
      'key': key,
      'section': section,
      'category': category,
      'label': label ?? key,
      'kind': kind,
      'choices': choices,
      'default': defaultValue,
      'description': description,
      'value': value,
    };

final List<Map<String, dynamic>> _options = [
  _option('enable_transport', 'bool',
      label: 'Enable transport',
      description: 'Route traffic for other peers. Uses more bandwidth.'),
  _option('default_gravity', 'int',
      label: 'Default gravity', defaultValue: '0'),
  _option('shared_instance_type', 'choice',
      category: 'Instance',
      label: 'Shared instance type',
      choices: ['tcp', 'unix'],
      defaultValue: 'unix',
      description: 'How local programs reach the shared instance.'),
  _option('loglevel', 'int',
      section: 'logging',
      category: 'Logging',
      label: 'Log level',
      defaultValue: '4',
      value: '6'),
];

Widget _harness(AppState state) {
  return MaterialApp(
    home: Scaffold(
      body: Builder(
        builder: (context) => ElevatedButton(
          onPressed: () => showReticulumConfigDialog(context, state),
          child: const Text('open'),
        ),
      ),
    ),
  );
}

void main() {
  late FakeBackend backend;
  late AppState state;

  setUp(() {
    backend = FakeBackend();
    backend.routes['GET /reticulum/config'] = {'ok': true, 'options': _options};
    backend.routes['PUT /reticulum/config'] = {'ok': true, 'restart_required': true};
    state = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
  });

  tearDown(() => state.dispose());

  Future<void> open(WidgetTester tester) async {
    await tester.pumpWidget(_harness(state));
    await tester.tap(find.text('open'));
    await tester.pump();
    await settle(tester);
  }

  testWidgets('renders the category headers and the options', (tester) async {
    await open(tester);

    expect(find.text('Reticulum Settings'), findsOneWidget);
    expect(find.text('TRANSPORT & ROUTING'), findsOneWidget);
    expect(find.text('INSTANCE'), findsOneWidget);
    expect(find.text('LOGGING'), findsOneWidget);
    expect(find.text('ENABLE TRANSPORT (?)'), findsOneWidget);
    expect(find.widgetWithText(TcTextField, 'Default gravity (?)'), findsOneWidget);
  });

  testWidgets('every option carries its description as a tooltip', (tester) async {
    await open(tester);

    expect(find.byTooltip('Route traffic for other peers. Uses more bandwidth.'),
        findsOneWidget);
    expect(find.byTooltip('How local programs reach the shared instance.'),
        findsOneWidget);
  });

  testWidgets('an unset option shows DEFAULT, a set one shows its value',
      (tester) async {
    await open(tester);

    // enable_transport and shared_instance_type are both unset, so both
    // choice rows sit on DEFAULT.
    expect(find.text(kUnsetLabel), findsNWidgets(2));
    expect(
      tester
          .widget<TcTextField>(find.widgetWithText(TcTextField, 'Log level (?)'))
          .controller
          .text,
      '6',
    );
  });

  testWidgets('saving sends only the changed key', (tester) async {
    await open(tester);

    await tester.tap(find.text('YES'));
    await tester.pump();
    await tester.tap(find.text('SAVE'));
    await settle(tester);

    final put = backend.requests
        .lastWhere((r) => r.method == 'PUT' && r.path == '/reticulum/config');
    final values = (jsonDecode(put.body) as Map<String, dynamic>)['values'];
    expect(values, {'enable_transport': 'Yes'});
  });

  testWidgets('an untouched dialog writes nothing', (tester) async {
    await open(tester);

    await tester.tap(find.text('SAVE'));
    await settle(tester);

    expect(backend.requests.any((r) => r.method == 'PUT'), isFalse);
  });

  testWidgets('a rejected save surfaces the backend message', (tester) async {
    backend.routes['PUT /reticulum/config'] =
        const FakeError(400, {'error': "'loglevel' must be between 0 and 7"});
    await open(tester);

    await tester.enterText(
        find.widgetWithText(TcTextField, 'Log level (?)'), '99');
    await tester.tap(find.text('SAVE'));
    await settle(tester);

    expect(find.text("'loglevel' must be between 0 and 7"), findsOneWidget);
  });
}
