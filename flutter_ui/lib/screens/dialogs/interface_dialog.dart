// Add/Edit Reticulum interface dialog -- port of interfaces_widget.py's
// InterfaceDialog. Field schemas mirror its _TYPE_FIELDS/_COMMON_FIELDS;
// required-field validation mirrors interfaces_config.REQUIRED_FIELDS (the
// backend re-validates and its error text is surfaced inline either way).
import 'package:flutter/material.dart';

import '../../api/client.dart';
import '../../api/models/interface.dart';
import '../../app_state.dart';
import '../../theme/section_theme.dart';
import '../../theme/theme_spec.dart';
import '../../theme/tokens.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_checkbox.dart';
import '../../widgets/tc_dialog.dart';
import '../../widgets/tc_text_field.dart';

const List<String> kEditableInterfaceTypes = [
  'AutoInterface',
  'TCPClientInterface',
  'TCPServerInterface',
  'UDPInterface',
  'SerialInterface',
  'RNodeInterface',
];

enum _FieldKind { text, integer, decimal, flag, choice }

class _FieldSpec {
  const _FieldSpec(this.key, this.label, this.kind, this.defaultValue,
      {this.choices = const []});

  final String key;
  final String label;
  final _FieldKind kind;
  final String defaultValue;
  final List<String> choices;
}

const List<_FieldSpec> _commonFields = [
  _FieldSpec('interface_mode', 'Interface mode', _FieldKind.choice, 'full', choices: [
    'full', 'access_point', 'pointtopoint', 'roaming', 'boundary', 'gateway',
  ]),
  _FieldSpec('networkname', 'Network name', _FieldKind.text, ''),
  _FieldSpec('passphrase', 'Passphrase', _FieldKind.text, ''),
  _FieldSpec('bitrate', 'Bitrate (bps)', _FieldKind.integer, '0'),
  _FieldSpec('announce_cap', 'Announce cap (%)', _FieldKind.decimal, '2.0'),
];

const Map<String, List<_FieldSpec>> _typeFields = {
  'AutoInterface': [
    _FieldSpec('group_id', 'Group ID', _FieldKind.text, 'reticulum'),
    _FieldSpec('discovery_scope', 'Discovery scope', _FieldKind.choice, 'link',
        choices: ['link', 'admin', 'site', 'organisation', 'global']),
    _FieldSpec('discovery_port', 'Discovery port', _FieldKind.integer, '29716'),
    _FieldSpec('data_port', 'Data port', _FieldKind.integer, '42671'),
    _FieldSpec('devices', 'Allowed devices (comma-separated)', _FieldKind.text, ''),
    _FieldSpec('ignored_devices', 'Ignored devices (comma-separated)', _FieldKind.text, ''),
  ],
  'TCPClientInterface': [
    _FieldSpec('target_host', 'Target host', _FieldKind.text, ''),
    _FieldSpec('target_port', 'Target port', _FieldKind.integer, '4965'),
    _FieldSpec('kiss_framing', 'KISS framing', _FieldKind.flag, 'No'),
    _FieldSpec('i2p_tunneled', 'I2P tunneled', _FieldKind.flag, 'No'),
    _FieldSpec('connect_timeout', 'Connect timeout (s)', _FieldKind.integer, '5'),
    _FieldSpec('max_reconnect_tries', 'Max reconnect tries (0 = unlimited)',
        _FieldKind.integer, '0'),
  ],
  'TCPServerInterface': [
    _FieldSpec('listen_ip', 'Listen IP', _FieldKind.text, '0.0.0.0'),
    _FieldSpec('listen_port', 'Listen port', _FieldKind.integer, '4965'),
    _FieldSpec('i2p_tunneled', 'I2P tunneled', _FieldKind.flag, 'No'),
    _FieldSpec('prefer_ipv6', 'Prefer IPv6', _FieldKind.flag, 'No'),
  ],
  'UDPInterface': [
    _FieldSpec('listen_ip', 'Listen IP', _FieldKind.text, '0.0.0.0'),
    _FieldSpec('listen_port', 'Listen port', _FieldKind.integer, '4242'),
    _FieldSpec('forward_ip', 'Forward IP', _FieldKind.text, '255.255.255.255'),
    _FieldSpec('forward_port', 'Forward port', _FieldKind.integer, '4242'),
  ],
  'SerialInterface': [
    _FieldSpec('port', 'Serial port', _FieldKind.text, ''),
    _FieldSpec('speed', 'Baud rate', _FieldKind.integer, '9600'),
    _FieldSpec('databits', 'Data bits', _FieldKind.integer, '8'),
    _FieldSpec('parity', 'Parity', _FieldKind.choice, 'N', choices: ['N', 'E', 'O']),
    _FieldSpec('stopbits', 'Stop bits', _FieldKind.integer, '1'),
  ],
  'RNodeInterface': [
    _FieldSpec('port', 'Serial port', _FieldKind.text, ''),
    _FieldSpec('frequency', 'Frequency (Hz)', _FieldKind.integer, '868000000'),
    _FieldSpec('bandwidth', 'Bandwidth (Hz)', _FieldKind.integer, '125000'),
    _FieldSpec('txpower', 'TX power (dBm)', _FieldKind.integer, '14'),
    _FieldSpec('spreadingfactor', 'Spreading factor', _FieldKind.integer, '8'),
    _FieldSpec('codingrate', 'Coding rate', _FieldKind.integer, '5'),
    _FieldSpec('flow_control', 'Flow control', _FieldKind.flag, 'No'),
    _FieldSpec('id_interval', 'ID interval (s)', _FieldKind.integer, '0'),
    _FieldSpec('id_callsign', 'ID callsign', _FieldKind.text, ''),
    _FieldSpec('airtime_limit_short', 'Airtime limit short (%)', _FieldKind.decimal, '0.0'),
    _FieldSpec('airtime_limit_long', 'Airtime limit long (%)', _FieldKind.decimal, '0.0'),
  ],
};

