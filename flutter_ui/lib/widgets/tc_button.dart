// Ghost button + icon button, hover-only per the design readme: "buttons
// don't move, they light up." Hover brightens; press darkens to bg-pressed.
import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../theme/effects.dart';
import '../theme/section_theme.dart';
import '../theme/shape.dart';
import '../theme/tokens.dart';
import 'tc_icon.dart';
import 'tc_tooltip.dart';

/// Height of a standard row control: ghost button, primary button, default
/// icon button. Every one of them measures exactly this, border included.
const double tcControlHeight = 30;

/// Height of header and column chrome: tabs, pills, compact icon buttons.
const double tcChromeHeight = 26;

/// Fraction of an icon button's box its glyph occupies, so a 22 lp button
/// does not carry the same glyph a 30 lp one does.
const double _iconButtonGlyphFactor = 0.47;

/// Width of the widest of [labels] in [style], so a button that swaps its
/// label mid-action reserves room for both and never resizes.
double _widest(List<String> labels, TextStyle style) {
  final painter = TextPainter(textDirection: TextDirection.ltr);
  var widest = 0.0;
  for (final label in labels) {
    painter.text = TextSpan(text: label, style: style);
    painter.layout();
    widest = math.max(widest, painter.width);
  }
  painter.dispose();
  return widest;
}

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
        child: SizedBox(
          height: tcControlHeight,
          child: AnimatedContainer(
            duration: TCEffects.durationMed,
            curve: TCEffects.easeTerminal,
            padding: const EdgeInsets.symmetric(horizontal: TCSpace.space3),
            decoration: BoxDecoration(
              color: bg,
              border: Border.all(color: border),
              borderRadius: tcCorners(context, scale: 0.5),
            ),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              mainAxisAlignment: MainAxisAlignment.center,
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
                      letterSpacing:
                          TCType.letterSpacingFor(TCType.textCaption, TCType.trackingWide),
                    ),
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

/// Horizontal padding that, with the 1 lp border, puts a primary button's
/// label on the same inset a ghost button's sits on.
const double _primaryInset = TCSpace.space4 - 1;

/// Filled variant for a dialog's confirming action (Create, Join, ...).
/// Same hover-brightens/press-darkens rule as [TcGhostButton], just filled
/// with the accent color instead of outlined.
class TcPrimaryButton extends StatefulWidget {
  const TcPrimaryButton({
    super.key,
    required this.label,
    required this.onPressed,
    this.busyLabel,
    this.busy = false,
  });

  final String label;

  /// What the button reads while the action is in flight. The button always
  /// reserves the wider of the two labels, so starting the action does not
  /// resize it and shove the rest of the action row sideways.
  final String? busyLabel;
  final bool busy;

  final VoidCallback? onPressed;

  @override
  State<TcPrimaryButton> createState() => _TcPrimaryButtonState();
}

class _TcPrimaryButtonState extends State<TcPrimaryButton> {
  bool _hover = false;
  bool _pressed = false;

  Widget _label(Color fg) {
    final style = TextStyle(
      fontSize: TCType.textCaption,
      color: fg,
      letterSpacing: TCType.letterSpacingFor(TCType.textCaption, TCType.trackingWide),
    );
    final busyLabel = widget.busyLabel;
    final text = Text(widget.busy && busyLabel != null ? busyLabel : widget.label, style: style);
    if (busyLabel == null) return text;
    return SizedBox(width: _widest([widget.label, busyLabel], style), child: Center(child: text));
  }

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
    final Color border = disabled ? tc.borderDefault : bg;

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
        child: SizedBox(
          height: tcControlHeight,
          child: AnimatedContainer(
            duration: TCEffects.durationMed,
            curve: TCEffects.easeTerminal,
            padding: const EdgeInsets.symmetric(horizontal: _primaryInset),
            decoration: BoxDecoration(
              color: bg,
              border: Border.all(color: border),
              borderRadius: tcCorners(context, scale: 0.5),
            ),
            child: Center(widthFactor: 1, child: _label(fg)),
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
    this.size = tcControlHeight,
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
              size: (widget.size * _iconButtonGlyphFactor).roundToDouble(),
              color: _hover ? tc.textPrimary : tc.textSecondary,
            ),
          ),
        ),
      ),
    );
  }
}
