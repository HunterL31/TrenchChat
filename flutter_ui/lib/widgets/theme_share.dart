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

/// The name a shared theme should be saved under given what [taken] already
/// holds: [name] itself when it is free, otherwise `name-2`, `name-3`, ...
/// The stem is trimmed as far as it must be to keep the result within
/// [maxThemeNameLength].
String freeThemeName(String name, Iterable<String> taken) {
  final used = taken.toSet();
  if (!used.contains(name)) return name;
  for (var n = 2;; n++) {
    final suffix = '-$n';
    final stemLimit = maxThemeNameLength - suffix.length;
    final stem = name.length > stemLimit ? name.substring(0, stemLimit) : name;
    final candidate = '$stem$suffix';
    if (!used.contains(candidate)) return candidate;
  }
}

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
///
/// ADD never silently replaces a theme the reader already has. A name that is
/// free is used as it is; a name already holding this very theme is left alone
/// and the button just reads ADDED; a name holding a different theme is saved
/// beside it as `name-2`.
class ThemeCodeCard extends StatefulWidget {
  const ThemeCodeCard({
    super.key,
    required this.name,
    required this.spec,
    this.library = const {},
    this.onAdd,
  });

  final String name;
  final ThemeSpec spec;

  /// The reader's saved themes, which decide the name this one lands under.
  final Map<String, ThemeSpec> library;

  /// Saves the theme under the name the card settled on. Returns whether it
  /// landed; the card only says ADDED when it did.
  final Future<bool> Function(String name, ThemeSpec spec)? onAdd;

  @override
  State<ThemeCodeCard> createState() => _ThemeCodeCardState();
}

class _ThemeCodeCardState extends State<ThemeCodeCard> {
  bool _busy = false;
  bool _added = false;

  /// The name the theme was kept under, once it has been.
  String? _savedAs;

  /// True when the library already holds exactly this theme under this name.
  bool get _alreadyHave => widget.library[widget.name] == widget.spec;

  Future<void> _add() async {
    if (_alreadyHave) {
      setState(() {
        _added = true;
        _savedAs = widget.name;
      });
      return;
    }
    final target = freeThemeName(widget.name, widget.library.keys);
    setState(() => _busy = true);
    final ok = await widget.onAdd!(target, widget.spec);
    if (!mounted) return;
    setState(() {
      _busy = false;
      _added = ok;
      _savedAs = ok ? target : null;
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
                  if (_savedAs != null && _savedAs != widget.name) ...[
                    const SizedBox(height: 4),
                    Text(
                      'Saved as "$_savedAs"',
                      overflow: TextOverflow.ellipsis,
                      softWrap: false,
                      style: TextStyle(fontSize: TCType.textMicro, color: tc.accentSecondary),
                    ),
                  ],
                ],
              ),
            ),
            const SizedBox(width: 8),
            Tooltip(
              message: _savedAs == null || _savedAs == widget.name
                  ? 'Save to my themes'
                  : 'Saved as "$_savedAs"',
              child: TcGhostButton(
                label: _added ? 'ADDED' : 'ADD',
                onPressed: widget.onAdd == null || _busy || _added ? null : _add,
              ),
            ),
          ],
        ),
      ),
    );
  }
}
