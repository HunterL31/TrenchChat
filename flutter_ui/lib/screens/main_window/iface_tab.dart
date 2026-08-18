// IFACE tab -- port of interfaces_widget.py's InterfacesWidget: the
// configured Reticulum interfaces with live status/RX/TX, plus add, edit,
// and delete for the editable types. The Qt widget's 5s stats auto-refresh
// is a manual REFRESH here to keep the tab timer-free.
import 'package:flutter/material.dart';

import '../../api/client.dart';
import '../../api/models/interface.dart';
import '../../app_state.dart';
import '../../theme/tokens.dart';
import '../../widgets/status_dot.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_icon.dart';
import '../dialogs/interface_dialog.dart';

String formatByteCount(int n) {
  if (n < 1024) return '$n B';
  if (n < 1024 * 1024) return '${(n / 1024).toStringAsFixed(1)} KB';
  return '${(n / (1024 * 1024)).toStringAsFixed(1)} MB';
}

class IfaceTab extends StatefulWidget {
  const IfaceTab({super.key, required this.state});

  final AppState state;

  @override
  State<IfaceTab> createState() => _IfaceTabState();
}

class _IfaceTabState extends State<IfaceTab> {
  List<RetInterface>? _interfaces;
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
  }

  Future<void> _add() async {
    final changed = await showInterfaceDialog(context, widget.state);
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
    final interfaces = _interfaces;
    return Container(
      color: TCColors.bgApp,
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
                    color: TCColors.textSecondary,
                    letterSpacing:
                        TCType.letterSpacingFor(TCType.textCaption, TCType.trackingWider),
                  ),
                ),
              ),
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
              style: TextStyle(fontSize: TCType.textCaption, color: TCColors.statusDanger),
            ),
          ],
          const SizedBox(height: 12),
          Expanded(
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
                        _headerRow(),
                        Container(height: 1, color: TCColors.borderDefault),
                        Expanded(child: _tableBody(interfaces)),
                      ],
                    ),
                  ),
                );
              },
            ),
          ),
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
  static const _minTableWidth = 620.0;

  Widget _tableBody(List<RetInterface>? interfaces) {
    if (interfaces == null) {
      return Center(
        child: Text(
          'LOADING…',
          style: TextStyle(fontSize: TCType.textCaption, color: TCColors.textTertiary),
        ),
      );
    }
    if (interfaces.isEmpty) {
      return Center(
        child: Text(
          'No interfaces configured.',
          style: TextStyle(fontSize: TCType.textBodySm, color: TCColors.textTertiary),
        ),
      );
    }
    return ListView(children: [for (final i in interfaces) _interfaceRow(i)]);
  }

  Widget _headerRow() {
    Widget cell(String label, int flex) => Expanded(
          flex: flex,
          child: Text(
            label,
            style: TextStyle(
              fontSize: TCType.textMicro,
              color: TCColors.textTertiary,
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

  Widget _interfaceRow(RetInterface iface) {
    final confirming = _confirmDeleteName == iface.name;
    Widget cell(Widget child, int flex) => Expanded(flex: flex, child: child);
    Text text(String s, {Color? color}) => Text(
          s,
          overflow: TextOverflow.ellipsis,
          style: TextStyle(fontSize: TCType.textBodySm, color: color ?? TCColors.textSecondary),
        );

    return Container(
      padding: const EdgeInsets.symmetric(vertical: 8),
      decoration: BoxDecoration(
        border: Border(bottom: BorderSide(color: TCColors.borderSubtle)),
      ),
      child: Row(
        children: [
          cell(text(iface.name, color: TCColors.green100), _flexName),
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
                true => TCColors.statusOnline,
                false => TCColors.statusDanger,
                null => TCColors.textTertiary,
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
                                  color: TCColors.statusDanger),
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
                        color: TCColors.textTertiary,
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
