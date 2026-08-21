// Theme codes: a whole named [ThemeSpec] packed into one text token, small
// enough to paste into a chat message.
//
// Wire shape: `tct1:<base64url-no-padding(raw DEFLATE(minified JSON))>`,
// where the JSON is the spec's `toJson()` with its empty maps dropped, plus
// a `name` key. The token's alphabet is deliberately URL-safe base64 so a
// code survives every place a message ends up, and so [themeCodeRe] can find
// one in running text the same way an emoji token is found.
//
// Decoding is total: a code this client cannot read -- wrong prefix, damaged
// payload, JSON that is not an object -- yields null and stays literal text
// rather than throwing, which is what lets a newer client's token pass
// through an older one unharmed.
import 'dart:convert';

import 'package:archive/archive.dart';

import 'theme_spec.dart';

/// The prefix every code this client writes carries, version included.
const String themeCodePrefix = 'tct1:';

/// The name a code carrying none is read under.
const String defaultThemeCodeName = 'theme';

/// The longest name a code carries; the backend's library rejects longer.
const int maxThemeNameLength = 64;

/// Matches a theme code token inside message text.
final RegExp themeCodeRe = RegExp('$themeCodePrefix[A-Za-z0-9_-]+');

/// Packs [name] and [spec] into a shareable code.
String encodeThemeCode(String name, ThemeSpec spec) {
  final doc = spec.toJson()..removeWhere((_, value) => value is Map && value.isEmpty);
  doc['name'] = _clampName(name);
  final deflated = Deflate(utf8.encode(jsonEncode(doc))).getBytes();
  return '$themeCodePrefix${base64Url.encode(deflated).replaceAll('=', '')}';
}

/// Unpacks a code, or null when it is not one this client can read.
({String name, ThemeSpec spec})? decodeThemeCode(String code) {
  final token = code.trim();
  if (!token.startsWith(themeCodePrefix)) return null;
  final payload = token.substring(themeCodePrefix.length);
  if (payload.isEmpty) return null;
  final Object? doc;
  try {
    final packed = base64Url.decode(payload.padRight((payload.length + 3) & ~3, '='));
    doc = jsonDecode(utf8.decode(Inflate(packed).getBytes()));
  } catch (_) {
    return null;
  }
  if (doc is! Map<String, dynamic>) return null;
  final rawName = doc['name'];
  final name = rawName is String && rawName.trim().isNotEmpty
      ? _clampName(rawName.trim())
      : defaultThemeCodeName;
  return (name: name, spec: ThemeSpec.fromJson(doc));
}

String _clampName(String name) {
  final trimmed = name.trim();
  if (trimmed.isEmpty) return defaultThemeCodeName;
  return trimmed.length <= maxThemeNameLength
      ? trimmed
      : trimmed.substring(0, maxThemeNameLength);
}
