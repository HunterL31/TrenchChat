// Inline custom-emoji rendering -- port of channel_view._render_content's
// token rewriting. `:name@hexhash:` resolves by 64-char SHA-256 hash;
// legacy `:name:` falls back to an exact name match. Unknown tokens stay as
// literal text (the request-from-sender path is backend-side).
//
// Plain-text runs are also linkified when an [InlineLinkConfig] is supplied,
// so http/https URLs render styled and tappable.
import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';

import '../api/models/emoji.dart';
import 'peer_image.dart';
import 'tc_tooltip.dart';

final RegExp emojiTokenRe = RegExp(r':([a-zA-Z0-9_-]+)(?:@([0-9a-fA-F]{64}))?:');

/// Bare http/https URLs in message text.
final RegExp urlRe = RegExp(r'https?://[^\s<>]+', caseSensitive: false);

/// Nomad Network page/file URLs (node hash + request path), so pages shared
/// in chat open in the NET tab.
final RegExp nomadUrlRe =
    RegExp(r'\b[0-9a-fA-F]{32}:/(?:page|file)/[^\s<>]+');

/// Trailing characters stripped off a detected URL, so a sentence-final period
/// or a wrapping bracket is not swept into the link.
const String _urlTrailingTrim = '.,;:!?)]}\'"';

const double _inlineEmojiSize = 18;

/// Styling and behavior for inline links. The caller owns [recognizers] and
/// must dispose them; every tappable link span appends its recognizer here.
class InlineLinkConfig {
  InlineLinkConfig({
    required this.style,
    required this.hoverStyle,
    required this.recognizers,
    this.onTap,
    this.hoveredUrl,
    this.onHover,
  });

  /// The link's resting style (uses linkColor).
  final TextStyle style;

  /// The link's style while pointed at (uses linkHoverColor).
  final TextStyle hoverStyle;

  /// Recognizer sink the caller disposes; one is added per tappable link.
  final List<TapGestureRecognizer> recognizers;

  /// Opens a tapped URL. Null renders links styled but inert.
  final void Function(String url)? onTap;

  /// The URL currently under the pointer, painted with [hoverStyle].
  final String? hoveredUrl;

  /// Reports the URL under the pointer (null on exit), so the caller can
  /// repaint the hovered link.
  final void Function(String? url)? onHover;
}

/// Splits a plain-text run into text and styled link spans. Returns a single
/// text span when there is nothing to link.
List<InlineSpan> _linkifyRun(String text, TextStyle style, InlineLinkConfig? links) {
  // ':/'' covers both web ('://') and nomad ('<hash>:/page/') URL shapes.
  if (links == null || !text.contains(':/')) {
    return [TextSpan(text: text, style: style)];
  }
  final matches = [...urlRe.allMatches(text), ...nomadUrlRe.allMatches(text)]
    ..sort((a, b) => a.start.compareTo(b.start));
  final spans = <InlineSpan>[];
  int last = 0;
  for (final m in matches) {
    if (m.start < last) continue;
    var url = m.group(0)!;
    var end = m.end;
    while (url.isNotEmpty && _urlTrailingTrim.contains(url[url.length - 1])) {
      url = url.substring(0, url.length - 1);
      end--;
    }
    if (url.isEmpty) continue;
    if (m.start > last) {
      spans.add(TextSpan(text: text.substring(last, m.start), style: style));
    }
    TapGestureRecognizer? recognizer;
    if (links.onTap != null) {
      recognizer = TapGestureRecognizer()..onTap = () => links.onTap!(url);
      links.recognizers.add(recognizer);
    }
    spans.add(TextSpan(
      text: url,
      style: links.hoveredUrl == url ? links.hoverStyle : links.style,
      recognizer: recognizer,
      mouseCursor: SystemMouseCursors.click,
      onEnter: links.onHover == null ? null : (_) => links.onHover!(url),
      onExit: links.onHover == null ? null : (_) => links.onHover!(null),
    ));
    last = end;
  }
  if (spans.isEmpty) return [TextSpan(text: text, style: style)];
  if (last < text.length) {
    spans.add(TextSpan(text: text.substring(last), style: style));
  }
  return spans;
}

/// Splits [content] into text and inline-image spans using [library]
/// (emoji hash -> CustomEmoji), linkifying plain text when [links] is given.
/// Returns a single text span when no token resolves.
List<InlineSpan> emojiSpans(
    String content, Map<String, CustomEmoji> library, TextStyle style,
    {InlineLinkConfig? links}) {
  if (!content.contains(':') || library.isEmpty) {
    return _linkifyRun(content, style, links);
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
      spans.addAll(_linkifyRun(content.substring(last, m.start), style, links));
    }
    spans.add(WidgetSpan(
      alignment: PlaceholderAlignment.middle,
      // A span has no context of its own; the Builder borrows the one the
      // surrounding text renders under, so the tip can read the section.
      child: Builder(
        builder: (context) => Tooltip(
          decoration: tcTooltipDecoration(context),
          textStyle: tcTooltipTextStyle(context),
          message: ':${emoji.name}:',
          child: peerImage(emoji.imageBytes, size: _inlineEmojiSize),
        ),
      ),
    ));
    last = m.end;
  }
  if (spans.isEmpty) return _linkifyRun(content, style, links);
  if (last < content.length) {
    spans.addAll(_linkifyRun(content.substring(last), style, links));
  }
  return spans;
}
