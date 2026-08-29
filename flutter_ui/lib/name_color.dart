// Deterministic sender display colors: every client derives the same color
// for the same identity hash.
import 'dart:convert';

import 'package:crypto/crypto.dart';
import 'package:flutter/painting.dart';

const Color ownMessageColor = Color(0xFF7EB8F7); // "#7eb8f7"

Color nameColor(String identityHex, {required bool isOwn}) {
  if (isOwn) return ownMessageColor;
  final digest = md5.convert(utf8.encode(identityHex)).bytes;
  final hue = ((digest[0] << 8) | digest[1]) % 360;
  return HSVColor.fromAHSV(1.0, hue.toDouble(), 180 / 255, 220 / 255).toColor();
}
