// Join Channel dialog -- lists standalone public channels discovered via a
// mesh announce but not yet joined. Updates live off ChannelDiscoveredEvent
// (see AppState.refreshDiscoveredChannels); the Refresh button just
// re-requests announces, same as the Qt JoinChannelDialog's Refresh.
//
// Discovery is announce-driven, not instant -- a channel created moments ago
// on another peer may take several seconds to appear here.
import 'package:flutter/material.dart';

import '../../api/models/server.dart';
import '../../app_state.dart';
import '../../theme/section_theme.dart';
import '../../theme/theme_spec.dart';
import '../../theme/tokens.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_dialog.dart';

Future<void> showJoinChannelDialog(BuildContext context, AppState state) {
  return showTcDialog<void>(
    context: context,
    builder: (context) => SectionTheme(
      spec: state.themeSpec,
      section: TCSection.dialogs,
      child: _JoinChannelDialogContent(state: state),
    ),
  );
}

class _JoinChannelDialogContent extends StatefulWidget {
  const _JoinChannelDialogContent({required this.state});
  final AppState state;

  @override
  State<_JoinChannelDialogContent> createState() => _JoinChannelDialogContentState();
}

class _JoinChannelDialogContentState extends State<_JoinChannelDialogContent> {
  String? _selectedHash;
  String? _error;
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    widget.state.refreshDiscoveredChannels();
  }

  Future<void> _submit() async {
    final hash = _selectedHash;
    if (hash == null) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    final ok = await widget.state.joinChannel(hash);
    if (!mounted) return;
    if (!ok) {
      setState(() {
        _busy = false;
        _error = widget.state.actionError ?? 'Could not join channel.';
      });
      return;
    }
    Navigator.pop(context);
  }

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    return AnimatedBuilder(
      animation: widget.state,
      builder: (context, _) {
        final channels = widget.state.discoveredChannels;
        // A discovered channel can disappear from the list (joined elsewhere,
        // announce aged out) without this dialog's setState firing -- fall
        // back to "nothing selected" for this build rather than mutate state
        // mid-build.
        final selectedHash =
            channels.any((c) => c.hash == _selectedHash) ? _selectedHash : null;

        return TcDialogShell(
          title: 'Join Channel',
          width: 440,
          errorText: _error,
          actions: [
            TcGhostButton(label: 'CANCEL', onPressed: () => Navigator.pop(context)),
            TcPrimaryButton(
              label: _busy ? 'JOINING…' : 'JOIN',
              onPressed: (selectedHash == null || _busy) ? null : _submit,
            ),
          ],
          children: [
            Text(
              'Channels announced on the network appear here.',
              style: TextStyle(fontSize: TCType.textCaption, color: tc.textTertiary),
            ),
            const SizedBox(height: 12),
            Container(
              height: 220,
              decoration: BoxDecoration(border: Border.all(color: tc.borderDefault)),
              child: channels.isEmpty
                  ? Center(
                      child: Text(
                        'No channels discovered yet.',
                        style: TextStyle(fontSize: TCType.textCaption, color: tc.textTertiary),
                      ),
                    )
                  : ListView(
                      padding: EdgeInsets.zero,
                      children: [
                        for (final c in channels)
                          _DiscoveredRow(
                            channel: c,
                            selected: c.hash == selectedHash,
                            onTap: () => setState(() => _selectedHash = c.hash),
                          ),
                      ],
                    ),
            ),
            const SizedBox(height: 10),
            Align(
              alignment: Alignment.centerLeft,
              child: TcGhostButton(
                label: '↻ REFRESH',
                onPressed: () => widget.state.refreshDiscoveredChannels(),
              ),
            ),
          ],
        );
      },
    );
  }
}

class _DiscoveredRow extends StatelessWidget {
  const _DiscoveredRow({required this.channel, required this.selected, required this.onTap});

  final Channel channel;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    return GestureDetector(
      onTap: onTap,
      child: MouseRegion(
        cursor: SystemMouseCursors.click,
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
          color: selected ? TCColors.green900 : Colors.transparent,
          child: Row(
            children: [
              Text('#',
                  style: TextStyle(color: selected ? tc.accentPrimary : tc.textTertiary)),
              const SizedBox(width: 4),
              Expanded(
                child: Text(
                  channel.name,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                    fontSize: TCType.textBodySm,
                    color: selected ? TCColors.green100 : tc.textSecondary,
                  ),
                ),
              ),
              if (channel.description.isNotEmpty)
                Flexible(
                  child: Text(
                    channel.description,
                    overflow: TextOverflow.ellipsis,
                    textAlign: TextAlign.right,
                    style: TextStyle(fontSize: TCType.textCaption, color: tc.textTertiary),
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }
}
