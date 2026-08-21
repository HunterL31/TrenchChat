// Themes shared in chat: the inline chip a theme code renders as, and the
// card under the message that previews it and saves it to the library.
//
// Same contract as the custom-emoji tokens next door -- a token this client
// can read is rewritten, one it cannot stays literal text, so a code from a
// newer client is still readable as words.
import 'package:flutter/material.dart';

import '../api/models/emoji.dart';
import '../theme/section_theme.dart';
import '../theme/theme_code.dart';
import '../theme/theme_spec.dart';
import '../theme/tokens.dart';
import 'emoji_text.dart';
import 'tc_button.dart';

/// The base tokens a card previews, in the order the swatch strip shows them.
const List<String> themePreviewTokens = [
  'bgApp',
  'bgSurface',
  'accentPrimary',
  'accentSecondary',
  'textPrimary',
  'textEmphasis',
];

/// One decodable theme code found in a message.
typedef SharedTheme = ({String code, String name, ThemeSpec spec});

/// The decodable theme codes in [content], in the order they appear, one
/// entry per distinct code.
List<SharedTheme> themeCodesIn(String content) {
  if (!content.contains(themeCodePrefix)) return const [];
  final seen = <String>{};
  final out = <SharedTheme>[];
  for (final match in themeCodeRe.allMatches(content)) {
    final code = match.group(0)!;
    if (!seen.add(code)) continue;
    final decoded = decodeThemeCode(code);
    if (decoded == null) continue;
    out.add((code: code, name: decoded.name, spec: decoded.spec));
  }
  return out;
}

/// Splits [content] into spans, each readable theme code becoming a chip and
/// every other run going through [emojiSpans].
List<InlineSpan> messageContentSpans(
    String content, Map<String, CustomEmoji> emojiLibrary, TextStyle style) {
  if (!content.contains(themeCodePrefix)) {
    return emojiSpans(content, emojiLibrary, style);
  }

  final spans = <InlineSpan>[];
  int last = 0;
  for (final match in themeCodeRe.allMatches(content)) {
    final decoded = decodeThemeCode(match.group(0)!);
    if (decoded == null) continue;
    if (match.start > last) {
      spans.addAll(emojiSpans(content.substring(last, match.start), emojiLibrary, style));
    }
    spans.add(WidgetSpan(
      alignment: PlaceholderAlignment.middle,
      child: _ThemeCodeChip(name: decoded.name),
    ));
    last = match.end;
  }
  if (spans.isEmpty) return emojiSpans(content, emojiLibrary, style);
  if (last < content.length) {
    spans.addAll(emojiSpans(content.substring(last), emojiLibrary, style));
  }
  return spans;
}

class _ThemeCodeChip extends StatelessWidget {
  const _ThemeCodeChip({required this.name});
  final String name;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    return Container(
      margin: const EdgeInsets.symmetric(horizontal: 1),
      padding: const EdgeInsets.symmetric(horizontal: 5, vertical: 1),
      decoration: BoxDecoration(
        color: tc.bgInset,
        border: Border.all(color: tc.borderAccent),
      ),
      child: Text(
        '[THEME: $name]',
        style: TextStyle(
          fontSize: TCType.textMicro,
          color: tc.accentPrimary,
          letterSpacing: TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWide),
        ),
      ),
    );
  }
}

/// The card under a message carrying a theme code: what the theme looks like,
/// and the one button that keeps it.
class ThemeCodeCard extends StatefulWidget {
  const ThemeCodeCard({super.key, required this.name, required this.spec, this.onAdd});

  final String name;
  final ThemeSpec spec;

  /// Saves the theme under its own name, replacing one saved there already.
  /// Returns whether it landed; the card only says ADDED when it did.
  final Future<bool> Function(String name, ThemeSpec spec)? onAdd;

  @override
  State<ThemeCodeCard> createState() => _ThemeCodeCardState();
}

class _ThemeCodeCardState extends State<ThemeCodeCard> {
  bool _busy = false;
  bool _added = false;

  Future<void> _add() async {
    setState(() => _busy = true);
    final ok = await widget.onAdd!(widget.name, widget.spec);
    if (!mounted) return;
    setState(() {
      _busy = false;
      _added = ok;
    });
  }

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final preview = widget.spec.resolveBase().asMap();
    return Padding(
      padding: const EdgeInsets.only(top: 6),
      child: Container(
        constraints: const BoxConstraints(maxWidth: 320),
        decoration: BoxDecoration(
          color: tc.bgSurfaceRaised,
          border: Border.all(color: tc.borderDefault),
        ),
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
        child: Row(
          children: [
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    'SHARED THEME',
                    style: TextStyle(
                      fontSize: TCType.textMicro,
                      color: tc.textTertiary,
                      letterSpacing:
                          TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWider),
                    ),
                  ),
                  const SizedBox(height: 2),
                  Text(
                    widget.name,
                    overflow: TextOverflow.ellipsis,
                    softWrap: false,
                    style: TextStyle(fontSize: TCType.textBodySm, color: tc.textPrimary),
                  ),
                  const SizedBox(height: 6),
                  Row(
                    children: [
                      for (final token in themePreviewTokens) ...[
                        Container(
                          width: 16,
                          height: 16,
                          decoration: BoxDecoration(
                            color: preview[token],
                            border: Border.all(color: tc.borderStrong),
                          ),
                        ),
                        const SizedBox(width: 4),
                      ],
                    ],
                  ),
                ],
              ),
            ),
            const SizedBox(width: 8),
            TcGhostButton(
              label: _added ? 'ADDED' : 'ADD',
              onPressed: widget.onAdd == null || _busy || _added ? null : _add,
            ),
          ],
        ),
      ),
    );
  }
}
