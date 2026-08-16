// 1a: no-chrome compose row. Enter sends, Shift+Enter inserts a newline.
// There is deliberately no Send button.
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../../theme/tokens.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_icon.dart';

class ComposeBar extends StatefulWidget {
  const ComposeBar({
    super.key,
    required this.channelName,
    required this.enabled,
    required this.onSend,
    this.pickEmoji,
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

  /// Opens the emoji picker; the returned compose token (a unicode char or
  /// `:name@hash:`) is inserted at the cursor.
  final Future<String?> Function()? pickEmoji;

  @override
  State<ComposeBar> createState() => _ComposeBarState();
}

class _ComposeBarState extends State<ComposeBar> {
  final TextEditingController _controller = TextEditingController();
  final FocusNode _focusNode = FocusNode();

  @override
  void initState() {
    super.initState();
    HardwareKeyboard.instance.addHandler(_onKeyEvent);
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
    final ok = await widget.onSend(text);
    if (!ok && mounted && _controller.text.isEmpty) {
      _controller.text = text;
      _controller.selection = TextSelection.collapsed(offset: text.length);
    }
  }

  Future<void> _insertEmoji() async {
    final token = await widget.pickEmoji?.call();
    if (token == null || !mounted) return;
    final text = _controller.text;
    final selection = _controller.selection;
    final start = selection.isValid ? selection.start : text.length;
    final end = selection.isValid ? selection.end : text.length;
    _controller.text = text.replaceRange(start, end, token);
    _controller.selection = TextSelection.collapsed(offset: start + token.length);
    _focusNode.requestFocus();
  }

  @override
  Widget build(BuildContext context) {
    return Container(
      decoration: BoxDecoration(
        color: TCColors.bgSurface,
        border: Border(top: BorderSide(color: TCColors.borderSubtle)),
      ),
      padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 14),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.center,
        children: [
          TcIcon(TcIcons.plus, size: 15, color: TCColors.textTertiary),
          const SizedBox(width: 10),
          Expanded(
            child: TextField(
              controller: _controller,
              focusNode: _focusNode,
              enabled: widget.enabled,
              minLines: 1,
              maxLines: 6,
              style: TextStyle(fontSize: TCType.textBodyMd, color: TCColors.textPrimary),
              decoration: InputDecoration(
                isDense: true,
                border: InputBorder.none,
                hintText: 'Message #${widget.channelName}…',
                hintStyle: TextStyle(fontSize: TCType.textBodyMd, color: TCColors.textTertiary),
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
              child: TcIcon(TcIcons.emoji, size: 15, color: TCColors.textTertiary),
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
                color: TCColors.textTertiary,
                letterSpacing: TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWide),
              ),
            ),
        ],
      ),
    );
  }
}
