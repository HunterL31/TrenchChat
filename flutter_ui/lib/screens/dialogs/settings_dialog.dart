// Settings dialog -- port of trenchchat/gui/settings.py's SettingsDialog
// Identity and Propagation Node tabs over GET/POST /settings and
// POST /me/display_name. The Qt dialog's avatar picker (needs a native file
// dialog) and Security/PIN tab (no API surface) are not in this spike.
import 'package:flutter/material.dart';

import '../../api/models/server.dart';
import '../../api/models/settings.dart';
import '../../app_state.dart';
import '../../theme/section_theme.dart';
import '../../theme/theme_spec.dart';
import '../../theme/tokens.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_checkbox.dart';
import '../../widgets/tc_dialog.dart';
import '../../widgets/tc_text_field.dart';
import 'appearance_dialog.dart';
import 'pin_dialogs.dart';

Future<void> showSettingsDialog(BuildContext context, AppState state) {
  return showTcDialog<void>(
    context: context,
    builder: (context) => _SettingsDialogContent(state: state),
  );
}

class _SettingsDialogContent extends StatefulWidget {
  const _SettingsDialogContent({required this.state});
  final AppState state;

  @override
  State<_SettingsDialogContent> createState() => _SettingsDialogContentState();
}

class _SettingsDialogContentState extends State<_SettingsDialogContent> {
  final _displayName = TextEditingController();
  final _outboundNode = TextEditingController();
  final _nodeName = TextEditingController();
  final _storageLimit = TextEditingController();

  bool _loading = true;
  bool _busy = false;
  String? _error;

  bool _propEnabled = false;
  String _filterMode = 'allowlist';
  final Set<String> _filterHashes = {};

  /// Session-local stand-in for the lockbox PIN state -- the lockbox has no
  /// API surface yet (locked-start design still open), so the ported PIN
  /// dialogs are exercised against this rather than persisted.
  String? _sessionPin;

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void dispose() {
    _displayName.dispose();
    _outboundNode.dispose();
    _nodeName.dispose();
    _storageLimit.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    try {
      final settings = await widget.state.api.getSettings();
      if (!mounted) return;
      setState(() {
        _displayName.text = widget.state.meDisplayName;
        _outboundNode.text = settings.outboundPropagationNode ?? '';
        _propEnabled = settings.propagationEnabled;
        _nodeName.text = settings.propagationNodeName;
        _storageLimit.text = '${settings.propagationStorageLimitMb}';
        _filterMode = settings.channelFilterMode;
        _filterHashes.addAll(settings.channelFilterHashes);
        _loading = false;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _loading = false;
        _error = 'Could not load settings: $e';
      });
    }
  }

  /// Every channel this client knows, mirroring the Qt dialog's
  /// storage.get_all_channels() checklist source.
  List<Channel> get _allChannels => [
        ...widget.state.standaloneChannels,
        for (final list in widget.state.channelsByServer.values) ...list,
      ];

  Future<void> _submit() async {
    final name = _displayName.text.trim();
    if (name.isEmpty) {
      setState(() => _error = 'Display name cannot be empty.');
      return;
    }
    final storageMb = int.tryParse(_storageLimit.text.trim());
    if (storageMb == null || storageMb < 16) {
      setState(() => _error = 'Storage limit must be a number of at least 16 MB.');
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });

    final okName = name == widget.state.meDisplayName ||
        await widget.state.saveDisplayName(name);
    final okSettings = await widget.state.saveSettings(TcSettings(
      propagationEnabled: _propEnabled,
      propagationNodeName: _nodeName.text.trim(),
      propagationStorageLimitMb: storageMb,
      channelFilterMode: _filterMode,
      channelFilterHashes: _filterHashes.toList(),
      outboundPropagationNode: _outboundNode.text.trim(),
    ));

