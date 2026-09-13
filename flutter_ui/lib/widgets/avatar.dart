// Port of components/data-display/Avatar.jsx.
import 'dart:typed_data';

import 'package:flutter/material.dart';

import '../theme/section_theme.dart';
import '../theme/shape.dart';
import '../theme/tokens.dart';
import 'peer_image.dart';
import 'status_dot.dart';

class Avatar extends StatelessWidget {
  const Avatar({
    super.key,
    required this.name,
    this.imageBytes,
    this.size = 36,
    this.status,
    this.ringColor,
  });

  final String name;
  final Uint8List? imageBytes;
  final double size;
  final PresenceStatus? status;

  /// Surface the presence dot's ring is cut out of; see [StatusDot].
  final Color? ringColor;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final trimmed = name.trim();
    final initial = trimmed.isEmpty ? '?' : trimmed[0].toUpperCase();
    final corners = tcAvatarCorners(context, size, stock: TCSpace.radiusSm)!;
    final initialTile = Container(
      width: size,
      height: size,
      alignment: Alignment.center,
      decoration: BoxDecoration(
        color: tc.bgInset,
        border: Border.all(color: tc.borderDefault),
        borderRadius: corners,
      ),
      child: Text(
        initial,
        style: TextStyle(
          color: tc.accentPrimary,
          fontFamily: TCType.fontMono,
          fontWeight: TCType.weightSemibold,
          fontSize: size * 0.4,
        ),
      ),
    );

    return SizedBox(
      width: size,
      height: size,
      child: Stack(
        clipBehavior: Clip.none,
        children: [
          if (imageBytes != null)
            Container(
              width: size,
              height: size,
              clipBehavior: Clip.antiAlias,
              decoration: BoxDecoration(borderRadius: corners),
              foregroundDecoration: BoxDecoration(
                border: Border.all(color: tc.borderDefault),
                borderRadius: corners,
              ),
              child: peerImage(
                imageBytes!,
                size: size,
                fit: BoxFit.cover,
                fallback: initialTile,
              ),
            )
          else
            initialTile,
          if (status != null)
            Positioned(
              right: 0,
              bottom: 0,
              child: StatusDot(
                status: status!,
                size: (size * 0.28).clamp(8, double.infinity),
                ringColor: ringColor,
              ),
            ),
        ],
      ),
    );
  }
}
