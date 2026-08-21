// 1a: no-chrome compose row. Enter sends, Shift+Enter inserts a newline.
// There is deliberately no Send button.
//
// Two things the draft shows short and sends long: a picked custom emoji
// (`:name:` -> `:name@hash:`) and a theme shared from the appearance editor
// (`[theme:Name]` -> its `tct1:` code). Both expand at send time, so neither
// a 64-char hash nor a whole packed theme ever sits in the user's words, and
// deleting the token is all it takes to not send it.
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

  @override
  void initState() {
    super.initState();
    HardwareKeyboard.instance.addHandler(_onKeyEvent);
    _consumeThemeShare();
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
    final next = '$text$separator${composeThemeToken(name)}';
    _controller.text = next;
    _controller.selection = TextSelection.collapsed(offset: next.length);
    _focusNode.requestFocus();
  }

  @override
  void dispose() {
    HardwareKeyboard.instance.removeHandler(_onKeyEvent);
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
    _controller.clear();
    final ok = await widget.onSend(_expandDraftThemes(_expandDraftEmoji(text)));
    if (ok) {
      _draftEmoji.clear();
      _draftThemes.clear();
    } else if (mounted && _controller.text.isEmpty) {
      // Restore the short form, and keep the mapping so a retry still expands.
      _controller.text = text;
      _controller.selection = TextSelection.collapsed(offset: text.length);
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
    _controller.text = text.replaceRange(start, end, token);
    _controller.selection = TextSelection.collapsed(offset: start + token.length);
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
