// A tooltip that never takes the press.
//
// Material's Tooltip defaults to TooltipTriggerMode.longPress, which arms a
// LongPressGestureRecognizer for touch, stylus, trackpad and unknown pointers.
// That recognizer competes in the gesture arena with the control underneath,
// so on those devices a press can show the tip instead of pressing the button
// -- and the user has to press again. Hover is not affected by trigger mode,
// so pointing at the control still explains it.
//
// Every tooltip on something clickable should be one of these; a tooltip on
// plain text (a truncated label, an inline emoji) can stay a bare Tooltip,
// where long-press-to-read is the only way to read it at all.
import 'package:flutter/material.dart';

class TcTooltip extends StatelessWidget {
  const TcTooltip({super.key, required this.message, required this.child});

  final String message;
  final Widget child;

  @override
  Widget build(BuildContext context) {
    return Tooltip(
      message: message,
      triggerMode: TooltipTriggerMode.manual,
      child: child,
    );
  }
}
