// The three-column shell: server rail (1b) + channel column (1b) +
// [channel header (1b) / message list (1a) / compose bar (1a)].
import 'package:flutter/material.dart';

import '../../api/models/link_quality.dart';
import '../../api/models/member.dart';
import '../../api/models/message.dart';
import '../../api/models/server.dart';
import '../../app_state.dart';
import '../../theme/tokens.dart';
import '../dialogs/join_channel_dialog.dart';
import '../dialogs/new_channel_dialog.dart';
import '../dialogs/new_server_dialog.dart';
import 'channel_column.dart';
import 'channel_header.dart';
import 'compose_bar.dart';
import 'message_list.dart';
import 'server_rail.dart';

class MainWindow extends StatefulWidget {
  const MainWindow({super.key, required this.state});
  final AppState state;

  @override
  State<MainWindow> createState() => _MainWindowState();
}

class _MainWindowState extends State<MainWindow> {
  ChannelTab _tab = ChannelTab.chat;

  // Dialogs show their own inline error text for a failed submit; this is
  // the catch-all for actions with no dialog to show it in (a failed send,
  // a failed background reload) so AppState.actionError has exactly one
  // place it surfaces app-wide.
  String? _lastShownActionError;

  void _maybeShowActionError(AppState state) {
    final message = state.actionError;
    if (message == null || message == _lastShownActionError) return;
    _lastShownActionError = message;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(message)));
    });
  }

  String _displayNameFor(String identityHashHex, String fallback) {
    final channelHash = widget.state.selectedChannelHash;
    if (channelHash != null) {
      final members = widget.state.membersByChannel[channelHash] ?? [];
      for (final m in members) {
        if (m.identityHash == identityHashHex && m.displayName.isNotEmpty) {
          return m.displayName;
        }
      }
    }
    if (fallback.isNotEmpty) return fallback;
    return identityHashHex.substring(0, identityHashHex.length >= 8 ? 8 : identityHashHex.length);
  }

  @override
  Widget build(BuildContext context) {
    final state = widget.state;

    return AnimatedBuilder(
      animation: state,
      builder: (context, _) {
        if (state.loading) {
          return const Center(child: CircularProgressIndicator());
        }
        if (state.error != null) {
          return Center(
            child: Text(
              'Failed to load: ${state.error}',
              style: TextStyle(color: TCColors.statusDanger),
            ),
          );
        }

        _maybeShowActionError(state);

        final selectedServer = state.selectedServerHash;
        final serverName = selectedServer != null
            ? state.servers.firstWhere((s) => s.hash == selectedServer).name
            : null;
        final List<Channel> channels =
            selectedServer != null ? state.channelsByServer[selectedServer] ?? [] : [];
        final channel = state.selectedChannel;
        final channelHash = state.selectedChannelHash;
        final List<Message> messages =
            channelHash != null ? state.messagesByChannel[channelHash] ?? [] : [];
        final List<PresenceEntry> presence =
            channelHash != null ? state.presenceByChannel[channelHash] ?? [] : [];
        final linkQuality = channelHash != null
            ? state.linkQualityByChannel[channelHash] ?? ChannelLinkQuality.unknown
            : ChannelLinkQuality.unknown;
        final permissions = channelHash != null ? state.permissionsByChannel[channelHash] : null;

        return Row(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            ServerRail(
              servers: [
                for (final s in state.servers) ServerRailEntry(hash: s.hash, name: s.name),
              ],
              selectedHash: state.selectedServerHash,
              onSelect: (hash) => state.selectServer(hash),
              onAddServer: () => showNewServerDialog(context, state),
            ),
            ChannelColumn(
              serverName: serverName,
              serverMemberCount:
                  selectedServer != null ? state.serverMemberCounts[selectedServer] : null,
              channels: channels,
              directChannels: state.standaloneChannels,
              selectedChannelHash: state.selectedChannelHash,
              onSelectChannel: (hash) => state.selectChannel(hash),
              onlinePresence: presence,
              onCreateChannel: () =>
                  showNewChannelDialog(context, state, serverHashHex: selectedServer),
              onJoinChannel: () => showJoinChannelDialog(context, state),
            ),
            Expanded(
              child: Column(
                children: [
                  ChannelHeader(
                    channelName: channel?.name ?? '',
                    topic: channel?.description ?? '',
                    linkQuality: linkQuality,
                    activeTab: _tab,
                    onTabSelected: (t) => setState(() => _tab = t),
                  ),
                  Expanded(
                    child: _tab == ChannelTab.chat
                        ? MessageList(
                            messages: messages,
                            meHashHex: state.meHashHex,
                            displayNameFor: _displayNameFor,
                            avatarBytesFor: (hash) => state.avatarCache[hash],
                            ensureAvatarLoaded: (hash) => state.avatarFor(hash),
                            onToggleReaction: channelHash == null
                                ? null
                                : (messageId, emojiHash) {
                                    final msg = (state.messagesByChannel[channelHash] ?? [])
                                        .firstWhere((m) => m.messageId == messageId);
                                    final mine = msg.reactions
                                        .where((r) => r.emojiHash == emojiHash)
                                        .any((r) => r.reactedByMe);
                                    if (mine) {
                                      state.api.removeReaction(channelHash, messageId, emojiHash);
                                    } else {
                                      state.api.addReaction(channelHash, messageId, emojiHash);
                                    }
                                  },
                          )
                        : Center(
                            child: Text(
                              _tab == ChannelTab.map ? 'MAP not in this spike' : 'IFACE not in this spike',
                              style: TextStyle(color: TCColors.textTertiary),
                            ),
                          ),
                  ),
                  if (_tab == ChannelTab.chat)
                    ComposeBar(
                      channelName: channel?.name ?? '',
                      enabled: channelHash != null && (permissions?.sendMessage ?? true),
                      onSend: (content) async {
                        await state.sendMessage(content);
                      },
                    ),
                ],
              ),
            ),
          ],
        );
      },
    );
  }
}
