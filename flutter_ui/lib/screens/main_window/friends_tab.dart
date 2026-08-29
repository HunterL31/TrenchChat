// FRIENDS tab -- saved contacts and the requests waiting on somebody,
// explicitly separate from the channel-scoped ONLINE roster in
// channel_column.dart. Nicknames shown here are tab-only: they never replace a
// peer's self-asserted display name in message bubbles or the presence roster.
//
// Only an accepted friend can be messaged, and only because the peer accepts
// us in return -- each side decides who reaches it, so a pending row is shown
// as exactly that rather than as somebody who can be talked to.
import 'package:flutter/material.dart';

import '../../api/models/dm.dart';
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

/// Share of the tab the request blocks may take between them before scrolling.
const double _requestsMaxFraction = 0.5;

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
  const FriendsTab({super.key, required this.state, this.onOpenNomadPage});

  final AppState state;

  /// Called with a nomad URL when the user opens a hosting friend's page.
  /// Null hides the page buttons.
  final void Function(String url)? onOpenNomadPage;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final friends = state.friends;
    final incoming = state.friendRequests.incoming;
    final outgoing = state.friendRequests.outgoing;
    final blockCount = (incoming.isEmpty ? 0 : 1) + (outgoing.isEmpty ? 0 : 1);
    return Container(
      color: tc.bgApp,
      padding: const EdgeInsets.all(18),
      child: LayoutBuilder(
        builder: (context, constraints) {
          // A request block sizes to its rows and scrolls past this cap, so a
          // long queue can never push the friends list off the tab.
          final blockCap = blockCount == 0
              ? 0.0
              : constraints.maxHeight * _requestsMaxFraction / blockCount;
          return Column(
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
              if (incoming.isNotEmpty) ...[
                ConstrainedBox(
                  constraints: BoxConstraints(maxHeight: blockCap),
                  child: _RequestBlock(
                    // A peer with no friend-request concept asks by messaging,
                    // so the heading says which happened rather than assuming.
                    label: incoming.every((r) => r.isMessageRequest)
                        ? 'SENT YOU A MESSAGE'
                        : 'ASKING TO BE ADDED',
                    requests: incoming,
                    trailing: (r) => Row(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        TcGhostButton(
                          label: 'ACCEPT',
                          onPressed: () => state.acceptFriendRequest(r.identityHash),
                        ),
                        const SizedBox(width: 6),
                        TcGhostButton(
                          label: 'DECLINE',
                          onPressed: () => state.declineFriendRequest(r.identityHash),
                        ),
                      ],
                    ),
                  ),
                ),
                const SizedBox(height: 12),
              ],
              if (outgoing.isNotEmpty) ...[
                ConstrainedBox(
                  constraints: BoxConstraints(maxHeight: blockCap),
                  child: _RequestBlock(
                    label: 'WAITING ON THEM',
                    requests: outgoing,
                    trailing: (r) => TcGhostButton(
                      label: 'CANCEL',
                      onPressed: () => state.cancelFriendRequest(r.identityHash),
                    ),
                  ),
                ),
                const SizedBox(height: 12),
              ],
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
                            style:
                                TextStyle(fontSize: TCType.textBodySm, color: tc.textTertiary),
                          ),
                        )
                      : ListView(
                          padding: const EdgeInsets.symmetric(vertical: 4),
                          children: [
                            for (final f in friends)
                              _FriendRow(
                                friend: f,
                                state: state,
                                onOpenNomadPage: onOpenNomadPage,
                              ),
                          ],
                        ),
                ),
              ),
            ],
          );
        },
      ),
    );
  }
}

class _FriendRow extends StatefulWidget {
  const _FriendRow({required this.friend, required this.state,
      this.onOpenNomadPage});

  final Friend friend;
  final AppState state;
  final void Function(String url)? onOpenNomadPage;

  @override
  State<_FriendRow> createState() => _FriendRowState();
}

