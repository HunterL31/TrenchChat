// Labeled bordered text input for dialog forms. Styling mirrors the only
// other styled text input in the app, compose_bar.dart's TextField config,
// wrapped in a bordered box since dialog fields (unlike the chromeless
// compose row) need a visible boundary.
import 'package:flutter/material.dart';

import '../theme/tokens.dart';

class TcTextField extends StatelessWidget {
  const TcTextField({
    super.key,
    required this.label,
    required this.controller,
    this.hintText,
    this.autofocus = false,
    this.onSubmitted,
  });

  final String label;
  final TextEditingController controller;
  final String? hintText;
  final bool autofocus;
  final ValueChanged<String>? onSubmitted;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          label,
          style: TextStyle(
            fontSize: TCType.textCaption,
            color: TCColors.textSecondary,
            letterSpacing: TCType.letterSpacingFor(TCType.textCaption, TCType.trackingWide),
          ),
        ),
        const SizedBox(height: 6),
        Container(
          decoration: BoxDecoration(
            color: TCColors.bgInset,
            border: Border.all(color: TCColors.borderDefault),
          ),
          padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
          child: TextField(
            controller: controller,
            autofocus: autofocus,
            onSubmitted: onSubmitted,
            style: TextStyle(fontSize: TCType.textBodyMd, color: TCColors.textPrimary),
            decoration: InputDecoration(
              isDense: true,
              border: InputBorder.none,
              hintText: hintText,
              hintStyle: TextStyle(fontSize: TCType.textBodyMd, color: TCColors.textTertiary),
            ),
          ),
        ),
      ],
    );
  }
}
