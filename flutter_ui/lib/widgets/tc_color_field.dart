// One editable color token: swatch, name, hex field, clear control. Built
// for the appearance editor, where thirty of these stack into a scrolling
// list, so the row is deliberately compact.
//
// Editing is total: a valid `#rrggbb` / `#aarrggbb` commits as it is typed,
// anything else is ignored, and the field snaps back to the live color when
// it loses focus. Nothing the user types can produce an invalid color. The
// swatch opens the visual picker on the same value, which commits the same
// way -- the two are alternatives, not a replacement.
import 'package:flutter/material.dart';

import '../theme/section_theme.dart';
import '../theme/shape.dart';
import '../theme/theme_spec.dart';
import '../theme/tokens.dart';
import 'tc_button.dart';
import 'tc_color_picker.dart';
import 'tc_icon.dart';
import 'tc_tooltip.dart';

/// The key of the text input inside a [TcColorField] labelled [label].
Key tcColorInputKey(String label) => Key('tc-color-input:$label');

/// The key of the swatch button that opens the picker for [label].
Key tcColorSwatchKey(String label) => Key('tc-color-swatch:$label');

class TcColorField extends StatefulWidget {
  const TcColorField({
    super.key,
    required this.label,
    required this.color,
    required this.onChanged,
    this.displayLabel,
    this.overridden = false,
    this.onClear,
  });

  /// The token key this row edits. It names the row's input key and is what
  /// the row's tooltip reveals; [displayLabel] is what the row reads as.
  final String label;

  /// The human name shown instead of [label]. Null shows the key itself.
  final String? displayLabel;

  /// The color in force right now -- the override if there is one, otherwise
  /// whatever the row inherits.
  final Color color;

  final ValueChanged<Color> onChanged;

  /// True when this scope sets the token itself rather than inheriting it.
  final bool overridden;

  /// Drops this scope's override. The clear control shows only when this is
  /// non-null and [overridden] is true.
  final VoidCallback? onClear;

  @override
  State<TcColorField> createState() => _TcColorFieldState();
}

class _TcColorFieldState extends State<TcColorField> {
  late final TextEditingController _controller =
      TextEditingController(text: encodeThemeColor(widget.color));
  final FocusNode _focus = FocusNode();

  @override
  void initState() {
    super.initState();
    _focus.addListener(() {
      if (!_focus.hasFocus) _syncText();
    });
  }

  @override
  void didUpdateWidget(TcColorField oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (widget.color != oldWidget.color) _syncText();
  }

  @override
  void dispose() {
    _controller.dispose();
    _focus.dispose();
    super.dispose();
  }

  /// Rewrites the field to the live color unless what is typed already means
  /// exactly that color -- retyping `00ff88` must not fight the caret.
  void _syncText() {
    if (parseThemeColor(_controller.text) == widget.color) return;
    final text = encodeThemeColor(widget.color);
    _controller.value = TextEditingValue(
      text: text,
      selection: TextSelection.collapsed(offset: text.length),
    );
  }

  void _onTextChanged(String raw) {
    final parsed = parseThemeColor(raw);
    if (parsed != null && parsed != widget.color) widget.onChanged(parsed);
  }

  Future<void> _pick() async {
    final picked = await showTcColorPicker(
      context,
      initial: widget.color,
      title: widget.displayLabel ?? widget.label,
    );
    if (!mounted || picked == null || picked == widget.color) return;
    widget.onChanged(picked);
  }

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final canClear = widget.overridden && widget.onClear != null;
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 3),
      child: Row(
        children: [
          TcTooltip(
            message: 'Pick color…',
            child: MouseRegion(
              cursor: SystemMouseCursors.click,
              child: GestureDetector(
                key: tcColorSwatchKey(widget.label),
                onTap: _pick,
                child: Container(
                  width: 16,
                  height: 16,
                  decoration: BoxDecoration(
                    color: widget.color,
                    border: Border.all(color: tc.borderStrong),
                    borderRadius: tcCorners(context, scale: 0.25),
                  ),
                ),
              ),
            ),
          ),
          const SizedBox(width: 8),
          Expanded(
            child: Tooltip(
              message: widget.label,
              child: Text(
                widget.displayLabel ?? widget.label,
                overflow: TextOverflow.ellipsis,
                softWrap: false,
                style: TextStyle(
                  fontSize: TCType.textCaption,
                  color: widget.overridden ? tc.accentPrimary : tc.textSecondary,
                ),
              ),
            ),
          ),
          const SizedBox(width: 8),
          Container(
            width: 108,
            decoration: BoxDecoration(
              color: tc.bgInset,
              border: Border.all(color: tc.borderDefault),
              borderRadius: tcCorners(context, scale: 0.5),
            ),
            padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 5),
            child: TextField(
              key: tcColorInputKey(widget.label),
              controller: _controller,
              focusNode: _focus,
              onChanged: _onTextChanged,
              onSubmitted: (_) => _syncText(),
              style: TextStyle(fontSize: TCType.textBodySm, color: tc.textPrimary),
              decoration: const InputDecoration(
                isDense: true,
                border: InputBorder.none,
                contentPadding: EdgeInsets.zero,
              ),
            ),
          ),
          const SizedBox(width: 6),
          SizedBox(
            width: 22,
            height: 22,
            child: canClear
                ? TcIconButton(
                    icon: TcIcons.close,
                    tooltip: 'Clear override',
                    size: 22,
                    onPressed: widget.onClear,
                  )
                : null,
          ),
        ],
      ),
    );
  }
}