class _FriendRowState extends State<_FriendRow> {
  bool _hover = false;

  List<TcContextMenuItem> _menuItems(BuildContext context) => [
    TcContextMenuItem(
      label: 'Message',
      onTap: () => widget.state.openDm(widget.friend.identityHash),
    ),
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
        child: GestureDetector(
          onTap: () => widget.state.openDm(f.identityHash),
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
                if (f.nomadNodeHash != null && widget.onOpenNomadPage != null) ...[
                  const SizedBox(width: 8),
                  TcIconButton(
                    icon: TcIcons.globe,
                    tooltip: 'Open their page',
                    size: 24,
                    onPressed: () => widget.onOpenNomadPage!(
                        '${f.nomadNodeHash}:/page/index.mu'),
                  ),
                ],
              ],
            ),
          ),
        ),
      ),
    );
  }
}

/// A block of friend requests waiting on somebody. The note is text the peer
/// wrote: shown, never acted on.
class _RequestBlock extends StatelessWidget {
  const _RequestBlock({required this.label, required this.requests, required this.trailing});

  final String label;
  final List<FriendRequest> requests;
  final Widget Function(FriendRequest) trailing;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Text(
          label,
          style: TextStyle(
            fontSize: TCType.textCaption,
            color: tc.textSecondary,
            letterSpacing: TCType.letterSpacingFor(TCType.textCaption, TCType.trackingWider),
          ),
        ),
        const SizedBox(height: 6),
        Flexible(
          fit: FlexFit.loose,
          child: Container(
            decoration: BoxDecoration(
              color: tc.bgSurface,
              border: Border.all(color: tc.borderSubtle),
            ),
            child: ListView(
              shrinkWrap: true,
              padding: EdgeInsets.zero,
              children: [
                for (final r in requests)
                  Padding(
                    padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 8),
                    child: Row(
                      children: [
                        Expanded(
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Row(
                                children: [
                                  Flexible(
                                    child: Text(
                                      _requestLabel(r),
                                      overflow: TextOverflow.ellipsis,
                                      style: TextStyle(
                                          fontSize: 13, color: tc.textSecondary),
                                    ),
                                  ),
                                  // A client with no friend-request concept can
                                  // only ask by messaging, so say which it was.
                                  if (r.isMessageRequest && !r.fromTrenchchat) ...[
                                    const SizedBox(width: 6),
                                    Text(
                                      'LXMF',
                                      style: TextStyle(
                                        fontSize: TCType.textMicro,
                                        color: tc.textTertiary,
                                        letterSpacing: TCType.letterSpacingFor(
                                            TCType.textMicro, TCType.trackingWide),
                                      ),
                                    ),
                                  ],
                                ],
                              ),
                              if (r.message != null && r.message!.isNotEmpty)
                                Text(
                                  r.messageCount > 1
                                      ? '${r.message}  (+${r.messageCount - 1} more)'
                                      : r.message!,
                                  maxLines: 2,
                                  overflow: TextOverflow.ellipsis,
                                  style: TextStyle(
                                    fontSize: TCType.textMicro,
                                    color: tc.textTertiary,
                                  ),
                                )
                              else if (r.note.isNotEmpty)
                                Text(
                                  r.note,
                                  maxLines: 2,
                                  overflow: TextOverflow.ellipsis,
                                  style: TextStyle(
                                    fontSize: TCType.textMicro,
                                    color: tc.textTertiary,
                                  ),
                                ),
                            ],
                          ),
                        ),
                        const SizedBox(width: 8),
                        trailing(r),
                      ],
                    ),
                  ),
              ],
            ),
          ),
        ),
      ],
    );
  }

  static String _requestLabel(FriendRequest r) {
    if (r.nickname.isNotEmpty) return r.nickname;
    if (r.displayName.isNotEmpty) return r.displayName;
    return _shortHash(r.identityHash);
  }
}