const Map<String, List<String>> _requiredFields = {
  'TCPClientInterface': ['target_host'],
  'TCPServerInterface': ['listen_ip', 'listen_port'],
  'SerialInterface': ['port'],
  'RNodeInterface': ['port'],
};

/// Pops `true` after a successful create/update so the caller can refresh.
Future<bool?> showInterfaceDialog(BuildContext context, AppState state,
    {RetInterface? existing}) {
  return showTcDialog<bool>(
    context: context,
    builder: (context) => SectionTheme(
      spec: state.themeSpec,
      section: TCSection.dialogs,
      child: _InterfaceDialogContent(state: state, existing: existing),
    ),
  );
}

class _InterfaceDialogContent extends StatefulWidget {
  const _InterfaceDialogContent({required this.state, this.existing});

  final AppState state;
  final RetInterface? existing;

  @override
  State<_InterfaceDialogContent> createState() => _InterfaceDialogContentState();
}

class _InterfaceDialogContentState extends State<_InterfaceDialogContent> {
  final _name = TextEditingController();
  final Map<String, TextEditingController> _textValues = {};
  final Map<String, String> _choiceValues = {};
  final Map<String, bool> _flagValues = {};

  late String _type;
  late bool _enabled;
  String? _error;
  bool _busy = false;

  bool get _editing => widget.existing != null;

  @override
  void initState() {
    super.initState();
    _type = widget.existing?.type ?? kEditableInterfaceTypes.first;
    _enabled = widget.existing?.enabled ?? true;
    _name.text = widget.existing?.name ?? '';
    _initFieldState();
  }

  @override
  void dispose() {
    _name.dispose();
    for (final c in _textValues.values) {
      c.dispose();
    }
    super.dispose();
  }

  List<_FieldSpec> get _activeSpecs => [...?_typeFields[_type], ..._commonFields];

  /// (Re)seed per-field state for the active type, keeping any state a field
  /// key already has (so flipping type back and forth doesn't lose input).
  /// When editing, the existing interface's config values win over the
  /// type's defaults -- same as the Qt dialog's _make_field_widget.
  void _initFieldState() {
    final existing = widget.existing?.config ?? const {};
    for (final spec in _activeSpecs) {
      final raw = existing[spec.key];
      switch (spec.kind) {
        case _FieldKind.flag:
          _flagValues.putIfAbsent(
              spec.key, () => _isYes(raw ?? spec.defaultValue));
        case _FieldKind.choice:
          _choiceValues.putIfAbsent(spec.key,
              () => spec.choices.contains(raw) ? raw! : spec.defaultValue);
        case _FieldKind.text:
        case _FieldKind.integer:
        case _FieldKind.decimal:
          _textValues.putIfAbsent(
              spec.key,
              () => TextEditingController(
                  text: (raw == null || raw.isEmpty) ? spec.defaultValue : raw));
      }
    }
  }

  static bool _isYes(String v) => ['yes', 'true', '1'].contains(v.toLowerCase());

