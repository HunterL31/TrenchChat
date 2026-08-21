// 1b: 60px server rail.
import 'package:flutter/material.dart';

import '../../theme/effects.dart';
import '../../theme/glow.dart';
import '../../theme/section_theme.dart';
import '../../theme/tokens.dart';
import '../../widgets/dashed_border.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_icon.dart';

class ServerRailEntry {
  const ServerRailEntry({required this.hash, required this.name});
  final String hash;
  final String name;
}

class ServerRail extends StatelessWidget {
  const ServerRail({
    super.key,
    required this.servers,
    required this.selectedHash,
    required this.onSelect,
    this.onAddServer,
    this.onSettings,
  });

  final List<ServerRailEntry> servers;
  final String? selectedHash;
  final ValueChanged<String> onSelect;
  final VoidCallback? onAddServer;
  final VoidCallback? onSettings;

  String _initials(String name) {
    final trimmed = name.trim();
    if (trimmed.isEmpty) return '?';
    final parts = trimmed.split(RegExp(r'[\s\-_]+')).where((p) => p.isNotEmpty).toList();
    if (parts.length >= 2) {
      return (parts[0][0] + parts[1][0]).toUpperCase();
    }
    return trimmed.substring(0, trimmed.length >= 2 ? 2 : 1).toUpperCase();
  }

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    return Container(
      width: 60,
      color: tc.bgApp,
      child: Column(
        children: [
          const SizedBox(height: 12),
          Text(
            '>_',
            style: TextStyle(
              fontFamily: SectionTheme.styleOf(context).displayFont,
              fontSize: 26,
              height: 1,
              color: tc.accentPrimary,
              shadows: tcTextGlow(context),
            ),
          ),
          const SizedBox(height: 12),
          Container(width: 28, height: 1, color: tc.borderSubtle),
          const SizedBox(height: 12),
          for (final s in servers) ...[
            _ServerTile(
              label: _initials(s.name),
              selected: s.hash == selectedHash,
              onTap: () => onSelect(s.hash),
            ),
            const SizedBox(height: 12),
          ],
          _AddServerTile(onTap: onAddServer),
          const Spacer(),
          Padding(
            padding: const EdgeInsets.only(bottom: 12),
            child: TcIconButton(
              icon: TcIcons.settings,
              tooltip: 'Settings',
              onPressed: onSettings,
            ),
          ),
        ],
      ),
    );
  }
}

class _ServerTile extends StatefulWidget {
  const _ServerTile({required this.label, required this.selected, required this.onTap});

  final String label;
  final bool selected;
  final VoidCallback onTap;

  @override
  State<_ServerTile> createState() => _ServerTileState();
}

class _ServerTileState extends State<_ServerTile> {
  bool _hover = false;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final selected = widget.selected;
    return MouseRegion(
      cursor: SystemMouseCursors.click,
      onEnter: (_) => setState(() => _hover = true),
      onExit: (_) => setState(() => _hover = false),
      child: GestureDetector(
        onTap: widget.onTap,
        child: AnimatedContainer(
          duration: TCEffects.durationMed,
          curve: TCEffects.easeTerminal,
          width: 38,
          height: 38,
          alignment: Alignment.center,
          decoration: BoxDecoration(
            color: selected ? tc.bgSelected : tc.bgInset,
            border: Border.all(
              color: selected
                  ? tc.borderAccent
                  : (_hover ? tc.borderStrong : tc.borderDefault),
            ),
            boxShadow: selected ? tcBoxGlowSm(context) : null,
          ),
          child: Text(
            widget.label,
            style: TextStyle(
              fontSize: 13,
              letterSpacing: TCType.letterSpacingFor(13, 0.04),
              color: selected ? tc.accentPrimary : tc.textSecondary,
            ),
          ),
        ),
      ),
    );
  }
}

class _AddServerTile extends StatefulWidget {
  const _AddServerTile({this.onTap});

  final VoidCallback? onTap;

  @override
  State<_AddServerTile> createState() => _AddServerTileState();
}

class _AddServerTileState extends State<_AddServerTile> {
  bool _hover = false;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final disabled = widget.onTap == null;
    return MouseRegion(
      cursor: disabled ? SystemMouseCursors.basic : SystemMouseCursors.click,
      onEnter: (_) => setState(() => _hover = true),
      onExit: (_) => setState(() => _hover = false),
      child: GestureDetector(
        onTap: widget.onTap,
        child: DashedBorder(
          color: _hover ? tc.borderStrong : tc.borderDefault,
          child: SizedBox(
            width: 38,
            height: 38,
            child: Center(
              child: Text(
                '+',
                style: TextStyle(
                  fontSize: 16,
                  color: _hover ? tc.textSecondary : tc.textTertiary,
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}
