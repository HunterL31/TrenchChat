// 1a: no-chrome compose row. Enter sends, Shift+Enter inserts a newline.
// There is deliberately no Send button.
//
// Two things the draft shows short and sends long: a picked custom emoji
// (`:name:` -> `:name@hash:`) and a theme shared from the appearance editor
// (`[theme:Name]` -> its `tct1:` code). Both expand at send time, so neither
// a 64-char hash nor a whole packed theme ever sits in the user's words, and
// deleting the token is all it takes to not send it. A theme token is
// all-or-nothing: editing any part of one takes the whole token out, so a
// half-deleted one is never sent as literal text.
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../../theme/section_theme.dart';
import '../../theme/tokens.dart';
import '../../widgets/emoji_text.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_icon.dart';

/// The token a staged theme share reads as in the draft.
String composeThemeToken(String name) => '[theme:$name]';

/// Matches a staged theme token in draft text.
final RegExp composeThemeTokenRe = RegExp(r'\[theme:([^\]\n]+)\]');

/// The opener a staged token always starts with, and the least of it that
/// has to survive for what is left to still read as one.
const String _themeTokenOpener = '[theme:';

/// True when [candidate] is [token] with characters taken out of it -- what a
/// half-deleted token looks like. An unrelated `[theme:...]` the user typed
/// is not one, since its own letters are not in the token.
bool _isThemeTokenRemnant(String candidate, String token) {
  if (candidate.length >= token.length ||
      candidate.length < _themeTokenOpener.length) {
    return false;
  }
  var i = 0;
  for (var j = 0; j < token.length && i < candidate.length; j++) {
    if (candidate.codeUnitAt(i) == token.codeUnitAt(j)) i++;
  }
  return i == candidate.length;
}

/// How much of [token] [text] still reads out from [start].
int _themeTokenPrefixLength(String text, int start, String token) {
  var n = 0;
  while (start + n < text.length &&
      n < token.length &&
      text.codeUnitAt(start + n) == token.codeUnitAt(n)) {
    n++;
  }
  return n;
}

/// The span of [text] holding what is left of [token] after part of it was
/// deleted, or null when nothing there reads as a remnant.
///
/// Two shapes count: a bracketed group that is the token with characters
/// taken out of it (a delete inside the name), and a leading run of the token
/// that stops short (a delete off its end). Both have to be more than the
/// opener, or an unrelated `[theme:something]` the user typed would qualify.
({int start, int end})? themeTokenRemnant(String text, String token) {
  const core = 'theme:';
  for (var at = text.indexOf(core); at >= 0; at = text.indexOf(core, at + 1)) {
    final start = at > 0 && text[at - 1] == '[' ? at - 1 : at;
    final newline = text.indexOf('\n', at);
    var end = text.indexOf(']', at);
    if (end < 0 || (newline >= 0 && newline < end)) {
      end = newline >= 0 ? newline : text.length;
    } else {
      end += 1;
    }
    if (_isThemeTokenRemnant(text.substring(start, end), token)) {
      return (start: start, end: end);
    }
    final prefix = _themeTokenPrefixLength(text, start, token);
    if (prefix > _themeTokenOpener.length) {
      return (start: start, end: start + prefix);
    }
  }
  return null;
}

class ComposeBar extends StatefulWidget {
  const ComposeBar({
    super.key,
    required this.channelName,
    required this.enabled,
    required this.onSend,
    this.pickEmoji,
    this.pendingThemeShare,
    this.onThemeShareConsumed,
    this.compact = false,
  });

  final String channelName;
  final bool enabled;

  /// Returns whether the message was accepted; on false the composed text is
  /// restored so a failed send never eats the user's words.
  final Future<bool> Function(String content) onSend;

  /// Narrow/touch mode: swaps the keyboard hint for a send button, since
  /// mobile keyboards have no Enter-to-send.
  final bool compact;

  /// Opens the emoji picker. A unicode char is inserted as-is; a custom
  /// emoji's `:name@hash:` is shown as just `:name:` and re-expanded on send,
  /// so a 64-char hash never sits in the user's draft.
  final Future<String?> Function()? pickEmoji;

