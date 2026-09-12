// Settings dialog -- Identity and Propagation Node tabs over GET/POST
// /settings and POST /me/display_name. An avatar picker (needs a native
// file dialog) and a Security/PIN tab (no API surface) are not in this
// spike.
import 'package:flutter/material.dart';

import '../../api/models/settings.dart';
import '../../api/models/upgrade.dart';
import '../../api/models/voice.dart';
import '../../app_state.dart';
import '../../format.dart';
import '../../theme/section_theme.dart';
import '../../theme/theme_spec.dart';
import '../../theme/tokens.dart';
import '../../widgets/badge.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_checkbox.dart';
import '../../widgets/tc_context_menu.dart';
import '../../widgets/tc_dialog.dart';
import '../../widgets/tc_text_field.dart';
import 'appearance_dialog.dart';
import 'pin_dialogs.dart';
import 'propagation_nodes_dialog.dart';

/// How many propagation nodes the settings pane lists inline before the
/// rest move behind a button.
const int _nodePreviewCount = 5;

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
  final _nodeName = TextEditingController();
  final _storageLimit = TextEditingController();
  final _listenPort = TextEditingController();

  bool _loading = true;
  bool _busy = false;
  String? _error;

  bool _propEnabled = false;

  /// The "Direct connections" switch. Applied the moment it is flipped
  /// rather than on SAVE: off closes the sessions this node holds, and a
  /// user withdrawing their address should not have to confirm it twice.
  bool _directEnabled = false;

  /// GET /voice/devices snapshot; unavailable until loaded (or when the
  /// backend has no audio stack, in which case [AudioDevices.reason] says why).
  AudioDevices _devices = AudioDevices.unavailable;
  String? _inputDevice;
  String? _outputDevice;

  /// Session-local stand-in for the lockbox PIN state -- the lockbox has no
  /// API surface yet (locked-start design still open), so the ported PIN
  /// dialogs are exercised against this rather than persisted.
  String? _sessionPin;

  @override
  void initState() {
    super.initState();
    widget.state.addListener(_onStateChanged);
    _load();
  }

  @override
  void dispose() {
    widget.state.removeListener(_onStateChanged);
    _displayName.dispose();
    _nodeName.dispose();
    _storageLimit.dispose();
    _listenPort.dispose();
    super.dispose();
  }

  /// The diagnostics list is AppState's, and a path_changed event moves it
  /// while the dialog is open.
  void _onStateChanged() {
    if (mounted) setState(() {});
  }

  Future<void> _load() async {
    try {
      final settings = await widget.state.api.getSettings();
      // A backend without the audio stack still answers, with a reason;
      // only a transport failure leaves the section in its unloaded state.
      AudioDevices devices = AudioDevices.unavailable;
      try {
        devices = await widget.state.api.getVoiceDevices();
      } catch (_) {}
      await widget.state.loadDirectConnections();
      await widget.state.loadDirectSessions();
      if (!mounted) return;
      final port = settings.upgradeListenPort > 0
          ? settings.upgradeListenPort
          : widget.state.directConnections.listenPort;
      setState(() {
        _displayName.text = widget.state.meDisplayName;
        _propEnabled = settings.propagationEnabled;
        _nodeName.text = settings.propagationNodeName;
        _storageLimit.text = '${settings.propagationStorageLimitMb}';
        _directEnabled = widget.state.directConnections.enabled;
        _listenPort.text = port > 0 ? '$port' : '';
        _devices = devices;
        _inputDevice = devices.selectedInput;
        _outputDevice = devices.selectedOutput;
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
    // An empty field is a backend that never answered with a port; sending
    // nothing leaves the stored one alone rather than asking for port zero.
    final portText = _listenPort.text.trim();
    final listenPort = portText.isEmpty ? 0 : int.tryParse(portText) ?? -1;
    if (portText.isNotEmpty && (listenPort < 1 || listenPort > 65535)) {
      setState(() =>
          _error = 'Listen port must be a number between 1 and 65535.');
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
      upgradeListenPort: listenPort,
    ));
    final devicesChanged = _devices.available &&
        (_inputDevice != _devices.selectedInput ||
            _outputDevice != _devices.selectedOutput);
    final okDevices = !devicesChanged ||
        await widget.state.setVoiceDevices(
            inputDevice: _inputDevice, outputDevice: _outputDevice);

    if (!mounted) return;
    if (!okName || !okSettings || !okDevices) {
      setState(() {
        _busy = false;
        _error = widget.state.takeActionError() ?? 'Could not save settings.';
      });
      return;
    }
    Navigator.pop(context);
  }

  /// The node in use, the nearest few heard on the mesh, and the controls to
  /// pin one or hand the choice back to the mesh. Up to MAX_TRACKED_NODES are
  /// held at once and they are ordered nearest first, so the rest go behind a
  /// button rather than pushing the rest of this pane off the screen.
  Widget _outboundNodeControls(TCSectionColors tc) {
    final propagation = widget.state.propagation;
    final selected = propagation.selected;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          selected == null
              ? 'No node heard yet — messages to an offline friend wait here '
                  'until they come back.'
              : 'Using ${_shortHash(selected)}'
                  '${propagation.pinned.isEmpty ? " (chosen automatically)" : " (pinned)"}',
          style: TextStyle(fontSize: TCType.textBodySm, color: tc.textPrimary),
        ),
        for (final node in propagation.nodes.take(_nodePreviewCount))
          PropagationNodeRow(state: widget.state, node: node),
        if (propagation.nodes.length > _nodePreviewCount)
          Padding(
            padding: const EdgeInsets.only(top: 6),
            child: TcGhostButton(
              label: 'ALL ${propagation.nodes.length} NODES',
              onPressed: () =>
                  showPropagationNodesDialog(context, widget.state),
            ),
          ),
        const SizedBox(height: 8),
        Row(
          children: [
            if (propagation.pinned.isNotEmpty)
              TcGhostButton(
                label: 'CHOOSE AUTOMATICALLY',
                onPressed: () => widget.state.pinPropagationNode(''),
              ),
            if (propagation.pinned.isNotEmpty) const SizedBox(width: 8),
            TcGhostButton(
              label: 'COLLECT NOW',
              onPressed: selected == null
                  ? null
                  : () => widget.state.collectPropagated(),
            ),
          ],
        ),
      ],
    );
  }

  static String _shortHash(String hex) => shortNodeHash(hex);

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
                      'A propagation node stores and forwards mail for the wider '
                      'LXMF network. It does not carry TrenchChat\'s own messages, '
                      'and what it relays cannot be filtered — propagated messages '
                      'are encrypted end to end, so a node cannot read what is in '
                      'them.',
                      style: TextStyle(
                          fontSize: TCType.textBodySm, color: tc.textSecondary),
                    ),
                    const SizedBox(height: 16),
                    Container(height: 1, color: tc.borderSubtle),
                    const SizedBox(height: 12),
                    _sectionLabel(tc, 'OFFLINE DIRECT MESSAGES'),
                    const SizedBox(height: 8),
                    Text(
                      'A direct message to a friend who is away is left with a '
                      'propagation node until they collect it — a group channel '
                      'can be caught up by any other member, but a conversation '
                      'has nobody else in it. The node sees who is talking to '
                      'whom and how much, never what was said.',
                      style: TextStyle(
                          fontSize: TCType.textBodySm, color: tc.textSecondary),
                    ),
                    const SizedBox(height: 10),
                    _outboundNodeControls(tc),
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
                    _sectionLabel(tc, 'VOICE'),
                    const SizedBox(height: 8),
                    if (!_devices.available)
                      Text(
                        _devices.reason.isEmpty
                            ? 'Audio devices are not available.'
                            : 'Audio devices are not available — ${_devices.reason}',
                        style: TextStyle(
                            fontSize: TCType.textBodySm, color: tc.textSecondary),
                      )
                    else ...[
                      _devicePicker(tc, 'MICROPHONE', _devices.input,
                          _inputDevice, (v) => setState(() => _inputDevice = v)),
                      const SizedBox(height: 10),
                      _devicePicker(tc, 'SPEAKERS', _devices.output,
                          _outputDevice, (v) => setState(() => _outputDevice = v)),
                      const SizedBox(height: 8),
                      Text(
                        'A device change takes effect immediately, even in a '
                        'live call. If a chosen device is unplugged, voice '
                        'falls back to the system default.',
                        style: TextStyle(
                            fontSize: TCType.textMicro, color: tc.textTertiary),
                      ),
                    ],
                    const SizedBox(height: 16),
                    Container(height: 1, color: tc.borderSubtle),
                    const SizedBox(height: 12),
                    _sectionLabel(tc, 'DIRECT CONNECTIONS'),
                    const SizedBox(height: 8),
                    _directConnections(tc),
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

  static const String _systemDefaultLabel = 'System default';

  /// One device row: label plus a button showing the current choice that
  /// opens the device list as a menu (null selection = system default).
  Widget _devicePicker(TCSectionColors tc, String label, List<String> devices,
      String? selected, ValueChanged<String?> onSelected) {
    // An unplugged device stays selectable, so reopening the dialog does not
    // silently drop a choice the pipeline is already falling back from.
    final options = [
      if (selected != null && !devices.contains(selected)) selected,
      ...devices,
    ];
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          label,
          style: TextStyle(
            fontSize: TCType.textCaption,
            color: tc.textSecondary,
            letterSpacing:
                TCType.letterSpacingFor(TCType.textCaption, TCType.trackingWide),
          ),
        ),
        const SizedBox(height: 6),
        Align(
          alignment: Alignment.centerLeft,
          child: Builder(
            builder: (buttonContext) => TcGhostButton(
              label: selected ?? _systemDefaultLabel,
              onPressed: () {
                final box = buttonContext.findRenderObject() as RenderBox?;
                final position = box?.localToGlobal(Offset.zero) ?? Offset.zero;
                showTcContextMenu(
                  context: buttonContext,
                  position: position,
                  items: [
                    TcContextMenuItem(
                      label: _systemDefaultLabel,
                      onTap: () => onSelected(null),
                    ),
                    for (final device in options)
                      TcContextMenuItem(
                        label: device,
                        onTap: () => onSelected(device),
                      ),
                  ],
                );
              },
            ),
          ),
        ),
      ],
    );
  }

  /// The switch, the port, and why each eligible peer has a session or has
  /// none. Whether this node is listening at all is the line that keeps a
  /// blocked port from reading as a NAT that will not punch.
  Widget _directConnections(TCSectionColors tc) {
    final direct = widget.state.directSessions;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        TcCheckbox(
          value: _directEnabled,
          label: 'Connect directly to members over IP when possible',
          onChanged: _onDirectEnabledChanged,
        ),
        const SizedBox(height: 8),
        Text(
          'A direct session carries files, voice and history between two '
          'members of a shared invite-only channel at IP speed. Everything '
          'still works without one: the pair stays on the mesh. Turning this '
          'off closes the sessions this node holds, and offers nobody an '
          'address.',
          style: TextStyle(fontSize: TCType.textBodySm, color: tc.textSecondary),
        ),
        const SizedBox(height: 10),
        TcTextField(
          label: 'Listen port (UDP)',
          controller: _listenPort,
          hintText: 'e.g. 42420',
          onSubmitted: (_) => _submit(),
        ),
        const SizedBox(height: 6),
        Text(
          direct.listening
              ? 'Listening on port ${direct.listenPort}. A new port takes '
                  'effect on the next launch.'
              : 'Not listening: nothing can arrive on this node. A new port '
                  'takes effect on the next launch.',
          style: TextStyle(
            fontSize: TCType.textMicro,
            color: direct.listening ? tc.textTertiary : tc.statusWarn,
          ),
        ),
        const SizedBox(height: 10),
        if (direct.sessions.isEmpty && direct.failures.isEmpty)
          Text(
            'No peer to report on yet.',
            style: TextStyle(fontSize: TCType.textBodySm, color: tc.textTertiary),
          ),
        for (final session in direct.sessions) _sessionRow(tc, session),
        for (final failure in direct.failures) _failureRow(tc, failure),
      ],
    );
  }

  /// One peer this node holds a session with: how long, how far, how much.
  Widget _sessionRow(TCSectionColors tc, DirectSession session) {
    final name = session.displayName.isNotEmpty
        ? session.displayName
        : widget.state.resolvePeerName(session.peer) ?? '';
    final roundTrip = session.roundTripSecs > 0
        ? '${(session.roundTripSecs * 1000).toStringAsFixed(0)} ms'
        : 'not measured yet';
    return Padding(
      padding: const EdgeInsets.only(top: 6),
      child: Row(
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Flexible(
                      child: Text(
                        '${name.isEmpty ? '' : '$name '}'
                        '${_shortHash(session.peer)}',
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(
                            fontSize: TCType.textBodySm, color: tc.textPrimary),
                      ),
                    ),
                    const SizedBox(width: 6),
                    const DirectBadge(),
                  ],
                ),
                Text(
                  'up ${formatRelativeAgo(session.since)
                      .replaceAll(' ago', '')}, round trip $roundTrip, '
                  '${formatByteCount(session.bytesIn)} in / '
                  '${formatByteCount(session.bytesOut)} out',
                  style: TextStyle(
                      fontSize: TCType.textMicro, color: tc.textTertiary),
                ),
              ],
            ),
          ),
          TcGhostButton(
            label: 'DROP',
            onPressed: () => widget.state.closeDirectSession(session.peer),
          ),
        ],
      ),
    );
  }

  /// One eligible peer with no session, and why not.
  Widget _failureRow(TCSectionColors tc, DirectFailure failure) {
    final name = widget.state.resolvePeerName(failure.peer) ?? '';
    final waiting = failure.nextAttempt >
        DateTime.now().millisecondsSinceEpoch / 1000;
    return Padding(
      padding: const EdgeInsets.only(top: 6),
      child: Row(
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  '${name.isEmpty ? '' : '$name '}${_shortHash(failure.peer)}',
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                      fontSize: TCType.textBodySm, color: tc.textSecondary),
                ),
                Text(
                  waiting
                      ? '${directFailureReason(failure.reason)}; waiting '
                          'until ${formatTsShort(failure.nextAttempt)}'
                      : directFailureReason(failure.reason),
                  style: TextStyle(
                      fontSize: TCType.textMicro, color: tc.textTertiary),
                ),
              ],
            ),
          ),
          TcGhostButton(
            label: 'TRY NOW',
            onPressed: () => widget.state.tryDirectSession(failure.peer),
          ),
        ],
      ),
    );
  }

  Future<void> _onDirectEnabledChanged(bool value) async {
    setState(() => _directEnabled = value);
    final ok = await widget.state.setDirectConnections(value);
    if (!mounted) return;
    setState(() {
      _directEnabled = widget.state.directConnections.enabled;
      if (!ok) {
        _error = widget.state.takeActionError() ??
            'Could not change direct connections.';
      }
    });
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
