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

import '../../attachments.dart';
import '../../theme/section_theme.dart';
import '../../theme/shape.dart';
import '../../theme/theme_spec.dart';
import '../../theme/tokens.dart';
import '../../widgets/emoji_text.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_icon.dart';
import '../../widgets/tc_tooltip.dart';

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

/// One channel's unsent draft: the text plus the token maps that expand it.
class _ComposeDraft {
  const _ComposeDraft(this.text, this.emoji, this.themes, this.attachment);

  final String text;
  final Map<String, String> emoji;
  final Map<String, String> themes;
  final PickedAttachment? attachment;

  bool get isEmpty =>
      text.isEmpty && emoji.isEmpty && themes.isEmpty && attachment == null;
}

class ComposeBar extends StatefulWidget {
  const ComposeBar({
    super.key,
    required this.channelName,
    required this.enabled,
    required this.onSend,
    this.channelHash,
    this.pickEmoji,
    this.pickAttachment,
    this.watchPastedImages,
    this.pendingThemeShare,
    this.onThemeShareConsumed,
    this.replyPreview,
    this.onCancelReply,
    this.compact = false,
  });

  final String channelName;

  /// Which channel the draft belongs to. Switching it stashes the draft under
  /// the old hash and restores whatever was left in the new one, so words
  /// typed in one channel never follow the reader into another. Null keeps a
  /// single draft; the shell always passes one.
  final String? channelHash;

  final bool enabled;

  /// Returns whether the message was accepted; on false the composed text and
  /// any staged attachment are restored, so a failed send never eats them.
  final Future<bool> Function(String content, PickedAttachment? attachment) onSend;

  /// Narrow/touch mode: swaps the keyboard hint for a send button, since
  /// mobile keyboards have no Enter-to-send.
  final bool compact;

  /// Opens the emoji picker. A unicode char is inserted as-is; a custom
  /// emoji's `:name@hash:` is shown as just `:name:` and re-expanded on send,
  /// so a 64-char hash never sits in the user's draft.
  final Future<String?> Function()? pickEmoji;

  /// Opens the file picker behind the + button. Null leaves the button inert,
  /// which is what isolated widget tests want.
  final Future<PickedAttachment?> Function()? pickAttachment;

  /// Subscribes a handler to the platform's paste events for as long as this
  /// compose bar lives, returning the disposer. Null disables paste-to-attach.
  final VoidCallback Function(void Function(PickedAttachment image) onImage)?
      watchPastedImages;

  /// A theme the appearance editor staged: dropped into the draft as
  /// `[theme:<name>]` and sent as [code]. Cleared through
  /// [onThemeShareConsumed] once it is in the draft.
  final ({String name, String code})? pendingThemeShare;

  final VoidCallback? onThemeShareConsumed;

  /// When set, the "replying to X" banner shows above the input; sending
  /// carries the reply through [onSend]'s caller. Null hides the banner.
  final ({String author, String snippet})? replyPreview;

  /// Clears the pending reply from the banner's cancel affordance.
  final VoidCallback? onCancelReply;

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

  /// Drafts left behind in other channels, keyed by channel hash.
  final Map<String, _ComposeDraft> _stashedDrafts = {};

  /// The image staged for the next send, shown as a chip above the input.
  PickedAttachment? _attachment;

  /// Whether the share currently offered has already been taken. Cleared
  /// when the offer goes away, so a second share still lands.
  bool _shareConsumed = false;

  /// True while this widget is the one writing the draft, so the edit does
  /// not re-enter the listener that is making it.
  bool _rewritingDraft = false;

  /// Stops the paste subscription when this compose bar goes away.
  VoidCallback? _stopWatchingPastes;

  @override
  void initState() {
    super.initState();
    HardwareKeyboard.instance.addHandler(_onKeyEvent);
    _controller.addListener(_onDraftChanged);
    _stopWatchingPastes = widget.watchPastedImages?.call(_onPastedImage);
    _consumeThemeShare();
  }

  /// A paste fires wherever the app has focus, so the compose bar takes one
  /// only when the message field is what the user is typing into.
  void _onPastedImage(PickedAttachment image) {
    if (!mounted || !_focusNode.hasFocus || !widget.enabled) return;
    setState(() => _attachment = image);
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
    // Before the share is taken, so a theme staged while the channel changes
    // still lands in the channel the reader is looking at.
    if (oldWidget.channelHash != widget.channelHash) {
      _switchDraft(oldWidget.channelHash);
    }
    _consumeThemeShare();
  }

