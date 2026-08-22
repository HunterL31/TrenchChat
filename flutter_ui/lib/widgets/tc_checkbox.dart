// Checkbox in the terminal design language: hairline border, solid green
// inner square when checked, hard corners until a theme rounds them. No
// Material ripple.
import 'package:flutter/material.dart';

import '../theme/effects.dart';
import '../theme/section_theme.dart';
import '../theme/shape.dart';
import '../theme/tokens.dart';

class TcCheckbox extends StatefulWidget {
  const TcCheckbox({
    super.key,
    required this.value,
    required this.onChanged,
    this.label,
  });

  final bool value;
  final ValueChanged<bool>? onChanged;
  final String? label;

  @override
  State<TcCheckbox> createState() => _TcCheckboxState();
}

class _TcCheckboxState extends State<TcCheckbox> {
  bool _hover = false;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final disabled = widget.onChanged == null;
    final box = AnimatedContainer(
      duration: TCEffects.durationFast,
      curve: TCEffects.easeTerminal,
      width: 14,
      height: 14,
      padding: const EdgeInsets.all(3),
      decoration: BoxDecoration(
        color: tc.bgInset,
        border: Border.all(
          color: widget.value
              ? tc.borderAccent
              : (_hover && !disabled ? tc.borderStrong : tc.borderDefault),
        ),
        borderRadius: tcCorners(context, scale: 0.3),
      ),
      child: widget.value
          ? Container(
              decoration: BoxDecoration(
                color: disabled ? tc.textDisabled : tc.accentPrimary,
                borderRadius: tcCorners(context, scale: 0.2),
              ),
            )
          : null,
    );

    return MouseRegion(
      cursor: disabled ? SystemMouseCursors.basic : SystemMouseCursors.click,
      onEnter: (_) => setState(() => _hover = true),
      onExit: (_) => setState(() => _hover = false),
      child: GestureDetector(
        onTap: disabled ? null : () => widget.onChanged!(!widget.value),
        child: widget.label == null
            ? box
            : Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  box,
                  const SizedBox(width: 8),
                  Flexible(
                    child: Text(
                      widget.label!,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                        fontSize: TCType.textBodySm,
                        color: disabled ? tc.textDisabled : tc.textSecondary,
                      ),
                    ),
                  ),
                ],
              ),
      ),
    );
  }
}

/// Row of mutually exclusive boxed options -- the CHAT/MAP/IFACE tab and
/// access-preset look, reusable for any enum-ish choice.
class TcChoiceRow extends StatelessWidget {
  const TcChoiceRow({
    super.key,
    required this.options,
    required this.value,
    required this.onSelected,
  });

  /// Display label per option value, in render order.
  final Map<String, String> options;
  final String value;
  final ValueChanged<String>? onSelected;

  @override
  Widget build(BuildContext context) {
    return Wrap(
      spacing: 6,
      runSpacing: 6,
      children: [
        for (final entry in options.entries)
          _ChoiceChip(
            label: entry.value,
            selected: entry.key == value,
            onTap: onSelected == null ? null : () => onSelected!(entry.key),
          ),
      ],
    );
  }
}

class _ChoiceChip extends StatelessWidget {
  const _ChoiceChip({required this.label, required this.selected, required this.onTap});

  final String label;
  final bool selected;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final disabled = onTap == null;
    return GestureDetector(
      onTap: onTap,
      child: MouseRegion(
        cursor: disabled ? SystemMouseCursors.basic : SystemMouseCursors.click,
        child: AnimatedContainer(
          duration: TCEffects.durationMed,
          curve: TCEffects.easeTerminal,
          padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
          decoration: BoxDecoration(
            color: selected ? tc.bgSelected : Colors.transparent,
            border: Border.all(
              color: selected ? tc.borderAccent : tc.borderDefault,
            ),
            borderRadius: tcCorners(context, scale: 0.5),
          ),
          child: Text(
            label,
            style: TextStyle(
              fontSize: TCType.textCaption,
              letterSpacing: TCType.letterSpacingFor(TCType.textCaption, TCType.trackingWide),
              color: disabled
                  ? tc.textDisabled
                  : (selected ? tc.textEmphasis : tc.textSecondary),
            ),
          ),
        ),
      ),
    );
  }
}
