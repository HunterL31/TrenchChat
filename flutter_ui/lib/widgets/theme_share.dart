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

/// A miniature of the main window painted with [spec]'s resolved palettes --
/// rail, channel column, top bar, and content each in their own section's
/// colors, so a per-section theme previews where it will actually land.
class ThemeMiniPreview extends StatelessWidget {
  const ThemeMiniPreview({super.key, required this.spec, this.height = 64});

  final ThemeSpec spec;
  final double height;

  /// A stand-in text line: a thin rounded-nothing bar of [color].
  Widget _bar(Color color, double width, [double barHeight = 3]) =>
      Container(width: width, height: barHeight, color: color);

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final rail = spec.resolve(TCSection.serverRail);
    final channels = spec.resolve(TCSection.channelList);
    final topBar = spec.resolve(TCSection.topBar);
    final content = spec.resolve(TCSection.content);

    return Container(
      height: height,
      decoration: BoxDecoration(border: Border.all(color: tc.borderStrong)),
      clipBehavior: Clip.hardEdge,
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Container(
            width: 12,
            color: rail.bgApp,
            padding: const EdgeInsets.symmetric(horizontal: 3, vertical: 4),
            child: Column(
              children: [
                Container(width: 6, height: 6, color: rail.accentPrimary),
                const SizedBox(height: 3),
                Container(width: 6, height: 6, color: rail.bgInset),
                const SizedBox(height: 3),
                Container(width: 6, height: 6, color: rail.bgInset),
              ],
            ),
          ),
          Container(
            width: 30,
            color: channels.bgSurface,
            padding: const EdgeInsets.all(4),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                _bar(channels.textEmphasis, 16),
                const SizedBox(height: 4),
                Container(
                  width: 22,
                  padding: const EdgeInsets.all(1.5),
                  color: channels.bgSelected,
                  child: _bar(channels.textEmphasis, 12),
                ),
                const SizedBox(height: 3),
                _bar(channels.textSecondary, 14),
                const SizedBox(height: 3),
                _bar(channels.textSecondary, 17),
                const SizedBox(height: 3),
                _bar(channels.statusOnline, 8),
              ],
            ),
          ),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Container(
                  height: 12,
                  color: topBar.bgSurfaceRaised,
                  padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 4),
                  child: Row(
                    children: [
                      _bar(topBar.textEmphasis, 18, 4),
                      const Spacer(),
                      _bar(topBar.accentPrimary, 10, 4),
                    ],
                  ),
                ),
                Expanded(
                  child: Container(
                    color: content.bgApp,
                    padding: const EdgeInsets.all(4),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        _bar(content.accentPrimary, 14),
                        const SizedBox(height: 3),
                        _bar(content.textPrimary, 42),
                        const SizedBox(height: 3),
                        _bar(content.textPrimary, 30),
                        const SizedBox(height: 5),
                        _bar(content.accentSecondary, 12),
                        const SizedBox(height: 3),
                        _bar(content.textPrimary, 36),
                      ],
                    ),
                  ),
                ),
                Container(
                  height: 9,
                  color: content.bgSurface,
                  padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 3),
                  child: Align(
                    alignment: Alignment.centerLeft,
                    child: _bar(content.textTertiary, 24),
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

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
    this.onApply,
  });

  final String name;
  final ThemeSpec spec;

  /// The reader's saved themes, which decide the name this one lands under.
  final Map<String, ThemeSpec> library;

  /// Saves the theme under the name the card settled on. Returns whether it
  /// landed; the card only says ADDED when it did.
  final Future<bool> Function(String name, ThemeSpec spec)? onAdd;

  /// Makes the theme the active one. Returns whether it took.
  final Future<bool> Function(ThemeSpec spec)? onApply;

  @override
  State<ThemeCodeCard> createState() => _ThemeCodeCardState();
}

class _ThemeCodeCardState extends State<ThemeCodeCard> {
  bool _busy = false;
  late bool _added = _nameAlreadyHolding != null;

  /// The name the theme was kept under, once it has been.
  late String? _savedAs = _nameAlreadyHolding;

  /// The library name already holding exactly this theme, if any -- the
  /// sender's own share starts out ADDED instead of offering ADD.
  String? get _nameAlreadyHolding {
    if (widget.library[widget.name] == widget.spec) return widget.name;
    for (final entry in widget.library.entries) {
      if (entry.value == widget.spec) return entry.key;
    }
    return null;
  }

  Future<void> _add() async {
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

  Future<void> _apply() async {
    setState(() => _busy = true);
    await widget.onApply!(widget.spec);
    if (!mounted) return;
    setState(() => _busy = false);
  }

  /// One line saying what the theme carries, so a style-only theme -- whose
  /// swatches are just the stock palette -- still reads as something.
  String get _contents {
    var colors = widget.spec.base.length;
    for (final tokens in widget.spec.sections.values) {
      colors += tokens.length;
    }
    var styles = 0;
    for (final overrides in widget.spec.styles.values) {
      styles += overrides.length;
    }
    final parts = [
      if (colors > 0) '$colors color${colors == 1 ? '' : 's'}',
      if (styles > 0) '$styles style${styles == 1 ? '' : 's'}',
    ];
    return parts.isEmpty ? 'stock theme' : parts.join(' · ');
  }

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
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
                  ThemeMiniPreview(spec: widget.spec),
                  const SizedBox(height: 4),
                  Text(
                    _contents,
                    style: TextStyle(fontSize: TCType.textMicro, color: tc.textSecondary),
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
            Column(
              crossAxisAlignment: CrossAxisAlignment.end,
              children: [
                Tooltip(
                  message: _savedAs == null || _savedAs == widget.name
                      ? 'Save to my themes'
                      : 'Saved as "$_savedAs"',
                  child: TcGhostButton(
                    label: _added ? 'ADDED' : 'ADD',
                    onPressed: widget.onAdd == null || _busy || _added ? null : _add,
                  ),
                ),
                if (widget.onApply != null) ...[
                  const SizedBox(height: 4),
                  Tooltip(
                    message: 'Use this theme now',
                    child: TcGhostButton(
                      label: 'APPLY',
                      onPressed: _busy ? null : _apply,
                    ),
                  ),
                ],
              ],
            ),
          ],
        ),
      ),
    );
  }
}
