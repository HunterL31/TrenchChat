// The one dialog pattern in this app -- every modal (new server, new
// channel, join channel, and whatever gets added after them) should call
// [showTcDialog] and wrap its content in [TcDialogShell] rather than
// inventing its own presentation or reaching for a raw M3 AlertDialog.
// AlertDialog is wrong here: buildAppTheme() sets no dialogTheme, so it
// renders with default Material rounded corners and surface tint, which
// clashes with this terminal-styled design language.
//
// The shell uses NotchedPanel (theme/notch.dart) -- "reserved for emphasis
// panels only" per that file's own comment, and a modal is exactly that --
// plus TCEffects.shadowModal, the ported --shadow-modal token that was
// sitting unused before this.
import 'package:flutter/material.dart';

import '../theme/effects.dart';
import '../theme/notch.dart';
import '../theme/section_theme.dart';
import '../theme/tokens.dart';

/// Presents [builder] as a centered modal with a dark scrim, fade + scale-in
/// transition, and tap-outside-to-dismiss. Returns whatever the dialog's
/// content pops via `Navigator.pop(context, value)`.
Future<T?> showTcDialog<T>({
  required BuildContext context,
  required WidgetBuilder builder,
}) {
  return showGeneralDialog<T>(
    context: context,
    barrierDismissible: true,
    barrierLabel: 'Dismiss',
    barrierColor: const Color.fromRGBO(0, 0, 0, 0.6),
    transitionDuration: TCEffects.durationMed,
    pageBuilder: (context, animation, secondaryAnimation) {
      // showGeneralDialog gives us no Material ancestor (unlike showDialog's
      // Dialog wrapper) -- TextField and other Material widgets need one.
      return Material(
        type: MaterialType.transparency,
        child: SafeArea(
          // viewInsets keeps the panel above an open soft keyboard; the scroll
          // view keeps a modal taller than the viewport reachable instead of
          // overflowing off a short phone screen.
          child: Padding(
            padding: EdgeInsets.only(bottom: MediaQuery.of(context).viewInsets.bottom),
            child: Center(
              child: SingleChildScrollView(
                child: builder(context),
              ),
            ),
          ),
        ),
      );
    },
    transitionBuilder: (context, animation, secondaryAnimation, child) {
      final curved = CurvedAnimation(parent: animation, curve: TCEffects.easeTerminal);
      return FadeTransition(
        opacity: curved,
        child: ScaleTransition(
          scale: Tween<double>(begin: 0.96, end: 1.0).animate(curved),
          child: child,
        ),
      );
    },
  );
}

/// Right-hand room for the scrollbar a desktop scroll behavior draws inside
/// the viewport, over whatever is at its right edge. Give it to a scrollable's
/// padding so the scrollbar never lands on the content: platforms that draw
/// none (and the widget tests, which render as one of them) need no room.
double scrollbarInset(BuildContext context) => switch (Theme.of(context).platform) {
      TargetPlatform.linux ||
      TargetPlatform.macOS ||
      TargetPlatform.windows =>
        TCSpace.space4,
      _ => 0,
    };

/// Shared chrome for dialog content: notched panel, title rule, and a
/// bottom-aligned action row. Dialogs supply their form fields as [children]
/// and their buttons as [actions].
class TcDialogShell extends StatelessWidget {
  const TcDialogShell({
    super.key,
    required this.title,
    required this.children,
    this.actions = const [],
    this.width = 380,
    this.errorText,
  });

  final String title;
  final List<Widget> children;
  final List<Widget> actions;
  final double width;
  final String? errorText;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    // Never wider than the screen minus a margin, so phone viewports fit.
    final maxWidth = MediaQuery.of(context).size.width - 24;
    return NotchedPanel(
      notch: TCSpace.notch,
      color: tc.bgSurfaceRaised,
      border: tc.borderDefault,
      boxShadow: TCEffects.shadowModal,
      child: Container(
        width: maxWidth < width ? maxWidth : width,
        padding: const EdgeInsets.all(20),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              title,
              style: TextStyle(
                fontFamily: SectionTheme.styleOf(context).displayFont,
                fontSize: 22,
                height: 1.1,
                color: tc.textEmphasis,
              ),
            ),
            const SizedBox(height: 10),
            Container(height: 1, color: tc.borderSubtle),
            const SizedBox(height: 16),
            ...children,
            if (errorText != null) ...[
              const SizedBox(height: 12),
              Text(
                errorText!,
                style: TextStyle(fontSize: TCType.textCaption, color: tc.statusDanger),
              ),
            ],
            if (actions.isNotEmpty) ...[
              const SizedBox(height: 20),
              Row(
                mainAxisAlignment: MainAxisAlignment.end,
                children: [
                  for (int i = 0; i < actions.length; i++) ...[
                    if (i > 0) const SizedBox(width: 8),
                    actions[i],
                  ],
                ],
              ),
            ],
          ],
        ),
      ),
    );
  }
}
