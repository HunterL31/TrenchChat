// 1b: 60px server rail.
import 'package:flutter/material.dart';

import '../../theme/effects.dart';
import '../../theme/glow.dart';
import '../../theme/section_theme.dart';
import '../../theme/shape.dart';
import '../../theme/tokens.dart';
import '../../widgets/dashed_border.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_context_menu.dart';
import '../../widgets/tc_icon.dart';
import '../../widgets/tc_tooltip.dart';

/// The square a server tile occupies, and the size its shape is cut to.
const double _tileSize = 38;

class ServerRailEntry {
  const ServerRailEntry({
    required this.hash,
    required this.name,
    this.canInvite = false,
    this.canManage = false,
  });

  final String hash;
  final String name;

  /// This reader's INVITE permission on the server, gating the rail menu's
  /// "Invite…" item.
  final bool canInvite;

  /// This reader's MANAGE_CHANNEL permission, gating "Edit permissions…".
  final bool canManage;
}

class ServerRail extends StatelessWidget {
  const ServerRail({
    super.key,
    required this.servers,
    required this.selectedHash,
    required this.onSelect,
    this.onHome,
    this.onAddServer,
    this.onSettings,
    this.onLeaveServer,
    this.onInviteServer,
    this.onEditServerPermissions,
  });

  final List<ServerRailEntry> servers;
  final String? selectedHash;
  final ValueChanged<String> onSelect;

  /// Deselects the current server and returns to the DIRECT CHANNELS view;
  /// wired to the >_ logo so an owner of a server can always get back home.
  final VoidCallback? onHome;

  final VoidCallback? onAddServer;
  final VoidCallback? onSettings;

  /// Server-tile right-click actions, each handed the server's hash. A null
  /// callback, or a false permission flag, leaves that item out of the menu.
  final ValueChanged<String>? onLeaveServer;
  final ValueChanged<String>? onInviteServer;
  final ValueChanged<String>? onEditServerPermissions;

  List<TcContextMenuItem> _menuFor(ServerRailEntry s) => [
        if (onInviteServer != null && s.canInvite)
          TcContextMenuItem(label: 'Invite…', onTap: () => onInviteServer!(s.hash)),
        if (onEditServerPermissions != null && s.canManage)
          TcContextMenuItem(
              label: 'Edit permissions…', onTap: () => onEditServerPermissions!(s.hash)),
        if (onLeaveServer != null)
          TcContextMenuItem(label: 'Leave server', onTap: () => onLeaveServer!(s.hash)),
      ];

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
          TcTooltip(
            message: onHome != null ? 'Home — direct channels' : '',
            child: MouseRegion(
              cursor: onHome != null ? SystemMouseCursors.click : SystemMouseCursors.basic,
              child: GestureDetector(
                onTap: onHome,
                child: Text(
                  '>_',
                  style: TextStyle(
                    fontFamily: SectionTheme.styleOf(context).displayFont,
                    fontSize: 26,
                    height: 1,
                    color: tc.accentPrimary,
                    shadows: tcTextGlow(context),
                  ),
                ),
              ),
            ),
          ),
          const SizedBox(height: 12),
          Container(width: 28, height: 1, color: tc.borderSubtle),
          const SizedBox(height: 12),
          for (final s in servers) ...[
            _ServerTile(
              label: _initials(s.name),
              tooltip: s.name,
              selected: s.hash == selectedHash,
              onTap: () => onSelect(s.hash),
              menuItems: _menuFor(s),
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
  const _ServerTile({
    required this.label,
    required this.tooltip,
    required this.selected,
    required this.onTap,
    this.menuItems = const [],
  });

  final String label;
  final String tooltip;
  final bool selected;
  final VoidCallback onTap;
  final List<TcContextMenuItem> menuItems;

  @override
  State<_ServerTile> createState() => _ServerTileState();
}

class _ServerTileState extends State<_ServerTile> {
  bool _hover = false;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final selected = widget.selected;
    return TcContextMenuRegion(
      items: widget.menuItems,
      child: TcTooltip(
        message: widget.tooltip,
        child: MouseRegion(
          cursor: SystemMouseCursors.click,
          onEnter: (_) => setState(() => _hover = true),
          onExit: (_) => setState(() => _hover = false),
          child: GestureDetector(
            onTap: widget.onTap,
            child: AnimatedContainer(
          duration: TCEffects.durationMed,
          curve: TCEffects.easeTerminal,
          width: _tileSize,
          height: _tileSize,
          alignment: Alignment.center,
          decoration: BoxDecoration(
            color: selected ? tc.bgSelected : tc.bgInset,
            border: Border.all(
              color: selected
                  ? tc.borderAccent
                  : (_hover ? tc.borderStrong : tc.borderDefault),
            ),
            borderRadius: tcAvatarCorners(context, _tileSize),
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
          borderRadius: tcAvatarCorners(context, _tileSize) ?? BorderRadius.zero,
          child: SizedBox(
            width: _tileSize,
            height: _tileSize,
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
