// IFACE tab -- port of interfaces_widget.py's InterfacesWidget: the
// configured Reticulum interfaces with live status/RX/TX, plus add, edit,
// and delete for the editable types. The Qt widget's 5s stats auto-refresh
// is a manual REFRESH here to keep the tab timer-free.
import 'package:flutter/material.dart';

import '../../api/client.dart';
import '../../api/models/bandwidth.dart';
import '../../api/models/discovery.dart';
import '../../api/models/interface.dart';
import '../../app_state.dart';
import '../../format.dart';
import '../../theme/section_theme.dart';
import '../../theme/theme_spec.dart';
import '../../theme/tokens.dart';
import '../../widgets/status_dot.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_icon.dart';
import '../dialogs/interface_dialog.dart';
import '../dialogs/reticulum_config_dialog.dart';

export '../../format.dart' show formatByteCount;

String formatRate(double? bytesPerSec) {
  if (bytesPerSec == null) return '—';
  if (bytesPerSec < 1024) return '${bytesPerSec.toStringAsFixed(0)} B/s';
  if (bytesPerSec < 1024 * 1024) {
    return '${(bytesPerSec / 1024).toStringAsFixed(1)} KB/s';
  }
  return '${(bytesPerSec / (1024 * 1024)).toStringAsFixed(1)} MB/s';
}

String windowLabel(int secs) {
  if (secs < 60) return '${secs}S';
  if (secs < 3600) return '${secs ~/ 60}M';
  return '${secs ~/ 3600}H';
}

class IfaceTab extends StatefulWidget {
  const IfaceTab({super.key, required this.state});

  final AppState state;

  @override
  State<IfaceTab> createState() => _IfaceTabState();
}

class _IfaceTabState extends State<IfaceTab> {
  List<RetInterface>? _interfaces;
  BandwidthReport? _bandwidth;
  DiscoveryReport? _discovery;
  Map<String, String> _suggested = {};
  String? _error;
  String? _confirmDeleteName;
  bool _restartRequired = false;

  @override
  void initState() {
    super.initState();
    _refresh();
  }

  Future<void> _refresh() async {
    try {
      final interfaces = await widget.state.api.getInterfaces();
      if (!mounted) return;
      setState(() {
        _interfaces = interfaces;
        _error = null;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _interfaces = [];
        _error = e is ApiException ? e.message : e.toString();
      });
    }
    // Diagnostics only: a failure hides the strip without erroring the tab.
    try {
      final bandwidth = await widget.state.api.getBandwidth();
      if (!mounted) return;
      setState(() => _bandwidth = bandwidth);
    } catch (_) {
      if (!mounted) return;
      setState(() => _bandwidth = null);
    }
    // Both optional: a backend without the discovery endpoints just hides
    // the section and the defaults button.
    try {
      final discovery = await widget.state.api.getDiscovery();
      if (!mounted) return;
      setState(() => _discovery = discovery);
    } catch (_) {
      if (!mounted) return;
      setState(() => _discovery = null);
    }
    try {
      final suggested = await widget.state.api.getSuggestedDefaults();
      if (!mounted) return;
      setState(() => _suggested = suggested);
    } catch (_) {
      if (!mounted) return;
      setState(() => _suggested = {});
    }
  }

  Future<void> _applyDefaults() async {
    try {
      await widget.state.api.applySuggestedDefaults();
      setState(() => _restartRequired = true);
      await _refresh();
    } catch (e) {
      if (!mounted) return;
      setState(() => _error = e is ApiException ? e.message : e.toString());
    }
  }

  Future<void> _toggleDiscovery() async {
    final settings = _discovery?.settings;
    if (settings == null) return;
    final enable = !settings.discoverInterfaces;
    final autoconnect =
        settings.autoconnectCount > 0 ? settings.autoconnectCount : 3;
    try {
      await widget.state.api.setDiscoverySettings(enable, autoconnect,
          requiredDiscoveryValue: settings.requiredDiscoveryValue);
      setState(() => _restartRequired = true);
      await _refresh();
    } catch (e) {
      if (!mounted) return;
      setState(() => _error = e is ApiException ? e.message : e.toString());
    }
  }

  Future<void> _pin(DiscoveredInterface iface) async {
    final hash = iface.discoveryHash;
    if (hash == null) return;
    try {
      await widget.state.api.pinDiscoveredInterface(hash);
      setState(() => _restartRequired = true);
      await _refresh();
    } catch (e) {
      if (!mounted) return;
      setState(() => _error = e is ApiException ? e.message : e.toString());
    }
  }

  Future<void> _add() async {
    final changed = await showInterfaceDialog(context, widget.state);
    if (changed == true) {
      setState(() => _restartRequired = true);
      await _refresh();
    }
  }

