// Port of components/data-display/Avatar.jsx.
import 'dart:typed_data';

import 'package:flutter/material.dart';

import '../theme/section_theme.dart';
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
  });

  final String name;
  final Uint8List? imageBytes;
  final double size;
  final PresenceStatus? status;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final trimmed = name.trim();
    final initial = trimmed.isEmpty ? '?' : trimmed[0].toUpperCase();

    return SizedBox(
      width: size,
      height: size,
      child: Stack(
        clipBehavior: Clip.none,
        children: [
          if (imageBytes != null)
            ClipRRect(
              borderRadius: BorderRadius.circular(TCSpace.radiusSm),
              child: peerImage(imageBytes!, size: size, fit: BoxFit.cover),
            )
          else
            Container(
              width: size,
              height: size,
              alignment: Alignment.center,
              decoration: BoxDecoration(
                color: tc.bgInset,
                border: Border.all(color: tc.borderDefault),
                borderRadius: BorderRadius.circular(TCSpace.radiusSm),
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
            ),
          if (status != null)
            Positioned(
              right: -2,
              bottom: -2,
              child: StatusDot(status: status!, size: (size * 0.28).clamp(8, double.infinity)),
            ),
        ],
      ),
    );
  }
}
