// Ghost button + icon button, hover-only per the design readme: "buttons
// don't move, they light up." Hover brightens; press darkens to bg-pressed.
import 'package:flutter/material.dart';

import '../theme/effects.dart';
import '../theme/section_theme.dart';
import '../theme/shape.dart';
import '../theme/tokens.dart';
import 'tc_icon.dart';
import 'tc_tooltip.dart';

class TcGhostButton extends StatefulWidget {
  const TcGhostButton({
    super.key,
    required this.label,
    required this.onPressed,
    this.icon,
    this.accent,
  });

  final String label;
  final TcIconData? icon;
  final VoidCallback? onPressed;

  /// Paints the label and border in this color instead of the neutral pair --
  /// for a button whose meaning is a warning (a delete confirmation).
  final Color? accent;

  @override
  State<TcGhostButton> createState() => _TcGhostButtonState();
}

class _TcGhostButtonState extends State<TcGhostButton> {
  bool _hover = false;
  bool _pressed = false;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final disabled = widget.onPressed == null;
    final Color bg = _pressed
        ? tc.bgPressed
        : _hover
            ? tc.bgHover
            : Colors.transparent;
    final Color fg = disabled
        ? tc.textDisabled
        : widget.accent ?? (_hover ? tc.textPrimary : tc.textSecondary);
    final Color border = widget.accent ?? (_hover ? tc.borderStrong : tc.borderDefault);

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
            borderRadius: tcCorners(context, scale: 0.5),
          ),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              if (widget.icon != null) ...[
                TcIcon(widget.icon!, size: TCType.textCaption, color: fg),
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

/// Filled variant for a dialog's confirming action (Create, Join, ...).
/// Same hover-brightens/press-darkens rule as [TcGhostButton], just filled
/// with the accent color instead of outlined.
class TcPrimaryButton extends StatefulWidget {
  const TcPrimaryButton({
    super.key,
    required this.label,
    required this.onPressed,
  });

  final String label;
  final VoidCallback? onPressed;

  @override
  State<TcPrimaryButton> createState() => _TcPrimaryButtonState();
}

class _TcPrimaryButtonState extends State<TcPrimaryButton> {
  bool _hover = false;
  bool _pressed = false;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final disabled = widget.onPressed == null;
    final Color bg = disabled
        ? tc.bgInset
        : _pressed
            ? tc.accentPrimaryActive
            : _hover
                ? tc.accentPrimaryHover
                : tc.accentPrimary;
    final Color fg = disabled ? tc.textDisabled : tc.textOnAccent;

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
          padding: const EdgeInsets.symmetric(horizontal: TCSpace.space4, vertical: 8),
          decoration: BoxDecoration(color: bg, borderRadius: tcCorners(context, scale: 0.5)),
          child: Text(
            widget.label,
            style: TextStyle(
              fontSize: TCType.textCaption,
              color: fg,
              letterSpacing: TCType.letterSpacingFor(TCType.textCaption, TCType.trackingWide),
            ),
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

  final TcIconData icon;
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
    final tc = SectionTheme.of(context);
    return TcTooltip(
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
              color: _hover ? tc.bgHover : Colors.transparent,
              border: Border.all(color: _hover ? tc.borderStrong : tc.borderDefault),
              borderRadius: tcCorners(context, scale: 0.5),
            ),
            child: TcIcon(
              widget.icon,
              size: 14,
              color: _hover ? tc.textPrimary : tc.textSecondary,
            ),
          ),
        ),
      ),
    );
  }
}