  Future<void> _editNodeConfig() async {
    final changed = await showReticulumConfigDialog(context, widget.state);
    if (changed == true) {
      setState(() => _restartRequired = true);
      await _refresh();
    }
  }

  Future<void> _edit(RetInterface iface) async {
    final changed = await showInterfaceDialog(context, widget.state, existing: iface);
    if (changed == true) {
      setState(() => _restartRequired = true);
      await _refresh();
    }
  }

  Future<void> _delete(String name) async {
    setState(() => _confirmDeleteName = null);
    try {
      await widget.state.api.deleteInterface(name);
      setState(() => _restartRequired = true);
      await _refresh();
    } catch (e) {
      if (!mounted) return;
      setState(() => _error = e is ApiException ? e.message : e.toString());
    }
  }

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final interfaces = _interfaces;
    return Container(
      color: tc.bgApp,
      padding: const EdgeInsets.all(18),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  'RETICULUM INTERFACES',
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                    fontSize: TCType.textCaption,
                    color: tc.textSecondary,
                    letterSpacing:
                        TCType.letterSpacingFor(TCType.textCaption, TCType.trackingWider),
                  ),
                ),
              ),
              if (_suggested.isNotEmpty) ...[
                TcGhostButton(
                    icon: TcIcons.plus, label: 'DEFAULTS', onPressed: _applyDefaults),
                const SizedBox(width: 6),
              ],
              TcGhostButton(
                  icon: TcIcons.settings, label: 'CONFIG', onPressed: _editNodeConfig),
              const SizedBox(width: 6),
              TcGhostButton(icon: TcIcons.plus, label: 'ADD', onPressed: _add),
              const SizedBox(width: 6),
              TcGhostButton(icon: TcIcons.sync, label: 'REFRESH', onPressed: _refresh),
            ],
          ),
          if (_restartRequired) ...[
            const SizedBox(height: 10),
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
              decoration: BoxDecoration(
                color: TCColors.amber900,
                border: Border.all(color: TCColors.amber700),
              ),
              child: Text(
                'CONFIG CHANGED — RESTART RETICULUM FOR IT TO TAKE EFFECT',
                style: TextStyle(
                  fontSize: TCType.textMicro,
                  color: TCColors.amber300,
                  letterSpacing:
                      TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWide),
                ),
              ),
            ),
          ],
          if (_error != null) ...[
            const SizedBox(height: 10),
            Text(
              _error!,
              style: TextStyle(fontSize: TCType.textCaption, color: tc.statusDanger),
            ),
          ],
          if (_bandwidth != null) ...[
            const SizedBox(height: 12),
            _bandwidthStrip(tc, _bandwidth!),
          ],
          const SizedBox(height: 12),
          Expanded(
            flex: 3,
            child: LayoutBuilder(
              builder: (context, constraints) {
                // Seven columns squeezed into a phone viewport ellipsize to
                // nothing, so the table keeps its minimum width and pans
                // sideways instead.
                final width = constraints.maxWidth < _minTableWidth
                    ? _minTableWidth
                    : constraints.maxWidth;
                return SingleChildScrollView(
                  scrollDirection: Axis.horizontal,
                  child: SizedBox(
                    width: width,
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.stretch,
                      children: [
                        _headerRow(tc),
                        Container(height: 1, color: tc.borderDefault),
                        Expanded(child: _tableBody(tc, interfaces)),
                      ],
                    ),
                  ),
                );
              },
            ),
          ),
          if (_discovery != null) ...[
            const SizedBox(height: 14),
            _discoveredHeader(tc, _discovery!.settings),
            const SizedBox(height: 6),
            Expanded(flex: 2, child: _discoveredTable(tc, _discovery!)),
          ],
        ],
      ),
    );
  }

  Widget _discoveredHeader(TCSectionColors tc, DiscoverySettings settings) {
    final status = settings.discoverInterfaces
        ? 'DISCOVERY ON · AUTOCONNECT ${settings.autoconnectCount}'
        : 'DISCOVERY OFF';
    return Row(
      children: [
        Expanded(
          child: Text(
            'DISCOVERED ENTRY POINTS',
            overflow: TextOverflow.ellipsis,
            style: TextStyle(
              fontSize: TCType.textCaption,
              color: tc.textSecondary,
              letterSpacing:
                  TCType.letterSpacingFor(TCType.textCaption, TCType.trackingWider),
            ),
          ),
        ),
        Text(
          status,
          style: TextStyle(
            fontSize: TCType.textMicro,
            color: settings.discoverInterfaces ? tc.statusOnline : tc.textTertiary,
            letterSpacing:
                TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWide),
          ),
        ),
        const SizedBox(width: 8),
        TcGhostButton(
          label: settings.discoverInterfaces ? 'DISABLE' : 'ENABLE',
          onPressed: _toggleDiscovery,
        ),
      ],
    );
  }

  Widget _discoveredTable(TCSectionColors tc, DiscoveryReport discovery) {
    if (discovery.interfaces.isEmpty) {
      return Center(
        child: Text(
          discovery.settings.discoverInterfaces
              ? 'Nothing discovered yet.'
              : 'Enable discovery to find entry points announced on the mesh.',
          style: TextStyle(fontSize: TCType.textBodySm, color: tc.textTertiary),
        ),
      );
    }
    return LayoutBuilder(
      builder: (context, constraints) {
        final width = constraints.maxWidth < _minTableWidth
            ? _minTableWidth
            : constraints.maxWidth;
        return SingleChildScrollView(
          scrollDirection: Axis.horizontal,
          child: SizedBox(
            width: width,
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                _discoveredHeaderRow(tc),
                Container(height: 1, color: tc.borderDefault),
                Expanded(
                  child: ListView(children: [
                    for (final i in discovery.interfaces) _discoveredRow(tc, i),
                  ]),
                ),
              ],
            ),
          ),
        );
      },
    );
  }

  Widget _discoveredHeaderRow(TCSectionColors tc) {
    Widget cell(String label, int flex) => Expanded(
          flex: flex,
          child: Text(
            label,
            style: TextStyle(
              fontSize: TCType.textMicro,
              color: tc.textTertiary,
              letterSpacing:
                  TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWide),
            ),
          ),
        );
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 6),
      child: Row(
        children: [
          cell('NAME', _flexName),
          cell('TYPE', _flexType),
          cell('STATUS', _flexStatus),
          cell('HOPS', 1),
          cell('LAST HEARD', _flexStatus),
          const SizedBox(width: _pinWidth),
        ],
      ),
    );
  }

  Widget _discoveredRow(TCSectionColors tc, DiscoveredInterface iface) {
    Widget cell(Widget child, int flex) => Expanded(flex: flex, child: child);
    Text text(String s, {Color? color}) => Text(
          s,
          overflow: TextOverflow.ellipsis,
          style:
              TextStyle(fontSize: TCType.textBodySm, color: color ?? tc.textSecondary),
        );
    final statusColor = switch (iface.status) {
      'available' => tc.statusOnline,
      'stale' => tc.statusDanger,
      _ => tc.textTertiary,
    };
    return Container(
      padding: const EdgeInsets.symmetric(vertical: 8),
      decoration: BoxDecoration(
        border: Border(bottom: BorderSide(color: tc.borderSubtle)),
      ),
      child: Row(
        children: [
          cell(text(iface.name, color: tc.textEmphasis), _flexName),
          cell(text(iface.type.replaceAll('Interface', '')), _flexType),
          cell(text(iface.status.toUpperCase(), color: statusColor), _flexStatus),
          cell(text(iface.hops?.toString() ?? '—'), 1),
          cell(text(_ago(iface.lastHeard)), _flexStatus),
          SizedBox(
            width: _pinWidth,
            child: iface.pinnable
                ? Align(
                    alignment: Alignment.centerRight,
                    child: TcGhostButton(label: 'PIN', onPressed: () => _pin(iface)),
                  )
                : const SizedBox.shrink(),
          ),
        ],
      ),
    );
  }

  static String _ago(double? epochSecs) {
    if (epochSecs == null) return '—';
    final diff =
        DateTime.now().millisecondsSinceEpoch / 1000.0 - epochSecs;
    if (diff < 60) return 'JUST NOW';
    if (diff < 3600) return '${diff ~/ 60}M AGO';
    if (diff < 86400) return '${diff ~/ 3600}H AGO';
    return '${diff ~/ 86400}D AGO';
  }

  Widget _bandwidthStrip(TCSectionColors tc, BandwidthReport bw) {
    Widget cell(String label, String rx, String tx) => Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                label,
                style: TextStyle(
                  fontSize: TCType.textMicro,
                  color: tc.textTertiary,
                  letterSpacing:
                      TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWide),
                ),
              ),
              const SizedBox(height: 2),
              Text('RX $rx',
                  style: TextStyle(
                      fontSize: TCType.textBodySm, color: tc.textSecondary)),
              Text('TX $tx',
                  style: TextStyle(
                      fontSize: TCType.textBodySm, color: tc.textSecondary)),
            ],
          ),
        );

    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
      decoration: BoxDecoration(
        border: Border.all(color: tc.borderSubtle),
      ),
      child: Row(
        children: [
          for (final w in bw.windows)
            cell('BANDWIDTH ${windowLabel(w.secs)}',
                formatRate(w.rxPerSec), formatRate(w.txPerSec)),
          cell('SESSION TOTAL', formatByteCount(bw.totalRx),
              formatByteCount(bw.totalTx)),
        ],
      ),
    );
  }

  static const _flexName = 3;
  static const _flexType = 3;
  static const _flexEnabled = 2;
  static const _flexStatus = 2;
  static const _flexBytes = 2;
  static const _actionsWidth = 200.0;
  static const _pinWidth = 70.0;
  static const _minTableWidth = 620.0;

  Widget _tableBody(TCSectionColors tc, List<RetInterface>? interfaces) {
    if (interfaces == null) {
      return Center(
        child: Text(
          'LOADING…',
          style: TextStyle(fontSize: TCType.textCaption, color: tc.textTertiary),
        ),
      );
    }
    if (interfaces.isEmpty) {
      return Center(
        child: Text(
          'No interfaces configured.',
          style: TextStyle(fontSize: TCType.textBodySm, color: tc.textTertiary),
        ),
      );
    }
    return ListView(children: [for (final i in interfaces) _interfaceRow(tc, i)]);
  }

  Widget _headerRow(TCSectionColors tc) {
    Widget cell(String label, int flex) => Expanded(
          flex: flex,
          child: Text(
            label,
            style: TextStyle(
              fontSize: TCType.textMicro,
              color: tc.textTertiary,
              letterSpacing: TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWide),
            ),
          ),
        );
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 6),
      child: Row(
        children: [
          cell('NAME', _flexName),
          cell('TYPE', _flexType),
          cell('ENABLED', _flexEnabled),
          cell('STATUS', _flexStatus),
          cell('RX', _flexBytes),
          cell('TX', _flexBytes),
          const SizedBox(width: _actionsWidth),
        ],
      ),
    );
  }

  Widget _interfaceRow(TCSectionColors tc, RetInterface iface) {
    final confirming = _confirmDeleteName == iface.name;
    Widget cell(Widget child, int flex) => Expanded(flex: flex, child: child);
    Text text(String s, {Color? color}) => Text(
          s,
          overflow: TextOverflow.ellipsis,
          style: TextStyle(fontSize: TCType.textBodySm, color: color ?? tc.textSecondary),
        );

    return Container(
      padding: const EdgeInsets.symmetric(vertical: 8),
      decoration: BoxDecoration(
        border: Border(bottom: BorderSide(color: tc.borderSubtle)),
      ),
      child: Row(
        children: [
          cell(text(iface.name, color: tc.textEmphasis), _flexName),
          cell(text(iface.type), _flexType),
          cell(
            StatusDot(
              status: iface.enabled ? PresenceStatus.online : PresenceStatus.offline,
              size: 10,
            ),
            _flexEnabled,
          ),
          cell(
            text(
              switch (iface.status) { true => 'UP', false => 'DOWN', null => '—' },
              color: switch (iface.status) {
                true => tc.statusOnline,
                false => tc.statusDanger,
                null => tc.textTertiary,
              },
            ),
            _flexStatus,
          ),
          cell(text(iface.rxb != null ? formatByteCount(iface.rxb!) : '—'), _flexBytes),
          cell(text(iface.txb != null ? formatByteCount(iface.txb!) : '—'), _flexBytes),
          SizedBox(
            width: _actionsWidth,
            child: iface.editable
                ? Row(
                    mainAxisAlignment: MainAxisAlignment.end,
                    children: confirming
                        ? [
                            Text(
                              'DELETE?',
                              style: TextStyle(
                                  fontSize: TCType.textCaption,
                                  color: tc.statusDanger),
                            ),
                            const SizedBox(width: 6),
                            TcGhostButton(label: 'YES', onPressed: () => _delete(iface.name)),
                            const SizedBox(width: 4),
                            TcGhostButton(
                              label: 'NO',
                              onPressed: () => setState(() => _confirmDeleteName = null),
                            ),
                          ]
                        : [
                            TcGhostButton(label: 'EDIT', onPressed: () => _edit(iface)),
                            const SizedBox(width: 4),
                            TcGhostButton(
                              label: 'DEL',
                              onPressed: () =>
                                  setState(() => _confirmDeleteName = iface.name),
                            ),
                          ],
                  )
                : Align(
                    alignment: Alignment.centerRight,
                    child: Text(
                      'READ-ONLY',
                      style: TextStyle(
                        fontSize: TCType.textMicro,
                        color: tc.textTertiary,
                        letterSpacing:
                            TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWide),
                      ),
                    ),
                  ),
          ),
        ],
      ),
    );
  }
}
