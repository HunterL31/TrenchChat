// Labeled bordered text input for dialog forms. Styling mirrors the only
// other styled text input in the app, compose_bar.dart's TextField config,
// wrapped in a bordered box since dialog fields (unlike the chromeless
// compose row) need a visible boundary.
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../theme/section_theme.dart';
import '../theme/shape.dart';
import '../theme/tokens.dart';

class TcTextField extends StatefulWidget {
  const TcTextField({
    super.key,
    required this.label,
    required this.controller,
    this.hintText,
    this.autofocus = false,
    this.onSubmitted,
    this.readOnly = false,
    this.inputFormatters,
  });

  final String label;
  final TextEditingController controller;
  final String? hintText;
  final bool autofocus;
  final ValueChanged<String>? onSubmitted;

  /// Passed straight to the inner field -- a length cap, a character filter.
  final List<TextInputFormatter>? inputFormatters;

  /// When true, the field displays its value but rejects edits -- used for
  /// an identity hash pre-filled from a context menu (see add_friend_dialog.dart).
  final bool readOnly;

  @override
  State<TcTextField> createState() => _TcTextFieldState();
}

class _TcTextFieldState extends State<TcTextField> {
  final FocusNode _focusNode = FocusNode();

  @override
  void initState() {
    super.initState();
    if (!widget.autofocus) return;
    // The framework grants autofocus while the dialog's route is still coming
    // in. Asking once more with the first frame up is what makes a freshly
    // opened dialog actually take what is typed into it -- web browsers in
    // particular ignore a focus that arrives too early, leaving a field that
    // looks ready and swallows every keystroke.
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted && !_focusNode.hasFocus) _focusNode.requestFocus();
    });
  }

  @override
  void dispose() {
    _focusNode.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          widget.label,
          style: TextStyle(
            fontSize: TCType.textCaption,
            color: tc.textSecondary,
            letterSpacing: TCType.letterSpacingFor(TCType.textCaption, TCType.trackingWide),
          ),
        ),
        const SizedBox(height: 6),
        Container(
          decoration: BoxDecoration(
            color: tc.bgInset,
            border: Border.all(color: tc.borderDefault),
            borderRadius: tcCorners(context, scale: 0.5),
          ),
          padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
          child: TextField(
            controller: widget.controller,
            focusNode: _focusNode,
            autofocus: widget.autofocus,
            onSubmitted: widget.onSubmitted,
            readOnly: widget.readOnly,
            inputFormatters: widget.inputFormatters,
            style: TextStyle(
              fontSize: TCType.textBodyMd,
              color: widget.readOnly ? tc.textSecondary : tc.textPrimary,
            ),
            decoration: InputDecoration(
              isDense: true,
              border: InputBorder.none,
              hintText: widget.hintText,
              hintStyle: TextStyle(fontSize: TCType.textBodyMd, color: tc.textTertiary),
            ),
          ),
        ),
      ],
    );
  }
}
