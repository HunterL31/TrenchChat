// A tooltip that never takes the press, wearing the theme.
//
// Material's Tooltip defaults to TooltipTriggerMode.longPress, which arms a
// LongPressGestureRecognizer for touch, stylus, trackpad and unknown pointers.
// That recognizer competes in the gesture arena with the control underneath,
// so on those devices a press can show the tip instead of pressing the button
// -- and the user has to press again. Hover is not affected by trigger mode,
// so pointing at the control still explains it.
//
// It also carries the enclosing section's palette and corner radius:
// Material's own tooltip is a light grey box that belongs to no theme in this
// app. [tcTooltipDecoration] and [tcTooltipTextStyle] are what a bare Tooltip
// -- one on plain text, where long-press-to-read is the only way to read it
// at all -- should be given so the app has one tooltip look.
import 'package:flutter/material.dart';

import '../theme/section_theme.dart';
import '../theme/shape.dart';
import '../theme/tokens.dart';

/// The tooltip surface for the section [context] sits in.
Decoration tcTooltipDecoration(BuildContext context) {
  final tc = SectionTheme.of(context);
  return BoxDecoration(
    color: tc.bgSurfaceRaised,
    border: Border.all(color: tc.borderDefault),
    borderRadius: tcCorners(context, scale: 0.5),
  );
}

/// The tooltip label style for the section [context] sits in.
TextStyle tcTooltipTextStyle(BuildContext context) => TextStyle(
      fontFamily: TCType.fontMono,
      fontSize: TCType.textCaption,
      color: SectionTheme.of(context).textPrimary,
    );

class TcTooltip extends StatelessWidget {
  const TcTooltip({super.key, required this.message, required this.child});

  final String message;
  final Widget child;

  @override
  Widget build(BuildContext context) {
    return Tooltip(
      message: message,
      triggerMode: TooltipTriggerMode.manual,
      decoration: tcTooltipDecoration(context),
      textStyle: tcTooltipTextStyle(context),
      child: child,
    );
  }
}
