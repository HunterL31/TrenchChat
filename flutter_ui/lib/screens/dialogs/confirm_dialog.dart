// The one yes/no modal. Destructive menu actions (leaving a channel) need a
// confirmation, and inventing a bespoke dialog for each of them is how a
// design language stops being one -- so they all come through here.
import 'package:flutter/material.dart';

import '../../app_state.dart';
import '../../theme/section_theme.dart';
import '../../theme/theme_spec.dart';
import '../../theme/tokens.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_dialog.dart';

/// Asks [message] under [title]. Resolves true only when the confirming
/// action was chosen; dismissing counts as no.
Future<bool> showTcConfirmDialog(
  BuildContext context,
  AppState state, {
  required String title,
  required String message,
  required String confirmLabel,
}) async {
  final confirmed = await showTcDialog<bool>(
    context: context,
    builder: (context) => SectionTheme(
      spec: state.themeSpec,
      section: TCSection.dialogs,
      child: Builder(
        builder: (context) => TcDialogShell(
          title: title,
          actions: [
            TcGhostButton(label: 'CANCEL', onPressed: () => Navigator.pop(context, false)),
            TcPrimaryButton(label: confirmLabel, onPressed: () => Navigator.pop(context, true)),
          ],
          children: [
            Text(
              message,
              style: TextStyle(
                fontSize: TCType.textBodySm,
                height: TCType.leadingBody,
                color: SectionTheme.of(context).textSecondary,
              ),
            ),
          ],
        ),
      ),
    ),
  );
  return confirmed ?? false;
}
