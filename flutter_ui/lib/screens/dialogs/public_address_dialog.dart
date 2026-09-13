// The one-time question about the public address echo. A pair of members has
// failed to connect directly because this node cannot learn the address it
// appears at from outside its own network, and no member it can already reach
// is in a position to tell it. The only remaining answer is a server outside
// the channel, and asking one is a disclosure, so it is a question rather than
// a setting that quietly turns itself on.
//
// Asked once per run: "Not now" is an answer for this session (AppState holds
// it, not browser storage), and Settings keeps the switch either way.
import 'package:flutter/material.dart';

import '../../app_state.dart';
import '../../theme/section_theme.dart';
import '../../theme/theme_spec.dart';
import '../../theme/tokens.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_dialog.dart';

/// What the user is told they would be disclosing, in the order that matters:
/// why it is being asked, what the server learns, what it does not, and that
/// nothing breaks if they decline.
const String publicAddressDialogTitle = 'Find this machine’s address?';

const List<String> publicAddressDialogBody = [
  'Two members cannot connect to each other directly: both are behind a '
      'router, and neither can work out the address the other would have to '
      'reach it at. No member either of them can already reach is in a '
      'position to tell them.',
  'A public address-echo service (STUN) answers one question: the address '
      'this machine appears to come from. Turning it on tells that server '
      'this machine’s address and that it asked, and nothing else. It '
      'sees no messages, no channels, and nobody you talk to.',
  'Leaving it off costs nothing but speed: the pair keeps talking over the '
      'mesh, which is slower for files, voice and history. You can change '
      'this either way in Settings, under Direct connections.',
];

/// Asks once, and returns true only when the user turned the echo on.
Future<bool> showPublicAddressDialog(BuildContext context, AppState state) async {
  final enabled = await showTcDialog<bool>(
    context: context,
    builder: (context) => SectionTheme(
      spec: state.themeSpec,
      section: TCSection.dialogs,
      child: Builder(
        builder: (context) => TcDialogShell(
          title: publicAddressDialogTitle,
          actions: [
            TcGhostButton(
              label: 'NOT NOW',
              onPressed: () => Navigator.pop(context, false),
            ),
            TcPrimaryButton(
              label: 'ENABLE',
              onPressed: () => Navigator.pop(context, true),
            ),
          ],
          children: [
            for (final paragraph in publicAddressDialogBody)
              Padding(
                padding: const EdgeInsets.only(bottom: 10),
                child: Text(
                  paragraph,
                  style: TextStyle(
                    fontSize: TCType.textBodySm,
                    height: TCType.leadingBody,
                    color: SectionTheme.of(context).textSecondary,
                  ),
                ),
              ),
          ],
        ),
      ),
    ),
  );
  if (enabled != true) return false;
  return state.setStun(enabled: true);
}
