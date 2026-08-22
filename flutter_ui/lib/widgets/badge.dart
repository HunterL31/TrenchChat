// Reaction chip: emoji + count, highlighted when the viewer reacted.
import 'dart:typed_data';

import 'package:flutter/material.dart';

import '../theme/effects.dart';
import '../theme/section_theme.dart';
import '../theme/shape.dart';
import '../theme/tokens.dart';
import 'peer_image.dart';

final RegExp _sha256Hex = RegExp(r'^[0-9a-fA-F]{64}$');

class ReactionChip extends StatefulWidget {
  const ReactionChip({
    super.key,
    required this.emoji,
    required this.count,
    required this.reactedByMe,
    this.imageBytes,
    this.onTap,
  });

  /// The reaction key: a unicode emoji, or a SHA-256 hash for a custom emoji.
  final String emoji;
  final int count;
  final bool reactedByMe;

  /// Custom-emoji image when the hash resolved locally; a hash with no image
  /// renders as '?' like the Qt chip.
  final Uint8List? imageBytes;
  final VoidCallback? onTap;

  @override
  State<ReactionChip> createState() => _ReactionChipState();
}

class _ReactionChipState extends State<ReactionChip> {
  bool _hover = false;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final bg = widget.reactedByMe ? tc.accentPrimaryMuted : tc.bgInset;
    final border = widget.reactedByMe ? tc.borderAccent : tc.borderDefault;
    final fg = widget.reactedByMe ? tc.accentPrimary : tc.textSecondary;

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
            border: Border.all(color: _hover ? tc.accentPrimary : border),
            borderRadius: tcCorners(context, stock: TCSpace.radiusSm, scale: 0.5),
          ),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              if (widget.imageBytes != null)
                peerImage(widget.imageBytes!, size: 14)
              else
                Text(
                  _sha256Hex.hasMatch(widget.emoji) ? '?' : widget.emoji,
                  style: TextStyle(fontSize: TCType.textCaption, color: fg),
                ),
              const SizedBox(width: 4),
              Text(
                '${widget.count}',
                style: TextStyle(fontSize: TCType.textCaption, color: fg),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
