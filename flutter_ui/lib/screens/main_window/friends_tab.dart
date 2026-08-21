// FRIENDS tab -- locally saved contacts, explicitly separate from the
// channel-scoped ONLINE roster in channel_column.dart. Nicknames shown here
// are tab-only: they never replace a peer's self-asserted display name in
// message bubbles or the presence roster.
import 'package:flutter/material.dart';

import '../../api/models/friend.dart';
import '../../app_state.dart';
import '../../format.dart';
import '../../theme/section_theme.dart';
import '../../theme/tokens.dart';
import '../../widgets/status_dot.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_context_menu.dart';
import '../../widgets/tc_icon.dart';
import '../dialogs/add_friend_dialog.dart';

String friendLabel(Friend f) {
  if (f.nickname.isNotEmpty) return f.nickname;
  if (f.displayName.isNotEmpty) return f.displayName;
  return _shortHash(f.identityHash);
}

String _shortHash(String hex) {
  if (hex.length <= 8) return hex;
  return '${hex.substring(0, 4)}…${hex.substring(hex.length - 4)}';
}

class FriendsTab extends StatelessWidget {
  const FriendsTab({super.key, required this.state});

  final AppState state;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final friends = state.friends;
    return Container(
      color: tc.bgApp,
      padding: const EdgeInsets.all(18),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              Text(
                'FRIENDS',
                style: TextStyle(
                  fontSize: TCType.textCaption,
                  color: tc.textSecondary,
                  letterSpacing:
                      TCType.letterSpacingFor(TCType.textCaption, TCType.trackingWider),
                ),
              ),
              const Spacer(),
              TcGhostButton(
                icon: TcIcons.plus,
                label: 'ADD',
                onPressed: () => showAddFriendDialog(context, state),
              ),
            ],
          ),
          const SizedBox(height: 12),
          Expanded(
            child: Container(
              decoration: BoxDecoration(
                color: tc.bgSurface,
                border: Border.all(color: tc.borderSubtle),
              ),
              child: friends.isEmpty
                  ? Center(
                      child: Text(
                        'No saved friends yet.',
                        style: TextStyle(fontSize: TCType.textBodySm, color: tc.textTertiary),
                      ),
                    )
                  : ListView(
                      padding: const EdgeInsets.symmetric(vertical: 4),
                      children: [
                        for (final f in friends) _FriendRow(friend: f, state: state),
                      ],
                    ),
            ),
          ),
        ],
      ),
    );
  }
}

class _FriendRow extends StatefulWidget {
  const _FriendRow({required this.friend, required this.state});

  final Friend friend;
  final AppState state;

  @override
  State<_FriendRow> createState() => _FriendRowState();
}

class _FriendRowState extends State<_FriendRow> {
  bool _hover = false;

  List<TcContextMenuItem> _menuItems(BuildContext context) => [
        TcContextMenuItem(
          label: 'Edit friend…',
          onTap: () =>
              showAddFriendDialog(context, widget.state, identityHash: widget.friend.identityHash),
        ),
        TcContextMenuItem(
          label: 'Remove friend',
          onTap: () => widget.state.removeFriend(widget.friend.identityHash),
        ),
      ];

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final f = widget.friend;
    return MouseRegion(
      cursor: SystemMouseCursors.click,
      onEnter: (_) => setState(() => _hover = true),
      onExit: (_) => setState(() => _hover = false),
      child: TcContextMenuRegion(
        items: _menuItems(context),
        child: Container(
          color: _hover ? tc.bgHover : Colors.transparent,
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 6),
          child: Row(
            children: [
              StatusDot(
                status: f.isOnline ? PresenceStatus.online : PresenceStatus.offline,
                size: 10,
              ),
              const SizedBox(width: 9),
              Expanded(
                child: Text(
                  friendLabel(f),
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(fontSize: 13, color: tc.textSecondary),
                ),
              ),
              const SizedBox(width: 6),
              Text(
                formatRelative(f.lastSeenAt),
                style: TextStyle(fontSize: TCType.textMicro, color: tc.textTertiary),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
