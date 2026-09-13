import 'dart:typed_data';

import 'package:flutter/material.dart';

/// Decode headroom over the drawn size, covering any realistic display DPI.
const int _decodeScale = 3;

/// [Image.memory] for bytes that came from a peer: bounded, and never fatal.
///
/// Image.memory decodes at the raster the file *declares*, not the box it is
/// drawn into, so a small file claiming huge dimensions costs the full
/// allocation before being scaled down. The backend refuses an implausible
/// declared decode, but that ceiling is per image and a screen holds one per
/// peer -- cacheWidth makes the cost track the display size. Only the width
/// is capped: passing both axes picks the exact resize policy, which squashes
/// a non-square source to a square raster before [fit] ever runs.
///
/// errorBuilder matters for the same reason: bytes that reached storage
/// without parsing as an image are stored as-is by design, so a decode
/// failure here is expected rather than exceptional. [fallback] is what a
/// caller with something better than a hole shows in its place.
Widget peerImage(
  Uint8List bytes, {
  required double size,
  BoxFit fit = BoxFit.contain,
  Widget? fallback,
}) {
  return Image.memory(
    bytes,
    width: size,
    height: size,
    fit: fit,
    filterQuality: FilterQuality.medium,
    cacheWidth: (size * _decodeScale).ceil(),
    errorBuilder: (context, error, stack) =>
        fallback ?? SizedBox(width: size, height: size),
  );
}
