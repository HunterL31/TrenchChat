// A minimal themed right-click menu -- there is no other right-click
// handling anywhere in this app. A raw Material PopupMenuButton/showMenu
// would render wrong for the same reason tc_dialog.dart's header comment
// warns about for AlertDialog: buildAppTheme() sets no popupMenuTheme, so
// it would come out with default M3 rounded corners and surface tint,
// clashing with this terminal-styled design language. This is built the
// same way as showTcDialog -- an Overlay entry styled from theme/tokens.dart
// -- instead of reaching for that Material primitive.
import 'dart:async';

import 'package:flutter/material.dart';

import '../theme/effects.dart';
import '../theme/section_theme.dart';
import '../theme/shape.dart';
import '../theme/tokens.dart';

class TcContextMenuItem {
  const TcContextMenuItem({required this.label, required this.onTap});

  final String label;
  final VoidCallback onTap;
}

/// Margin kept between the menu and the screen edge when it has to be nudged
/// back on-screen.
const double _edgeMargin = 8;

/// Shows [items] as a themed popup anchored at the global [position] (e.g.
/// from `onSecondaryTapDown`'s or `onLongPressStart`'s `details.globalPosition`).
/// Dismisses on tap-outside, or after an item is chosen.
Future<void> showTcContextMenu({
  required BuildContext context,
  required Offset position,
  required List<TcContextMenuItem> items,
}) {
  final overlay = Overlay.of(context);
  final completer = Completer<void>();
  late OverlayEntry entry;

  void dismiss() {
    entry.remove();
    if (!completer.isCompleted) completer.complete();
  }

  entry = OverlayEntry(
    builder: (context) => Stack(
      children: [
        Positioned.fill(
          child: GestureDetector(
            behavior: HitTestBehavior.opaque,
            onTap: dismiss,
            onSecondaryTap: dismiss,
          ),
        ),
        Positioned.fill(
          child: CustomSingleChildLayout(
            delegate: _TcContextMenuLayout(position),
            child: _TcContextMenuPanel(items: items, onSelected: dismiss),
          ),
        ),
      ],
    ),
  );

  overlay.insert(entry);
  return completer.future;
}

/// Wraps [child] so both a right-click and a long-press open the same menu.
/// The long-press path is what makes these menus reachable on touch devices,
/// which have no secondary tap at all. Renders [child] alone when [items] is
/// empty, so a row with nothing to offer stays gesture-free.
class TcContextMenuRegion extends StatelessWidget {
  const TcContextMenuRegion({super.key, required this.items, required this.child});

  final List<TcContextMenuItem> items;
  final Widget child;

  @override
  Widget build(BuildContext context) {
    if (items.isEmpty) return child;
    void show(Offset position) =>
        showTcContextMenu(context: context, position: position, items: items);
    return GestureDetector(
      onSecondaryTapDown: (details) => show(details.globalPosition),
      onLongPressStart: (details) => show(details.globalPosition),
      child: child,
    );
  }
}

/// Anchors the panel at the tap point, pulling it back inside the screen when
/// it would otherwise run off the right or bottom edge -- the common case on a
/// phone, where the tap point is often close to both.
class _TcContextMenuLayout extends SingleChildLayoutDelegate {
  const _TcContextMenuLayout(this.position);

  final Offset position;

  @override
  BoxConstraints getConstraintsForChild(BoxConstraints constraints) => constraints.loosen();

  @override
  Offset getPositionForChild(Size size, Size childSize) {
    double axis(double wanted, double child, double available) {
      if (child + _edgeMargin >= available) return 0;
      return wanted.clamp(0, available - child - _edgeMargin).toDouble();
    }

    return Offset(
      axis(position.dx, childSize.width, size.width),
      axis(position.dy, childSize.height, size.height),
    );
  }

  @override
  bool shouldRelayout(_TcContextMenuLayout oldDelegate) => oldDelegate.position != position;
}

class _TcContextMenuPanel extends StatelessWidget {
  const _TcContextMenuPanel({required this.items, required this.onSelected});

  final List<TcContextMenuItem> items;
  final VoidCallback onSelected;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final corners = tcCorners(context, scale: 0.5);
    // A Stack child positioned by left/top alone is laid out unbounded, so the
    // stretched rows below need an explicit width to resolve against.
    return Material(
      type: MaterialType.transparency,
      child: ConstrainedBox(
        constraints: const BoxConstraints(minWidth: 160, maxWidth: 320),
        child: IntrinsicWidth(
          child: Container(
            clipBehavior: corners == null ? Clip.none : Clip.antiAlias,
            decoration: BoxDecoration(
              color: tc.bgSurfaceRaised,
              border: Border.all(color: tc.borderDefault),
              borderRadius: corners,
              boxShadow: TCEffects.shadowModal,
            ),
            padding: const EdgeInsets.symmetric(vertical: 4),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                for (final item in items) _TcContextMenuRow(item: item, onSelected: onSelected),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _TcContextMenuRow extends StatefulWidget {
  const _TcContextMenuRow({required this.item, required this.onSelected});

  final TcContextMenuItem item;
  final VoidCallback onSelected;

  @override
  State<_TcContextMenuRow> createState() => _TcContextMenuRowState();
}

class _TcContextMenuRowState extends State<_TcContextMenuRow> {
  bool _hover = false;

  void _handleTap() {
    widget.onSelected();
    widget.item.onTap();
  }

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    return MouseRegion(
      cursor: SystemMouseCursors.click,
      onEnter: (_) => setState(() => _hover = true),
      onExit: (_) => setState(() => _hover = false),
      child: GestureDetector(
        onTap: _handleTap,
        child: AnimatedContainer(
          duration: TCEffects.durationFast,
          curve: TCEffects.easeTerminal,
          color: _hover ? tc.bgHover : Colors.transparent,
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
          child: Text(
            widget.item.label,
            style: TextStyle(fontSize: TCType.textBodySm, color: tc.textSecondary),
          ),
        ),
      ),
    );
  }
}
