// The three-column shell: server rail (1b) + channel column (1b) +
// [channel header (1b) / message list (1a) / compose bar (1a)].
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../../api/models/dm.dart';
import '../../api/models/link_quality.dart';
import '../../api/models/member.dart';
import '../../api/models/message.dart';
import '../../api/models/server.dart';
import '../../api/models/voice.dart';
import '../../app_state.dart';
import '../../attachments.dart';
import '../../clipboard_image.dart';
import '../../clipboard_paste.dart';
import '../../theme/section_theme.dart';
import '../../theme/theme_spec.dart';
import '../../theme/tokens.dart';
import '../../widgets/emoji_text.dart' show nomadUrlRe;
import '../dialogs/add_friend_dialog.dart';
import '../dialogs/confirm_dialog.dart';
import '../dialogs/emoji_picker_dialog.dart';
import '../dialogs/incoming_invite_dialog.dart';
import '../dialogs/invite_dialog.dart';
import '../dialogs/join_channel_dialog.dart';
import '../dialogs/members_dialog.dart';
import '../dialogs/new_channel_dialog.dart';
import '../dialogs/new_server_dialog.dart';
import '../dialogs/permissions_dialog.dart';
import '../dialogs/settings_dialog.dart';
import '../dialogs/start_dm_dialog.dart';
import 'channel_column.dart';
import 'channel_header.dart';
import 'browser_tab.dart';
import 'compose_bar.dart';
import 'friends_tab.dart';
import 'iface_tab.dart';
import 'map_tab.dart';
import 'message_list.dart';
import 'server_rail.dart';
import 'voice_panel.dart';

/// Below this width the three-column shell collapses to a single pane with
/// the rail + channel column in a drawer.
const double compactBreakpoint = 700;

class MainWindow extends StatefulWidget {
  const MainWindow({super.key, required this.state});
  final AppState state;

  @override
  State<MainWindow> createState() => _MainWindowState();
}

class _MainWindowState extends State<MainWindow> {
  ChannelTab _tab = ChannelTab.chat;
  final GlobalKey<ScaffoldState> _scaffoldKey = GlobalKey<ScaffoldState>();

  /// The message the compose bar is replying to, and the channel it belongs
  /// to -- so a reply started in one channel is dropped on switching away.
  Message? _replyTarget;
  String? _replyChannelHash;

  // Dialogs show their own inline error text for a failed submit and claim it
  // with AppState.takeActionError(); this is the catch-all for actions with no
  // UI of their own (a failed send, a failed background reload) so
  // AppState.actionError has exactly one place it surfaces app-wide.
  String? _lastShownActionError;

  /// True while the current staged theme share has already pulled the view
  /// back to chat, so the switch happens once per share.
  bool _themeShareShown = false;

