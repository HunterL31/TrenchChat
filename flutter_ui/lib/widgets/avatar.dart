// Port of components/data-display/Avatar.jsx.
import 'dart:typed_data';

import 'package:flutter/material.dart';

import '../theme/tokens.dart';
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
              child: Image.memory(
                imageBytes!,
                width: size,
                height: size,
                fit: BoxFit.cover,
              ),
            )
          else
            Container(
              width: size,
              height: size,
              alignment: Alignment.center,
              decoration: BoxDecoration(
                color: TCColors.bgInset,
                border: Border.all(color: TCColors.borderDefault),
                borderRadius: BorderRadius.circular(TCSpace.radiusSm),
              ),
              child: Text(
                initial,
                style: TextStyle(
                  color: TCColors.accentPrimary,
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