  String _valueOf(_FieldSpec spec) => switch (spec.kind) {
        _FieldKind.flag => (_flagValues[spec.key] ?? false) ? 'Yes' : 'No',
        _FieldKind.choice => _choiceValues[spec.key] ?? spec.defaultValue,
        _ => _textValues[spec.key]?.text.trim() ?? '',
      };

  Future<void> _submit() async {
    final name = _name.text.trim();
    if (name.isEmpty) {
      setState(() => _error = 'Interface name is required.');
      return;
    }
    final typeValues = {
      for (final spec in _typeFields[_type] ?? <_FieldSpec>[]) spec.key: _valueOf(spec),
    };
    for (final key in _requiredFields[_type] ?? const <String>[]) {
      final value = typeValues[key] ?? '';
      if (value.isEmpty || value == '0') {
        final label =
            _typeFields[_type]!.firstWhere((s) => s.key == key).label;
        setState(() => _error = "'$label' is required.");
        return;
      }
    }
    final commonValues = {for (final spec in _commonFields) spec.key: _valueOf(spec)};

    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      if (_editing) {
        await widget.state.api
            .updateInterface(name, _type, _enabled, typeValues, commonValues);
      } else {
        await widget.state.api
            .createInterface(name, _type, _enabled, typeValues, commonValues);
      }
      if (!mounted) return;
      Navigator.pop(context, true);
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _busy = false;
        _error = e is ApiException ? e.message : e.toString();
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return TcDialogShell(
      title: _editing ? 'Edit Interface' : 'Add Interface',
      width: 480,
      errorText: _error,
      actions: [
        TcGhostButton(label: 'CANCEL', onPressed: () => Navigator.pop(context)),
        TcPrimaryButton(
          label: _busy ? 'SAVING…' : 'SAVE',
          onPressed: _busy ? null : _submit,
        ),
      ],
      children: [
        Container(
          constraints: const BoxConstraints(maxHeight: 420),
          child: ListView(
            shrinkWrap: true,
            children: [
              TcTextField(
                label: 'Interface name',
                controller: _name,
                hintText: 'e.g. My TCP Hub',
                autofocus: !_editing,
                // The name is the record key, so a rename would 404 the PUT.
                // Editable only on create.
                readOnly: _editing,
              ),
              const SizedBox(height: 10),
              _fieldLabel('TYPE'),
              const SizedBox(height: 6),
              TcChoiceRow(
                options: {for (final t in kEditableInterfaceTypes) t: t},
                value: _type,
                onSelected: _editing
                    ? null
                    : (t) => setState(() {
                          _type = t;
                          _initFieldState();
                        }),
              ),
              const SizedBox(height: 10),
              TcCheckbox(
                value: _enabled,
                label: 'Enabled',
                onChanged: (v) => setState(() => _enabled = v),
              ),
              const SizedBox(height: 12),
              _fieldLabel('TYPE-SPECIFIC SETTINGS'),
              const SizedBox(height: 8),
              ..._buildFields(_typeFields[_type] ?? []),
              const SizedBox(height: 12),
              _fieldLabel('COMMON SETTINGS'),
              const SizedBox(height: 8),
              ..._buildFields(_commonFields),
            ],
          ),
        ),
      ],
    );
  }

  List<Widget> _buildFields(List<_FieldSpec> specs) => [
        for (final spec in specs) ...[
          switch (spec.kind) {
            _FieldKind.flag => TcCheckbox(
                value: _flagValues[spec.key] ?? false,
                label: spec.label,
                onChanged: (v) => setState(() => _flagValues[spec.key] = v),
              ),
            _FieldKind.choice => Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  _fieldLabel(spec.label.toUpperCase()),
                  const SizedBox(height: 6),
                  TcChoiceRow(
                    options: {for (final c in spec.choices) c: c.toUpperCase()},
                    value: _choiceValues[spec.key] ?? spec.defaultValue,
                    onSelected: (v) => setState(() => _choiceValues[spec.key] = v),
                  ),
                ],
              ),
            _ => TcTextField(label: spec.label, controller: _textValues[spec.key]!),
          },
          const SizedBox(height: 10),
        ],
      ];

  Widget _fieldLabel(String label) => Text(
        label,
        style: TextStyle(
          fontSize: TCType.textCaption,
          color: SectionTheme.of(context).textSecondary,
          letterSpacing: TCType.letterSpacingFor(TCType.textCaption, TCType.trackingWide),
        ),
      );
}