    if (!mounted) return;
    if (!okName || !okSettings) {
      setState(() {
        _busy = false;
        _error = widget.state.takeActionError() ?? 'Could not save settings.';
      });
      return;
    }
    Navigator.pop(context);
  }

  @override
  Widget build(BuildContext context) {
    return SectionTheme(
      spec: widget.state.themeSpec,
      section: TCSection.dialogs,
      child: Builder(builder: _buildContent),
    );
  }

  Widget _buildContent(BuildContext context) {
    final tc = SectionTheme.of(context);
    return TcDialogShell(
      title: 'Settings',
      width: 460,
      errorText: _error,
      actions: [
        TcGhostButton(label: 'CANCEL', onPressed: () => Navigator.pop(context)),
        TcPrimaryButton(
          label: _busy ? 'SAVING…' : 'SAVE',
          onPressed: _busy || _loading ? null : _submit,
        ),
      ],
      children: _loading
          ? [
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 24),
                child: Center(
                  child: Text(
                    'LOADING…',
                    style: TextStyle(
                      fontSize: TCType.textCaption,
                      color: tc.textTertiary,
                      letterSpacing:
                          TCType.letterSpacingFor(TCType.textCaption, TCType.trackingWide),
                    ),
                  ),
                ),
              ),
            ]
          : [
              Container(
                constraints: const BoxConstraints(maxHeight: 420),
                child: ListView(
                  shrinkWrap: true,
                  // Keeps every line clear of the scrollbar, rather than the
                  // longer ones running under it.
                  padding: EdgeInsets.only(right: scrollbarInset(context)),
                  children: [
                    _sectionLabel(tc, 'IDENTITY'),
                    const SizedBox(height: 8),
                    TcTextField(
                      label: 'Display name',
                      controller: _displayName,
                      onSubmitted: (_) => _submit(),
                    ),
                    const SizedBox(height: 10),
                    _readonlyRow(tc, 'Identity hash', widget.state.meHashHex),
                    const SizedBox(height: 10),
                    TcTextField(
                      label: 'Propagation node',
                      controller: _outboundNode,
                      hintText: 'Leave blank to use direct delivery only',
                      onSubmitted: (_) => _submit(),
                    ),
                    const SizedBox(height: 16),
                    Container(height: 1, color: tc.borderSubtle),
                    const SizedBox(height: 12),
                    _sectionLabel(tc, 'PROPAGATION NODE'),
                    const SizedBox(height: 8),
                    TcCheckbox(
                      value: _propEnabled,
                      label: 'Enable propagation node on this instance',
                      onChanged: (v) => setState(() => _propEnabled = v),
                    ),
                    const SizedBox(height: 10),
                    TcTextField(
                      label: 'Node name',
                      controller: _nodeName,
                      hintText: 'e.g. my-relay',
                      onSubmitted: (_) => _submit(),
                    ),
                    const SizedBox(height: 10),
                    TcTextField(
                      label: 'Storage limit (MB)',
                      controller: _storageLimit,
                      onSubmitted: (_) => _submit(),
                    ),
                    const SizedBox(height: 10),
                    Text(
                      'CHANNEL FILTER',
                      style: TextStyle(
                        fontSize: TCType.textCaption,
                        color: tc.textSecondary,
                        letterSpacing:
                            TCType.letterSpacingFor(TCType.textCaption, TCType.trackingWide),
                      ),
                    ),
                    const SizedBox(height: 6),
                    TcChoiceRow(
                      options: const {'allowlist': 'ALLOWLIST', 'all': 'ALL'},
                      value: _filterMode,
                      onSelected: (v) => setState(() => _filterMode = v),
                    ),
                    if (_filterMode == 'allowlist' && _allChannels.isNotEmpty) ...[
                      const SizedBox(height: 10),
                      Container(
                        decoration: BoxDecoration(
                          color: tc.bgInset,
                          border: Border.all(color: tc.borderDefault),
                        ),
                        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            for (final c in _allChannels)
                              Padding(
                                padding: const EdgeInsets.symmetric(vertical: 3),
                                child: TcCheckbox(
                                  value: _filterHashes.contains(c.hash),
                                  label: '#${c.name}',
                                  onChanged: (v) => setState(() {
                                    if (v) {
                                      _filterHashes.add(c.hash);
                                    } else {
                                      _filterHashes.remove(c.hash);
                                    }
                                  }),
                                ),
                              ),
                          ],
                        ),
                      ),
                    ],
                    const SizedBox(height: 16),
                    Container(height: 1, color: tc.borderSubtle),
                    const SizedBox(height: 12),
                    _sectionLabel(tc, 'SECURITY'),
                    const SizedBox(height: 8),
                    Text(
                      _sessionPin != null
                          ? 'A PIN locks the app in this session only — it does not '
                              'encrypt your identity or message database at rest.'
                          : 'No PIN is set. Your identity file and message database '
                              'are stored unencrypted.',
                      style: TextStyle(
                          fontSize: TCType.textBodySm, color: tc.textSecondary),
                    ),
                    const SizedBox(height: 8),
                    Row(
                      children: [
                        if (_sessionPin == null)
                          TcGhostButton(label: 'SET PIN…', onPressed: _onSetPin)
                        else ...[
                          TcGhostButton(label: 'CHANGE PIN…', onPressed: _onChangePin),
                          const SizedBox(width: 6),
                          TcGhostButton(label: 'LOCK NOW', onPressed: _onLockNow),
                        ],
                      ],
                    ),
                    const SizedBox(height: 6),
                    Text(
                      'The lock screen and PIN dialogs are UI-only in this spike '
                      '— the lockbox is not reachable over the API yet.',
                      style: TextStyle(
                          fontSize: TCType.textMicro, color: tc.textTertiary),
                    ),
                    const SizedBox(height: 16),
                    Container(height: 1, color: tc.borderSubtle),
                    const SizedBox(height: 12),
                    _sectionLabel(tc, 'APPEARANCE'),
                    const SizedBox(height: 8),
                    Text(
                      _themeSummary,
                      style: TextStyle(fontSize: TCType.textBodySm, color: tc.textSecondary),
                    ),
                    const SizedBox(height: 8),
                    Row(
                      children: [
                        TcGhostButton(label: 'EDIT THEME…', onPressed: _onEditTheme),
                      ],
                    ),
                    const SizedBox(height: 16),
                    Container(height: 1, color: tc.borderSubtle),
                    const SizedBox(height: 12),
                    _sectionLabel(tc, 'ABOUT'),
                    const SizedBox(height: 8),
                    _readonlyRow(tc, 'Version', _version),
                  ],
                ),
              ),
            ],
    );
  }

  String get _version {
    final version = widget.state.appVersion;
    return version.isKnown ? version.version : 'Unknown';
  }

  /// One line describing how far the saved theme departs from stock.
  String get _themeSummary {
    final spec = widget.state.themeSpec;
    if (spec.isEmpty) return 'Using the stock palette.';
    final tokens = spec.base.length +
        spec.sections.values.fold<int>(0, (sum, tokens) => sum + tokens.length);
    final styles = spec.styles.values.fold<int>(0, (sum, keys) => sum + keys.length);
    final scopes = <String>{
      ...spec.sections.keys,
      ...spec.styles.keys,
      if (spec.base.isNotEmpty) ThemeSpec.baseStyleScope,
    }.length;
    final counted = [
      if (tokens > 0) '$tokens color${tokens == 1 ? '' : 's'}',
      if (styles > 0) '$styles style${styles == 1 ? '' : 's'}',
    ].join(' and ');
    return '$counted customized across $scopes scope${scopes == 1 ? '' : 's'}.';
  }

  Future<void> _onEditTheme() async {
    final staged = await showAppearanceDialog(context, widget.state);
    if (!mounted) return;
    // A staged share belongs in the compose box, so get out of its way.
    if (staged == true) {
      Navigator.pop(context);
      return;
    }
    setState(() {});
  }

  Future<void> _onSetPin() async {
    final pin = await showSetPinDialog(context);
    if (pin != null && mounted) setState(() => _sessionPin = pin);
  }

  Future<void> _onChangePin() async {
    final change = await showChangePinDialog(
      context,
      verifyPin: (pin) => pin == _sessionPin,
    );
    if (change != null && mounted) setState(() => _sessionPin = change.newPin);
  }

  Future<void> _onLockNow() async {
    await showUnlockDialog(context, verifyPin: (pin) => pin == _sessionPin);
  }

  Widget _sectionLabel(TCSectionColors tc, String label) => Text(
        label,
        style: TextStyle(
          fontSize: TCType.textCaption,
          color: tc.accentPrimary,
          letterSpacing: TCType.letterSpacingFor(TCType.textCaption, TCType.trackingWider),
        ),
      );

  Widget _readonlyRow(TCSectionColors tc, String label, String value) => Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            label.toUpperCase(),
            style: TextStyle(
              fontSize: TCType.textCaption,
              color: tc.textSecondary,
              letterSpacing:
                  TCType.letterSpacingFor(TCType.textCaption, TCType.trackingWide),
            ),
          ),
          const SizedBox(height: 6),
          SelectableText(
            value,
            style: TextStyle(fontSize: TCType.textBodySm, color: tc.textTertiary),
          ),
        ],
      );
}