  /// Stashes the draft under the channel being left and restores the one left
  /// in the channel being entered.
  void _switchDraft(String? leaving) {
    if (leaving != null) {
      final draft = _ComposeDraft(
          _controller.text, Map.of(_draftEmoji), Map.of(_draftThemes), _attachment);
      if (draft.isEmpty) {
        _stashedDrafts.remove(leaving);
      } else {
        _stashedDrafts[leaving] = draft;
      }
    }
    final entering = widget.channelHash;
    final restored = entering == null ? null : _stashedDrafts.remove(entering);
    _draftEmoji
      ..clear()
      ..addAll(restored?.emoji ?? const {});
    _draftThemes
      ..clear()
      ..addAll(restored?.themes ?? const {});
    _attachment = restored?.attachment;
    _setDraftText(restored?.text ?? '');
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
    _stopWatchingPastes?.call();
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
    final attachment = _attachment;
    if (!widget.enabled) return;
    if (text.trim().isEmpty && attachment == null) return;
    final expanded = _expandDraftThemes(_expandDraftEmoji(text));
    _setDraftText('');
    if (attachment != null) setState(() => _attachment = null);
    final ok = await widget.onSend(expanded, attachment);
    if (ok) {
      _draftEmoji.clear();
      _draftThemes.clear();
    } else if (mounted) {
      // Restore the short form, and keep the mapping so a retry still expands.
      if (_controller.text.isEmpty) _setDraftText(text);
      // Independently of the text, since a new draft typed during the send
      // does not mean the user meant to drop the image with it.
      if (attachment != null && _attachment == null) {
        setState(() => _attachment = attachment);
      }
    }
  }

  Future<void> _pickAttachment() async {
    final picked = await widget.pickAttachment?.call();
    if (picked == null || !mounted) return;
    setState(() => _attachment = picked);
    _focusNode.requestFocus();
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
    // A rounded theme puts the composer in a filled field of its own, the
    // way every rounded chat client does; a square one keeps the bare row.
    final rounded = tcIsRounded(context);
    final row = Row(
      crossAxisAlignment: CrossAxisAlignment.center,
      children: [
        MouseRegion(
          cursor: widget.pickAttachment == null
              ? SystemMouseCursors.basic
              : SystemMouseCursors.click,
          child: GestureDetector(
            onTap: widget.pickAttachment == null ? null : _pickAttachment,
            child: TcTooltip(
              message: 'Attach an image',
              child: TcIcon(TcIcons.plus, size: 15, color: tc.textTertiary),
            ),
          ),
        ),
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
          cursor:
              widget.pickEmoji == null ? SystemMouseCursors.basic : SystemMouseCursors.click,
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
    );
    return Container(
      decoration: BoxDecoration(
        color: tc.bgSurface,
        border: Border(top: BorderSide(color: tc.borderSubtle)),
      ),
      padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 14),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          if (widget.replyPreview != null) _replyBanner(tc),
          if (_attachment != null) _attachmentChip(tc, _attachment!),
          if (rounded)
            Container(
              decoration: BoxDecoration(color: tc.bgInset, borderRadius: tcCorners(context)),
              padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 9),
              child: row,
            )
          else
            row,
        ],
      ),
    );
  }

  /// The staged image, shown the way the reply banner is: above the input,
  /// with its own way out. Removing it is the only thing that unstages it.
  Widget _attachmentChip(TCSectionColors tc, PickedAttachment attachment) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: Row(
        children: [
          ClipRRect(
            borderRadius: tcCorners(context) ?? BorderRadius.zero,
            child: Image.memory(
              attachment.bytes,
              width: 40,
              height: 40,
              fit: BoxFit.cover,
              cacheWidth: 120,
              cacheHeight: 120,
              errorBuilder: (context, error, stack) =>
                  Container(width: 40, height: 40, color: tc.bgInset),
            ),
          ),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              attachment.name,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(fontSize: TCType.textMicro, color: tc.textSecondary),
            ),
          ),
          const SizedBox(width: 8),
          MouseRegion(
            cursor: SystemMouseCursors.click,
            child: GestureDetector(
              onTap: () => setState(() => _attachment = null),
              child: Text(
                'REMOVE',
                style: TextStyle(
                  fontSize: TCType.textMicro,
                  color: tc.textTertiary,
                  letterSpacing:
                      TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWide),
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }

  Widget _replyBanner(TCSectionColors tc) {
    final preview = widget.replyPreview!;
    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: Row(
        children: [
          Container(width: 2, height: 26, color: tc.borderAccent),
          const SizedBox(width: 8),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  'Replying to ${preview.author}',
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                    fontSize: TCType.textMicro,
                    fontWeight: FontWeight.w600,
                    color: tc.accentPrimary,
                  ),
                ),
                Text(
                  preview.snippet,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(fontSize: TCType.textMicro, color: tc.textTertiary),
                ),
              ],
            ),
          ),
          const SizedBox(width: 8),
          MouseRegion(
            cursor: SystemMouseCursors.click,
            child: GestureDetector(
              onTap: widget.onCancelReply,
              child: Text(
                'CANCEL',
                style: TextStyle(
                  fontSize: TCType.textMicro,
                  color: tc.textTertiary,
                  letterSpacing:
                      TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWide),
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}
