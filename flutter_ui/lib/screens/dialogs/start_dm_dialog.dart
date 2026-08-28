// Start a direct message -- a picker over accepted friends, because they are
// the only peers a conversation can exist with. A friend still waiting on a
// request, or one who has not added us back, is deliberately absent: the
// message would be refused at their end, and offering it here would only
// hide that.
import 'package:flutter/material.dart';

import '../../api/models/friend.dart';
import '../../app_state.dart';
import '../../theme/section_theme.dart';
import '../../theme/theme_spec.dart';
import '../../theme/tokens.dart';
import '../../widgets/status_dot.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_dialog.dart';
import '../dialogs/add_friend_dialog.dart';

Future<void> showStartDmDialog(BuildContext context, AppState state) {
  return showTcDialog<void>(
    context: context,
    builder: (context) => SectionTheme(
      spec: state.themeSpec,
      section: TCSection.dialogs,
      child: _StartDmDialogContent(state: state),
    ),
  );
}

class _StartDmDialogContent extends StatelessWidget {
  const _StartDmDialogContent({required this.state});

  final AppState state;

  static String _label(Friend f) {
    if (f.nickname.isNotEmpty) return f.nickname;
    if (f.displayName.isNotEmpty) return f.displayName;
    final hex = f.identityHash;
    if (hex.length <= 8) return hex;
    return '${hex.substring(0, 4)}…${hex.substring(hex.length - 4)}';
  }

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final friends = state.friends;
    return TcDialogShell(
      title: 'New Direct Message',
      actions: [
        TcGhostButton(label: 'CLOSE', onPressed: () => Navigator.pop(context)),
        if (friends.isEmpty)
          TcPrimaryButton(
            label: 'ADD A FRIEND',
            onPressed: () {
              Navigator.pop(context);
              showAddFriendDialog(context, state);
            },
          ),
      ],
      children: [
        if (friends.isEmpty)
          Text(
            'A direct message needs a friend on both sides. Add someone, or '
            'accept a request, and they will appear here.',
            style: TextStyle(fontSize: TCType.textBodySm, color: tc.textTertiary),
          )
        else
          ConstrainedBox(
            constraints: const BoxConstraints(maxHeight: 280),
            child: ListView(
              shrinkWrap: true,
              children: [
                for (final f in friends)
                  InkWell(
                    onTap: () {
                      Navigator.pop(context);
                      state.openDm(f.identityHash);
                    },
                    child: Padding(
                      padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 8),
                      child: Row(
                        children: [
                          StatusDot(
                            status: f.isOnline
                                ? PresenceStatus.online
                                : PresenceStatus.offline,
                            size: 10,
                          ),
                          const SizedBox(width: 9),
                          Expanded(
                            child: Text(
                              _label(f),
                              overflow: TextOverflow.ellipsis,
                              style: TextStyle(fontSize: 13, color: tc.textSecondary),
                            ),
                          ),
                        ],
                      ),
                    ),
                  ),
              ],
            ),
          ),
      ],
    );
  }
}
