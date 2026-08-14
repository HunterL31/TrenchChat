// Mirrors trenchchat/gui/channel_view.py's _name_color() exactly, so the Qt
// client and this UI agree on every sender's display color.
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