  /// A theme the appearance editor staged: dropped into the draft as
  /// `[theme:<name>]` and sent as [code]. Cleared through
  /// [onThemeShareConsumed] once it is in the draft.
  final ({String name, String code})? pendingThemeShare;

  final VoidCallback? onThemeShareConsumed;

  @override
  State<ComposeBar> createState() => _ComposeBarState();
}

class _ComposeBarState extends State<ComposeBar> {
  final TextEditingController _controller = TextEditingController();
  final FocusNode _focusNode = FocusNode();

  /// Emoji name -> hash for customs picked into the current draft, so the
  /// short `:name:` the user sees goes out as an unambiguous `:name@hash:`.
  final Map<String, String> _draftEmoji = {};

  /// Theme name -> code for shares staged into the current draft, so the
  /// short `[theme:name]` the user sees goes out as the full code.
  final Map<String, String> _draftThemes = {};

  /// Whether the share currently offered has already been taken. Cleared
  /// when the offer goes away, so a second share still lands.
  bool _shareConsumed = false;

  /// True while this widget is the one writing the draft, so the edit does
  /// not re-enter the listener that is making it.
  bool _rewritingDraft = false;

  @override
  void initState() {
    super.initState();
    HardwareKeyboard.instance.addHandler(_onKeyEvent);
    _controller.addListener(_onDraftChanged);
    _consumeThemeShare();
  }

  /// Keeps a staged theme token all-or-nothing: editing any part of one takes
  /// the whole token out of the draft, the way deleting an attachment chip
  /// does, rather than leaving a half-token to be sent as literal text.
  void _onDraftChanged() {
    if (_rewritingDraft || _draftThemes.isEmpty) return;
    var text = _controller.text;
    var cursor = _controller.selection.baseOffset;
    var changed = false;
    for (final name in _draftThemes.keys.toList()) {
      final token = composeThemeToken(name);
      if (text.contains(token)) continue;
      _draftThemes.remove(name);
      final remnant = themeTokenRemnant(text, token);
      if (remnant == null) continue;
      text = text.replaceRange(remnant.start, remnant.end, '');
      cursor = remnant.start;
      changed = true;
    }
    if (changed) _setDraftText(text, cursor: cursor);
  }

  /// Writes the draft without [_onDraftChanged] acting on it -- every caller
  /// here already knows what the token mappings should be.
  void _setDraftText(String text, {int? cursor}) {
    final offset = (cursor ?? text.length).clamp(0, text.length);
    _rewritingDraft = true;
    _controller.value = TextEditingValue(
      text: text,
      selection: TextSelection.collapsed(offset: offset),
    );
    _rewritingDraft = false;
  }

  @override
  void didUpdateWidget(ComposeBar oldWidget) {
    super.didUpdateWidget(oldWidget);
    _consumeThemeShare();
  }

