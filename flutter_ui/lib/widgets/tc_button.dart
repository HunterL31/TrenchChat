// Ghost button + icon button, hover-only per the design readme: "buttons
// don't move, they light up." Hover brightens; press darkens to bg-pressed.
import 'package:flutter/material.dart';

import '../theme/effects.dart';
import '../theme/tokens.dart';

class TcGhostButton extends StatefulWidget {
  const TcGhostButton({
    super.key,
    required this.label,
    required this.onPressed,
    this.icon,
  });

  final String label;
  final String? icon;
  final VoidCallback? onPressed;

  @override
  State<TcGhostButton> createState() => _TcGhostButtonState();
}

class _TcGhostButtonState extends State<TcGhostButton> {
  bool _hover = false;
  bool _pressed = false;

  @override
  Widget build(BuildContext context) {
    final disabled = widget.onPressed == null;
    final Color bg = _pressed
        ? TCColors.bgPressed
        : _hover
            ? TCColors.bgHover
            : Colors.transparent;
    final Color fg = disabled
        ? TCColors.textDisabled
        : _hover
            ? TCColors.textPrimary
            : TCColors.textSecondary;
    final Color border = _hover ? TCColors.borderStrong : TCColors.borderDefault;

    return MouseRegion(
      cursor: disabled ? SystemMouseCursors.basic : SystemMouseCursors.click,
      onEnter: (_) => setState(() => _hover = true),
      onExit: (_) => setState(() {
        _hover = false;
        _pressed = false;
      }),
      child: GestureDetector(
        onTapDown: disabled ? null : (_) => setState(() => _pressed = true),
        onTapUp: disabled ? null : (_) => setState(() => _pressed = false),
        onTapCancel: disabled ? null : () => setState(() => _pressed = false),
        onTap: widget.onPressed,
        child: AnimatedContainer(
          duration: TCEffects.durationMed,
          curve: TCEffects.easeTerminal,
          padding: const EdgeInsets.symmetric(horizontal: TCSpace.space3, vertical: 6),
          decoration: BoxDecoration(
            color: bg,
            border: Border.all(color: border),
          ),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              if (widget.icon != null) ...[
                Text(widget.icon!, style: TextStyle(fontSize: TCType.textCaption, color: fg)),
                const SizedBox(width: 6),
              ],
              Flexible(
                child: Text(
                  widget.label,
                  overflow: TextOverflow.ellipsis,
                  softWrap: false,
                  style: TextStyle(
                    fontSize: TCType.textCaption,
                    color: fg,
                    letterSpacing: TCType.letterSpacingFor(TCType.textCaption, TCType.trackingWide),
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class TcIconButton extends StatefulWidget {
  const TcIconButton({
    super.key,
    required this.icon,
    required this.tooltip,
    required this.onPressed,
    this.size = 30,
  });

  final String icon;
  final String tooltip;
  final double size;
  final VoidCallback? onPressed;

  @override
  State<TcIconButton> createState() => _TcIconButtonState();
}

class _TcIconButtonState extends State<TcIconButton> {
  bool _hover = false;

  @override
  Widget build(BuildContext context) {
    return Tooltip(
      message: widget.tooltip,
      child: MouseRegion(
        cursor: SystemMouseCursors.click,
        onEnter: (_) => setState(() => _hover = true),
        onExit: (_) => setState(() => _hover = false),
        child: GestureDetector(
          onTap: widget.onPressed,
          child: AnimatedContainer(
            duration: TCEffects.durationMed,
            curve: TCEffects.easeTerminal,
            width: widget.size,
            height: widget.size,
            alignment: Alignment.center,
            decoration: BoxDecoration(
              color: _hover ? TCColors.bgHover : Colors.transparent,
              border: Border.all(color: _hover ? TCColors.borderStrong : TCColors.borderDefault),
            ),
            child: Text(
              widget.icon,
              style: TextStyle(
                fontSize: 14,
                color: _hover ? TCColors.textPrimary : TCColors.textSecondary,
              ),
            ),
          ),
        ),
      ),
    );
  }
}
