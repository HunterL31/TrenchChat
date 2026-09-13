// The share picker: which monitor, and clearer or smoother. Shows a picture
// of each monitor grabbed once for this dialog, so what will be shared is
// seen before anything is captured, and says who can watch: participants
// this node holds a direct connection with, and nobody else.
import 'dart:convert';

import 'package:flutter/material.dart';

import '../../api/models/screen.dart';
import '../../app_state.dart';
import '../../theme/section_theme.dart';
import '../../theme/theme_spec.dart';
import '../../theme/tokens.dart';
import '../../widgets/peer_image.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_dialog.dart';

const String screenSourceDialogTitle = 'Share your screen';

const String screenSourceDialogNote =
    'Only participants of this voice session you hold a direct connection '
    'with can watch. Nothing about the share ever crosses the mesh.';

/// Asks which monitor and which preset, then starts the share. Returns true
/// once the backend has it running.
Future<bool> showScreenSourceDialog(BuildContext context, AppState state) async {
  final sources = await state.loadScreenSources();
  if (!context.mounted) return false;
  final choice = await showTcDialog<({int monitor, ScreenPreset preset})>(
    context: context,
    builder: (context) => SectionTheme(
      spec: state.themeSpec,
      section: TCSection.dialogs,
      child: _ScreenSourceDialog(sources: sources),
    ),
  );
  if (choice == null) return false;
  return state.startScreenShare(monitor: choice.monitor, preset: choice.preset);
}

class _ScreenSourceDialog extends StatefulWidget {
  const _ScreenSourceDialog({required this.sources});
  final ScreenSources sources;

  @override
  State<_ScreenSourceDialog> createState() => _ScreenSourceDialogState();
}

class _ScreenSourceDialogState extends State<_ScreenSourceDialog> {
  late int _monitor = widget.sources.monitors.any(
          (m) => m.index == widget.sources.selectedMonitor)
      ? widget.sources.selectedMonitor
      : (widget.sources.monitors.isEmpty ? 1 : widget.sources.monitors.first.index);
  late ScreenPreset _preset = widget.sources.selectedPreset;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final sources = widget.sources;
    return TcDialogShell(
      title: screenSourceDialogTitle,
      width: 460,
      actions: [
        TcGhostButton(
          label: 'CANCEL',
          onPressed: () => Navigator.pop(context),
        ),
        TcPrimaryButton(
          label: 'SHARE',
          onPressed: sources.available && sources.monitors.isNotEmpty
              ? () => Navigator.pop(context, (monitor: _monitor, preset: _preset))
              : null,
        ),
      ],
      children: [
        if (!sources.available)
          Text(
            sources.reason.isEmpty
                ? 'This machine cannot capture its screen.'
                : 'This machine cannot capture its screen: ${sources.reason}',
            style: TextStyle(fontSize: TCType.textBodySm, color: tc.statusWarn),
          )
        else ...[
          Wrap(
            spacing: 10,
            runSpacing: 10,
            children: [
              for (final monitor in sources.monitors)
                _MonitorTile(
                  monitor: monitor,
                  selected: monitor.index == _monitor,
                  onTap: () => setState(() => _monitor = monitor.index),
                ),
            ],
          ),
          const SizedBox(height: 12),
          Row(
            children: [
              for (final preset in ScreenPreset.values) ...[
                _PresetChip(
                  preset: preset,
                  selected: preset == _preset,
                  onTap: () => setState(() => _preset = preset),
                ),
                const SizedBox(width: 8),
              ],
            ],
          ),
        ],
        const SizedBox(height: 12),
        Text(
          screenSourceDialogNote,
          style: TextStyle(
            fontSize: TCType.textMicro,
            height: TCType.leadingBody,
            color: tc.textTertiary,
          ),
        ),
      ],
    );
  }
}

class _MonitorTile extends StatelessWidget {
  const _MonitorTile({
    required this.monitor,
    required this.selected,
    required this.onTap,
  });

  final ScreenSource monitor;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final thumbnail = monitor.thumbnail;
    return GestureDetector(
      onTap: onTap,
      child: Container(
        width: 200,
        padding: const EdgeInsets.all(6),
        decoration: BoxDecoration(
          color: selected ? tc.accentPrimaryMuted : tc.bgInset,
          border: Border.all(color: selected ? tc.accentPrimary : tc.borderDefault),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            SizedBox(
              height: 105,
              width: double.infinity,
              child: thumbnail == null
                  ? Container(color: tc.bgSurface)
                  : peerImage(base64Decode(thumbnail), size: 188, fit: BoxFit.contain),
            ),
            const SizedBox(height: 6),
            Text(
              'MONITOR ${monitor.index} · ${monitor.width}×${monitor.height}',
              style: TextStyle(
                fontSize: TCType.textMicro,
                color: selected ? tc.accentPrimary : tc.textSecondary,
                letterSpacing:
                    TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWide),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _PresetChip extends StatelessWidget {
  const _PresetChip({
    required this.preset,
    required this.selected,
    required this.onTap,
  });

  final ScreenPreset preset;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final (label, detail) = switch (preset) {
      ScreenPreset.clearer => ('CLEARER', '1080p · 15 fps'),
      ScreenPreset.smoother => ('SMOOTHER', '720p · 30 fps'),
    };
    return GestureDetector(
      onTap: onTap,
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
        decoration: BoxDecoration(
          color: selected ? tc.accentPrimaryMuted : tc.bgInset,
          border: Border.all(color: selected ? tc.accentPrimary : tc.borderDefault),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              label,
              style: TextStyle(
                fontSize: TCType.textMicro,
                color: selected ? tc.accentPrimary : tc.textEmphasis,
                letterSpacing:
                    TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWide),
              ),
            ),
            Text(
              detail,
              style: TextStyle(fontSize: TCType.textMicro, color: tc.textTertiary),
            ),
          ],
        ),
      ),
    );
  }
}
