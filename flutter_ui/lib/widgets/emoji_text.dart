// Inline custom-emoji rendering -- port of channel_view._render_content's
// token rewriting. `:name@hexhash:` resolves by 64-char SHA-256 hash;
// legacy `:name:` falls back to an exact name match. Unknown tokens stay as
// literal text (the request-from-sender path is backend-side).
import 'package:flutter/material.dart';

import '../api/models/emoji.dart';
import 'peer_image.dart';

final RegExp emojiTokenRe = RegExp(r':([a-zA-Z0-9_-]+)(?:@([0-9a-fA-F]{64}))?:');

const double _inlineEmojiSize = 18;

/// Splits [content] into text and inline-image spans using [library]
/// (emoji hash -> CustomEmoji). Returns a single text span when no token
/// resolves.
List<InlineSpan> emojiSpans(
    String content, Map<String, CustomEmoji> library, TextStyle style) {
  if (!content.contains(':') || library.isEmpty) {
    return [TextSpan(text: content, style: style)];
  }

  final spans = <InlineSpan>[];
  int last = 0;
  for (final m in emojiTokenRe.allMatches(content)) {
    final name = m.group(1)!;
    final hash = m.group(2);
    final CustomEmoji? emoji = hash != null
        ? library[hash]
        : library.values.where((e) => e.name == name).firstOrNull;
    if (emoji == null) continue;
    if (m.start > last) {
      spans.add(TextSpan(text: content.substring(last, m.start), style: style));
    }
    spans.add(WidgetSpan(
      alignment: PlaceholderAlignment.middle,
      child: Tooltip(
        message: ':${emoji.name}:',
        child: peerImage(emoji.imageBytes, size: _inlineEmojiSize),
      ),
    ));
    last = m.end;
  }
  if (spans.isEmpty) return [TextSpan(text: content, style: style)];
  if (last < content.length) {
    spans.add(TextSpan(text: content.substring(last), style: style));
  }
  return spans;
}
