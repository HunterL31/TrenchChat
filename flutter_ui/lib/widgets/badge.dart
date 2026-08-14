// Reaction chip: emoji + count, highlighted when the viewer reacted.
import 'package:flutter/material.dart';

import '../theme/effects.dart';
import '../theme/tokens.dart';

class ReactionChip extends StatefulWidget {
  const ReactionChip({
    super.key,
    required this.emoji,
    required this.count,
    required this.reactedByMe,
    this.onTap,
  });

  final String emoji;
  final int count;
  final bool reactedByMe;
  final VoidCallback? onTap;

  @override
  State<ReactionChip> createState() => _ReactionChipState();
}

class _ReactionChipState extends State<ReactionChip> {
  bool _hover = false;

  @override
  Widget build(BuildContext context) {
    final bg = widget.reactedByMe ? TCColors.accentPrimaryMuted : TCColors.bgInset;
    final border = widget.reactedByMe ? TCColors.borderAccent : TCColors.borderDefault;
    final fg = widget.reactedByMe ? TCColors.accentPrimary : TCColors.textSecondary;

    return MouseRegion(
      cursor: SystemMouseCursors.click,
      onEnter: (_) => setState(() => _hover = true),
      onExit: (_) => setState(() => _hover = false),
      child: GestureDetector(
        onTap: widget.onTap,
        child: AnimatedContainer(
          duration: TCEffects.durationMed,
          curve: TCEffects.easeTerminal,
          padding: const EdgeInsets.symmetric(horizontal: 7, vertical: 2),
          decoration: BoxDecoration(
            color: bg,
            border: Border.all(color: _hover ? TCColors.accentPrimary : border),
            borderRadius: BorderRadius.circular(TCSpace.radiusSm),
          ),
          child: Text(
            '${widget.emoji} ${widget.count}',
            style: TextStyle(fontSize: TCType.textCaption, color: fg),
          ),
        ),
      ),
    );
  }
}