  /// Takes the offered share once, after this frame -- the offer is read
  /// while the compose bar is being built, so it cannot be cleared inline.
  void _consumeThemeShare() {
    if (widget.pendingThemeShare == null) {
      _shareConsumed = false;
      return;
    }
    if (_shareConsumed) return;
    _shareConsumed = true;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      final staged = widget.pendingThemeShare;
      if (!mounted || staged == null) return;
      _insertThemeToken(staged.name, staged.code);
      widget.onThemeShareConsumed?.call();
    });
  }

  /// Appends the theme's token to the draft, a space clear of whatever is
  /// already typed there.
  void _insertThemeToken(String name, String code) {
    _draftThemes[name] = code;
    final text = _controller.text;
    final separator = text.isEmpty || text.endsWith(' ') || text.endsWith('\n') ? '' : ' ';
    _setDraftText('$text$separator${composeThemeToken(name)}');
    _focusNode.requestFocus();
  }

  @override
  void dispose() {
    HardwareKeyboard.instance.removeHandler(_onKeyEvent);
    _controller.removeListener(_onDraftChanged);
    _controller.dispose();
    _focusNode.dispose();
    super.dispose();
  }

  bool _onKeyEvent(KeyEvent event) {
    if (!_focusNode.hasFocus) return false;
    if (event is KeyDownEvent && event.logicalKey == LogicalKeyboardKey.enter) {
      if (HardwareKeyboard.instance.isShiftPressed) {
        return false; // let the TextField insert the newline
      }
      _submit();
      return true; // swallow -- no newline, no default handling
    }
    return false;
  }

  Future<void> _submit() async {
    final text = _controller.text;
    if (text.trim().isEmpty || !widget.enabled) return;
    final expanded = _expandDraftThemes(_expandDraftEmoji(text));
    _setDraftText('');
    final ok = await widget.onSend(expanded);
    if (ok) {
      _draftEmoji.clear();
      _draftThemes.clear();
    } else if (mounted && _controller.text.isEmpty) {
      // Restore the short form, and keep the mapping so a retry still expands.
      _setDraftText(text);
    }
  }

  /// Rewrites each `:name:` picked this draft back to `:name@hash:`. Tokens
  /// that already carry a hash, and names the user typed themselves, are left
  /// exactly as they are.
  String _expandDraftEmoji(String text) {
    if (_draftEmoji.isEmpty) return text;
    return text.replaceAllMapped(emojiTokenRe, (m) {
      if (m.group(2) != null) return m[0]!;
      final hash = _draftEmoji[m.group(1)!];
      return hash == null ? m[0]! : ':${m.group(1)}@$hash:';
    });
  }

  /// Rewrites each `[theme:name]` staged this draft to its code. A token the
  /// user deleted simply is not there to expand.
  String _expandDraftThemes(String text) {
    if (_draftThemes.isEmpty) return text;
    return text.replaceAllMapped(
      composeThemeTokenRe,
      (m) => _draftThemes[m.group(1)!] ?? m[0]!,
    );
  }

  Future<void> _insertEmoji() async {
    final picked = await widget.pickEmoji?.call();
    if (picked == null || !mounted) return;
    final token = _shorten(picked);
    final text = _controller.text;
    final selection = _controller.selection;
    final start = selection.isValid ? selection.start : text.length;
    final end = selection.isValid ? selection.end : text.length;
    _setDraftText(text.replaceRange(start, end, token), cursor: start + token.length);
    _focusNode.requestFocus();
  }

  /// `:name@hash:` -> `:name:`, remembering the hash. Anything else (a unicode
  /// emoji, an unrecognised string) is inserted unchanged.
  String _shorten(String picked) {
    final m = emojiTokenRe.matchAsPrefix(picked);
    if (m == null || m.end != picked.length || m.group(2) == null) return picked;
    _draftEmoji[m.group(1)!] = m.group(2)!;
    return ':${m.group(1)}:';
  }

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    return Container(
      decoration: BoxDecoration(
        color: tc.bgSurface,
        border: Border(top: BorderSide(color: tc.borderSubtle)),
      ),
      padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 14),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.center,
        children: [
          TcIcon(TcIcons.plus, size: 15, color: tc.textTertiary),
          const SizedBox(width: 10),
          Expanded(
            child: TextField(
              controller: _controller,
              focusNode: _focusNode,
              enabled: widget.enabled,
              minLines: 1,
              maxLines: 6,
              style: TextStyle(fontSize: TCType.textBodyMd, color: tc.textPrimary),
              decoration: InputDecoration(
                isDense: true,
                border: InputBorder.none,
                hintText: 'Message #${widget.channelName}…',
                hintStyle: TextStyle(fontSize: TCType.textBodyMd, color: tc.textTertiary),
              ),
            ),
          ),
          const SizedBox(width: 10),
          MouseRegion(
            cursor: widget.pickEmoji == null
                ? SystemMouseCursors.basic
                : SystemMouseCursors.click,
            child: GestureDetector(
              onTap: widget.pickEmoji == null ? null : _insertEmoji,
              child: TcIcon(TcIcons.emoji, size: 15, color: tc.textTertiary),
            ),
          ),
          const SizedBox(width: 10),
          if (widget.compact)
            TcIconButton(
              icon: TcIcons.send,
              tooltip: 'Send',
              size: 30,
              onPressed: widget.enabled ? _submit : null,
            )
          else
            Text(
              'ENTER TO SEND · SHIFT+ENTER NEWLINE',
              style: TextStyle(
                fontSize: TCType.textMicro,
                color: tc.textTertiary,
                letterSpacing: TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWide),
              ),
            ),
        ],
      ),
    );
  }
}