  void _maybeShowActionError(AppState state, TCSectionColors colors, TCSectionStyle style) {
    final message = state.actionError;
    if (message == null || message == _lastShownActionError) return;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      // Re-read rather than trusting the message this frame was built with: a
      // dialog showing the failure itself takes it in the meantime, and then
      // there is nothing left for the snackbar to say.
      final pending = state.actionError;
      if (!mounted || pending == null || pending == _lastShownActionError) return;
      _lastShownActionError = pending;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(
          pending,
          style: TextStyle(fontSize: TCType.textBodySm, color: colors.textPrimary),
        ),
        backgroundColor: colors.bgSurfaceRaised,
        behavior: SnackBarBehavior.floating,
        duration: const Duration(seconds: 4),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(style.cornerRadius),
          side: BorderSide(color: colors.statusDanger),
        ),
      ));
    });
  }

  /// A theme staged from the appearance editor lands in the compose box,
  /// which only the chat tab shows -- so the share brings the chat tab back
  /// with it rather than dropping into a pane that cannot show it.
  void _maybeShowThemeShare(AppState state) {
    if (state.pendingThemeShare == null) {
      _themeShareShown = false;
      return;
    }
    if (_themeShareShown || _tab == ChannelTab.chat) return;
    _themeShareShown = true;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      setState(() => _tab = ChannelTab.chat);
    });
  }

  /// Leaves a channel from its row menu, once confirmed. Stored history is
  /// kept either way, which is what the confirmation says.
  Future<void> _leaveChannel(Channel channel) async {
    final confirmed = await showTcConfirmDialog(
      context,
      widget.state,
      title: 'Leave #${channel.name}',
      message: 'You will stop receiving messages here. '
          'Your local history is kept, and you can join again later.',
      confirmLabel: 'LEAVE',
    );
    if (!confirmed) return;
    await widget.state.leaveChannel(channel.hash);
  }

  /// A conversation's title: the peer's nickname if we gave one, else the
  /// name they assert, else a short form of their hash.
  String _dmLabel(AppState state, DmConversation dm) {
    for (final f in state.friends) {
      if (f.identityHash == dm.peerHash && f.nickname.isNotEmpty) return f.nickname;
    }
    if (dm.displayName.isNotEmpty) return dm.displayName;
    final hex = dm.peerHash;
    if (hex.length <= 8) return hex;
    return '${hex.substring(0, 4)}…${hex.substring(hex.length - 4)}';
  }

  String _serverName(AppState state, String hash) =>
      state.servers.firstWhere((s) => s.hash == hash,
          orElse: () => state.servers.isNotEmpty
              ? state.servers.first
              : throw StateError('no server')).name;

  /// Leaves a server from its rail menu, once confirmed.
  Future<void> _leaveServer(String hash) async {
    final name = _serverName(widget.state, hash);
    final confirmed = await showTcConfirmDialog(
      context,
      widget.state,
      title: 'Leave $name',
      message: 'You will stop receiving this server’s channels. '
          'You can be invited back later.',
      confirmLabel: 'LEAVE',
    );
    if (!confirmed) return;
    await widget.state.leaveServer(hash);
  }

  /// Adds the reaction if the viewer hasn't reacted with [emojiKey] yet,
  /// removes it if they have -- same toggle the chips use.
  void _toggleReaction(String channelHash, String messageId, String emojiKey) {
    final state = widget.state;
    final msg = (state.messagesByChannel[channelHash] ?? [])
        .firstWhere((m) => m.messageId == messageId);
    final mine = msg.reactions
        .where((r) => r.emojiHash == emojiKey)
        .any((r) => r.reactedByMe);
    if (mine) {
      state.api.removeReaction(channelHash, messageId, emojiKey);
    } else {
      state.api.addReaction(channelHash, messageId, emojiKey);
    }
  }

  /// Opens the file picker for the compose bar's + button. A refusal (too
  /// large, unreadable) goes to the app-wide error surface; a cancel is
  /// silent, since the user already knows they cancelled.
  Future<PickedAttachment?> _pickAttachment() async {
    final picked = await pickImageAttachment();
    if (!mounted) return null;
    if (picked.error != null) {
      widget.state.reportError(picked.error!);
      return null;
    }
    return picked.attachment;
  }

  /// Puts a pasted image through the same size gate the file picker applies,
  /// so the compose bar only ever sees one the backend will accept.
  VoidCallback _watchPastedImages(void Function(PickedAttachment) onImage) {
    return watchClipboardImagePaste((PastedImage pasted) {
      final result = attachmentFromBytes(pasted.name, pasted.bytes);
      if (result.error != null) {
        if (mounted) widget.state.reportError(result.error!);
        return;
      }
      onImage(result.attachment!);
    });
  }

  /// Nomad page links open in the NET tab; anything else is the interim
  /// clipboard action (url_launcher is not a dependency -- wire launchUrl
  /// here once it is).
  Future<void> _openLink(String url) async {
    if (nomadUrlRe.matchAsPrefix(url) != null) {
      setState(() => _tab = ChannelTab.browse);
      widget.state.openNomadUrl(url);
      return;
    }
    await Clipboard.setData(ClipboardData(text: url));
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text('Link copied: $url'),
        behavior: SnackBarBehavior.floating,
        duration: const Duration(seconds: 2),
      ),
    );
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
        final spec = state.themeSpec;
        // Chrome that belongs to no single section: base overrides only.
        final baseColors = spec.resolveBase();

        if (state.loading) {
          return const Center(child: CircularProgressIndicator());
        }
        if (state.error != null) {
          return Center(
            child: Text(
              'Failed to load: ${state.error}',
              style: TextStyle(color: baseColors.statusDanger),
            ),
          );
        }

        _maybeShowActionError(state, baseColors, spec.resolveBaseStyle());
        _maybeShowThemeShare(state);

        final selectedServer = state.selectedServerHash;
        final serverName = selectedServer != null
            ? state.servers.firstWhere((s) => s.hash == selectedServer).name
            : null;
        final List<Channel> channels =
            selectedServer != null ? state.channelsByServer[selectedServer] ?? [] : [];
        // Preload permissions/sync state for every listed channel so the row
        // context menu and sync badge are correct without a prior visit. The
        // guard keeps this to one fetch per channel: once loaded, nothing is
        // missing and no callback is scheduled.
        final listedHashes = [
          for (final c in channels) c.hash,
          for (final c in state.standaloneChannels) c.hash,
        ];
        if (listedHashes.any((h) => !state.permissionsByChannel.containsKey(h))) {
          WidgetsBinding.instance
              .addPostFrameCallback((_) => state.ensureChannelMeta(listedHashes));
        }
        final channel = state.selectedChannel;
        final channelHash = state.selectedChannelHash;
        // A conversation has no channel row, so its header and compose gate
        // come from the conversation itself: no permissions to read, and
        // nothing to send once the friendship ends.
        final dm = state.selectedDmHash == null
            ? null
            : state.dmFor(state.selectedDmHash!);
        final headerName = dm != null ? _dmLabel(state, dm) : channel?.name ?? '';
        final headerTopic = dm != null
            ? (dm.isFriend
                ? 'Direct message'
                : 'Direct message — no longer a friend')
            : channel?.description ?? '';
        final List<Message> messages =
            channelHash != null ? state.messagesByChannel[channelHash] ?? [] : [];
        if (_replyTarget != null && _replyChannelHash != channelHash) {
          _replyTarget = null;
          _replyChannelHash = null;
        }
        final List<PresenceEntry> presence =
            channelHash != null ? state.presenceByChannel[channelHash] ?? [] : [];
        final linkQuality = channelHash != null
            ? state.linkQualityByChannel[channelHash] ?? ChannelLinkQuality.unknown
            : ChannelLinkQuality.unknown;
        final permissions = channelHash != null ? state.permissionsByChannel[channelHash] : null;
        final friendHashes = state.friends.map((f) => f.identityHash).toSet();

        final List<VoiceParticipant> voiceRoster = channelHash != null
            ? state.voiceRosterByChannel[channelHash] ?? const []
            : const [];
        final inVoice = state.voiceChannelHash != null;
        // GUI gate, mirroring actions.join_voice_channel: open-join channels
        // need no permission row; the actions guard and VoiceManager's core
        // enforcement remain the real boundaries.
        final canJoinVoice = channel != null &&
            channelHash != null &&
            !inVoice &&
            (channel.openJoin || (permissions?.voiceChat ?? false));

        final compact = MediaQuery.of(context).size.width < compactBreakpoint;

        final rail = SectionTheme(
          spec: spec,
          section: TCSection.serverRail,
          child: ServerRail(
            servers: [
              for (final s in state.servers)
                ServerRailEntry(
                  hash: s.hash,
                  name: s.name,
                  canInvite: state.serverPermissionsByHash[s.hash]?.invite ?? false,
                  canManage: state.serverPermissionsByHash[s.hash]?.manageChannel ?? false,
                  hasUnread: (state.channelsByServer[s.hash] ?? [])
                      .any((c) => (state.unreadByChannel[c.hash] ?? 0) > 0),
                ),
            ],
            selectedHash: state.selectedServerHash,
            onSelect: (hash) => state.selectServer(hash),
            onHome: () => state.selectHome(),
            onAddServer: () => showNewServerDialog(context, state),
            onSettings: () => showSettingsDialog(context, state),
            onLeaveServer: _leaveServer,
            onInviteServer: (hash) => showServerInviteDialog(
                context, state,
                serverHashHex: hash, serverName: _serverName(state, hash)),
            onEditServerPermissions: (hash) => showServerPermissionsDialog(
                context, state,
                serverHashHex: hash, serverName: _serverName(state, hash)),
          ),
        );

        final channelColumn = ChannelColumn(
          serverName: serverName,
          serverMemberCount:
              selectedServer != null ? state.serverMemberCounts[selectedServer] : null,
          channels: channels,
          directChannels: state.standaloneChannels,
          selectedChannelHash: state.selectedChannelHash,
          onSelectChannel: (hash) {
            state.selectChannel(hash);
            if (_scaffoldKey.currentState?.isDrawerOpen ?? false) {
              _scaffoldKey.currentState!.closeDrawer();
            }
          },
          onlinePresence: presence,
          meHashHex: state.meHashHex,
          pendingInvites: state.pendingInvites,
          onTapInvite: (invite) => showIncomingInviteDialog(context, state, invite),
          onCreateChannel: () =>
              showNewChannelDialog(context, state, serverHashHex: selectedServer),
          onCreateDirectChannel: () => showNewChannelDialog(context, state),
          dms: state.dms,
          onSelectDm: (hash) {
            state.selectDm(hash);
            if (_scaffoldKey.currentState?.isDrawerOpen ?? false) {
              _scaffoldKey.currentState!.closeDrawer();
            }
          },
          onDeleteDm: (dm) => state.deleteDm(dm.hash),
          onStartDm: () => showStartDmDialog(context, state),
          onJoinChannel: () => showJoinChannelDialog(context, state),
          friendHashes: friendHashes,
          onAddFriend: (hash) => showAddFriendDialog(context, state, identityHash: hash),
          voiceParticipants: voiceRoster,
          onJoinVoice: canJoinVoice ? () => state.joinVoice(channelHash) : null,
          syncStates: state.syncStateByChannel,
          unreadCounts: state.unreadByChannel,
          channelPermissions: state.permissionsByChannel,
          onViewMembers: (c) => showMembersDialog(context, state,
              channelHashHex: c.hash, channelName: c.name),
          onInviteToChannel: (c) => showInviteDialog(context, state,
              channelHashHex: c.hash, channelName: c.name),
          onEditPermissions: (c) => showPermissionsDialog(context, state,
              channelHashHex: c.hash, channelName: c.name),
          onLeaveChannel: _leaveChannel,
        );

        final voicePanel = inVoice
            ? VoicePanel(
                channelName: state.channelByHash(state.voiceChannelHash!)?.name ?? '',
                quality: state.voiceQualityLevel,
                muted: state.voiceMuted,
                audioError: state.voiceAudioError,
                onToggleMute: () => state.toggleVoiceMute(),
                onLeave: () => state.leaveVoice(),
              )
            : null;
        final channelPane = SectionTheme(
          spec: spec,
          section: TCSection.channelList,
          child: voicePanel == null
              ? channelColumn
              : Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [Expanded(child: channelColumn), voicePanel],
                ),
        );

        final content = SectionTheme(
          spec: spec,
          section: TCSection.content,
          child: Column(
                children: [
                  SectionTheme(
                    spec: spec,
                    section: TCSection.topBar,
                    child: ChannelHeader(
                      channelName: headerName,
                      topic: headerTopic,
                      linkQuality: linkQuality,
                      connectionState: state.connectionState,
                      activeTab: _tab,
                      onTabSelected: (t) => setState(() => _tab = t),
                      compact: compact,
                      onOpenNav:
                          compact ? () => _scaffoldKey.currentState?.openDrawer() : null,
                      onViewMembers: channel == null || channelHash == null
                          ? null
                          : () => showMembersDialog(
                                context,
                                state,
                                channelHashHex: channelHash,
                                channelName: channel.name,
                              ),
                    ),
                  ),
                  Expanded(
                    child: switch (_tab) {
                      ChannelTab.map => MapTab(state: state),
                      ChannelTab.iface => IfaceTab(state: state),
                      ChannelTab.friends =>
                        FriendsTab(state: state, onOpenNomadPage: _openLink),
                      ChannelTab.browse => BrowserTab(state: state),
                      ChannelTab.chat => MessageList(
                            messages: messages,
                            meHashHex: state.meHashHex,
                            displayNameFor: _displayNameFor,
                            avatarBytesFor: (hash) => state.avatarCache[hash],
                            ensureAvatarLoaded: (hash) => state.avatarFor(hash),
                            attachmentBytesFor: channelHash == null
                                ? null
                                : (messageId) => state.attachmentCache[
                                    AppState.attachmentKey(channelHash, messageId)],
                            ensureAttachmentLoaded: channelHash == null
                                ? null
                                : (messageId) =>
                                    state.attachmentFor(channelHash, messageId),
                            emojiLibrary: state.customEmojis,
                            onToggleReaction: channelHash == null
                                ? null
                                : (messageId, emojiHash) =>
                                    _toggleReaction(channelHash, messageId, emojiHash),
                            onReact: channelHash == null
                                ? null
                                : (messageId) async {
                                    final selection =
                                        await showEmojiPickerDialog(context, state);
                                    if (selection == null) return;
                                    _toggleReaction(
                                        channelHash, messageId, selection.reactionKey);
                                  },
                            onReply: channelHash == null
                                ? null
                                : (m) => setState(() {
                                      _replyTarget = m;
                                      _replyChannelHash = channelHash;
                                    }),
                            onOpenLink: _openLink,
                            friendHashes: friendHashes,
                            onAddFriend: (hash) =>
                                showAddFriendDialog(context, state, identityHash: hash),
                            onAddTheme: state.saveThemeAs,
                            onApplyTheme: (spec) async {
                              await state.saveTheme(spec);
                              return state.themeSpec == spec;
                            },
                            themeLibrary: state.themeLibrary,
                            onLoadOlder: channelHash == null
                                ? null
                                : () => state.loadOlderMessages(channelHash),
                            hasMoreOlder:
                                channelHash != null && state.hasMoreOlder(channelHash),
                            loadingOlder:
                                channelHash != null && state.loadingOlder(channelHash),
                          ),
                    },
                  ),
                  // In compact mode the drawer hides the column's panel, so
                  // mute/leave stay reachable above the compose bar.
                  if (compact && voicePanel != null)
                    SectionTheme(
                      spec: spec,
                      section: TCSection.channelList,
                      child: voicePanel,
                    ),
                  if (_tab == ChannelTab.chat)
                    ComposeBar(
                      channelName: headerName,
                      channelHash: channelHash,
                      enabled: dm != null
                          ? dm.isFriend
                          : channelHash != null && (permissions?.sendMessage ?? true),
                      replyPreview: _replyTarget == null
                          ? null
                          : (
                              author: _replyTarget!.senderHash == state.meHashHex
                                  ? 'you'
                                  : _displayNameFor(
                                      _replyTarget!.senderHash, _replyTarget!.senderName),
                              snippet: _replyTarget!.content.replaceAll('\n', ' '),
                            ),
                      onCancelReply: () => setState(() => _replyTarget = null),
                      onSend: (content, attachment) async {
                        final replyTo = _replyTarget?.messageId;
                        // A conversation is addressed by peer, not by
                        // channel, and carries no permissions to check.
                        final ok = state.selectedDmHash != null
                            ? await state.sendDirectMessage(
                                content,
                                replyTo: replyTo,
                                imageData: attachment?.bytes,
                              )
                            : await state.sendMessage(
                                content,
                                replyTo: replyTo,
                                imageDataB64: attachment == null
                                    ? null
                                    : base64Encode(attachment.bytes),
                              );
                        if (ok && _replyTarget != null) {
                          setState(() => _replyTarget = null);
                        }
                        return ok;
                      },
                      pickAttachment: _pickAttachment,
                      watchPastedImages: _watchPastedImages,
                      pickEmoji: () async =>
                          (await showEmojiPickerDialog(context, state, title: 'Add emoji'))
                              ?.composeToken,
                      pendingThemeShare: state.pendingThemeShare,
                      onThemeShareConsumed: state.consumePendingThemeShare,
                      compact: compact,
                    ),
                ],
              ),
        );

        if (compact) {
          return Scaffold(
            key: _scaffoldKey,
            backgroundColor: baseColors.bgApp,
            drawer: Drawer(
              width: 266,
              backgroundColor: baseColors.bgSurface,
              shape: const RoundedRectangleBorder(),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [rail, Expanded(child: channelPane)],
              ),
            ),
            body: SafeArea(child: content),
          );
        }

        return Scaffold(
          backgroundColor: baseColors.bgApp,
          body: Row(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              rail,
              SizedBox(width: 206, child: channelPane),
              Expanded(child: content),
            ],
          ),
        );
      },
    );
  }
}
