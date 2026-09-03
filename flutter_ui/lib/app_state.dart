// Central app state: owns the ApiClient/TcSocket, holds everything the
// three-column shell reads, and applies live WS events. Kept as one
// ChangeNotifier rather than a state-management package -- the surface
// area here is small enough that a package would add ceremony, not clarity.
import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;

import 'api/client.dart';
import 'api/events.dart';
import 'api/models/app_version.dart';
import 'api/models/dm.dart';
import 'api/models/emoji.dart';
import 'api/models/friend.dart';
import 'api/models/invite.dart';
import 'api/models/link_quality.dart';
import 'api/models/member.dart';
import 'api/models/message.dart';
import 'api/models/nomad.dart';
import 'api/models/permissions.dart';
import 'api/models/server.dart';
import 'api/models/settings.dart';
import 'api/models/voice.dart';
import 'api/ws.dart';
import 'attachments.dart';
import 'theme/theme_spec.dart';

/// How long reaction events for one channel are coalesced before the
/// channel's messages are re-fetched.
const Duration _reactionRefreshWindow = Duration(milliseconds: 250);

/// How many messages a single history page holds. A full page back means
/// there may be more; a short page is the end of history.
const int messagePageSize = 50;

/// What a send or share refusal reads as. The reasons are api.py's and
/// actions.py's own machine-readable set; anything unrecognised says the
/// message did not go rather than inventing a cause.
String sendRefusalMessage(String? reason) => switch (reason) {
      'no_send_permission' => "You don't have permission to send in this channel.",
      'no_share_permission' =>
        'You do not have permission to share files in this channel.',
      'open_join_channel' => 'Files are shared in invite-only channels only.',
      'no_channel' => 'That channel is not known here.',
      'no_recipients' =>
        'Not sent: no known subscribers to deliver to yet. Try again once peers are online.',
      'storage' => 'Not enough file storage on this node.',
      'file_too_large' => 'That file is over the size this node shares.',
      'file_and_image' => 'A message carries an image or a file, not both.',
      'incomplete_file' => 'That file could not be read.',
      'empty_file' => 'That file is empty.',
      'bad_file_base64' => 'That file could not be read.',
      'bad_manifest' => 'That file could not be shared.',
      'no_file_in_dm' => 'Files are not shared in direct messages.',
      _ => 'Message was not sent.',
    };

class AppState extends ChangeNotifier {
  /// [httpClient] lets tests inject a mock transport; the real app leaves it
  /// null and gets a standard IO client. [saveFileBytes] is the same seam for
  /// the save dialog, which is a plugin call widget tests must not make.
  AppState(
      {required String baseUrl,
      http.Client? httpClient,
      String token = '',
      FileSaver? saveFileBytes})
      : api = ApiClient(baseUrl: baseUrl, client: httpClient, token: token),
        _saveFileBytes = saveFileBytes ?? saveBytesToFile,
        _socket = TcSocket(baseUrl: baseUrl, token: token);

  final ApiClient api;
  final FileSaver _saveFileBytes;
  final TcSocket _socket;
  StreamSubscription<TcEvent>? _sub;

  String meHashHex = '';
  String meDisplayName = '';

  /// The running build, and what the installer that delivered it replaced.
  /// Unknown until [loadVersion] answers -- a backend too old to serve
  /// /version simply leaves it that way.
  AppVersionInfo appVersion = AppVersionInfo.unknown;

  List<Server> servers = [];
  List<Channel> standaloneChannels = [];
  List<Channel> discoveredChannels = [];
  List<PendingInvite> pendingInvites = [];
  final Map<String, List<Channel>> channelsByServer = {};
  final Map<String, int> serverMemberCounts = {};

  String? selectedServerHash;
  String? selectedChannelHash;

  final Map<String, List<Member>> membersByChannel = {};
  final Map<String, List<Message>> messagesByChannel = {};
  final Map<String, List<PresenceEntry>> presenceByChannel = {};
  final Map<String, ChannelLinkQuality> linkQualityByChannel = {};
  final Map<String, ChannelPermissions> permissionsByChannel = {};

  /// This reader's permissions on each server, gating the server-rail menu's
  /// Invite / Edit permissions items. Loaded in [init].
  final Map<String, ChannelPermissions> serverPermissionsByHash = {};

  /// Per-channel history paging. `true` in [hasMoreOlderByChannel] means an
  /// older page may still exist; [loadingOlderByChannel] guards against firing
  /// overlapping older-page fetches while one is in flight.
  final Map<String, bool> hasMoreOlderByChannel = {};
  final Map<String, bool> loadingOlderByChannel = {};

  /// The backend event socket's state, separate from mesh link quality: this
  /// says whether live updates are flowing. Optimistic default so a client
  /// that never opens the socket (widget tests) reads as connected.
  TcConnState connectionState = TcConnState.connected;

  /// Unread message count per channel, mirroring the unread field DMs carry.
  /// Loaded from GET /channels/unread, bumped live on WS messages for
  /// channels not on screen, and zeroed when a channel is selected.
  final Map<String, int> unreadByChannel = {};

  /// Per-channel sync state from the backend's SyncStatusTracker. "incomplete"
  /// means history is known to be missing -- including rows a peer served that
  /// we refused as unverifiable, which would otherwise be silent.
  final Map<String, String> syncStateByChannel = {};
  final Map<String, Uint8List?> avatarCache = {};

  final Map<String, List<VoiceParticipant>> voiceRosterByChannel = {};

  /// The live voice session, straight from GET /voice/status; idle when not
  /// in a call. Refreshed on session events and by [_voicePollTimer].
  VoiceStatus voiceStatus = VoiceStatus.idle;

  /// Optimistic local mute state; reconciled from the backend on each poll.
  bool voiceMuted = false;

  /// The session is up but the backend has no working audio pipeline -- we
  /// stay in the call, listening-only. A `voice_session: audio_error` event
  /// raises it the moment the pipeline fails; [refreshVoiceStatus] keeps it
  /// true from GET /voice/status thereafter, so a client that joined before
  /// this one connected (or missed the event on a dropped socket) still shows
  /// the state instead of a plain LIVE.
  bool voiceAudioError = false;
  Timer? _voicePollTimer;

  /// One-line headline for the voice panel's audio warning, empty when the
  /// pipeline is fully up. Distinguishes total failure from one direction
  /// down; the pipeline runs whichever of mic/speakers opened.
  String get voiceAudioWarning {
    if (voiceStatus.channel == null) return '';
    if (!voiceStatus.audioAvailable) return 'NO AUDIO — CAPTURE AND PLAYBACK DOWN';
    if (!voiceStatus.inputOk && !voiceStatus.outputOk) {
      return 'NO AUDIO — CAPTURE AND PLAYBACK DOWN';
    }
    if (!voiceStatus.inputOk) return 'MIC UNAVAILABLE — LISTENING ONLY';
    if (!voiceStatus.outputOk) return 'AUDIO OUTPUT UNAVAILABLE — MIC STILL LIVE';
    return '';
  }

  /// The backend's stated cause for the warning above (missing library,
  /// device open failure, ...), straight from GET /voice/status. Empty until
  /// the status refresh after an audio_error lands.
  String get voiceAudioReason {
    if (voiceStatus.audioReason.isNotEmpty) return voiceStatus.audioReason;
    if (!voiceStatus.inputOk && voiceStatus.inputError.isNotEmpty) {
      return voiceStatus.inputError;
    }
    if (!voiceStatus.outputOk && voiceStatus.outputError.isNotEmpty) {
      return voiceStatus.outputError;
    }
    return '';
  }

  /// Per-channel debounce for reaction refreshes. A sync backfill or a burst
  /// of reactions fires one event each; without coalescing that is one full
  /// message re-fetch per reaction.
  final Map<String, Timer> _reactionRefreshTimers = {};

  /// Channels whose permissions/sync-state preload is in flight, so
  /// [ensureChannelMeta] never launches a duplicate fetch for one.
  final Set<String> _channelMetaLoading = {};

  String? get voiceChannelHash => voiceStatus.channel;
  LinkQualityLevel get voiceQualityLevel => voiceOverallLevel(voiceStatus);

  /// Custom emoji library, keyed by emoji hash. Loaded lazily on first
  /// [ensureEmojiLoaded] and kept fresh on [EmojiReceivedEvent].
  final Map<String, CustomEmoji> customEmojis = {};
  bool _emojisLoaded = false;

  /// Locally saved contacts. Tab-only: never used as the display name in
  /// message bubbles or the presence roster (see friends_tab.dart).
  List<Friend> friends = [];

  /// Direct-message conversations, newest activity first. A conversation's
  /// messages live in [messagesByChannel] under its own hash, exactly like a
  /// channel's -- they are the same rows on the backend.
  List<DmConversation> dms = [];
  FriendRequests friendRequests = const FriendRequests.empty();
  PropagationStatus propagation = const PropagationStatus.none();

  /// The conversation being read, or null when a channel is selected. Set
  /// alongside [selectedChannelHash], which stays the address the message
  /// list renders either way.
  String? selectedDmHash;

  /// Peers heard via trenchchat.user announces, from the last [loadDirectory]
  /// query. Kept live on [DirectoryUpdatedEvent] so the invite picker reflects
  /// a peer's renamed self without a reload.
  List<DirectoryEntry> directory = [];

  /// Nomad Network nodes heard on the mesh, keyed by node destination hash.
  /// Loaded on first browse-tab open, kept live on [NomadNodeEvent].
  final Map<String, NomadNode> nomadNodes = {};

  /// Saved page bookmarks. Local-only, like [friends].
  List<NomadBookmark> nomadBookmarks = [];

  int _networkMapRevision = 0;

  /// Bumped on every [NetworkMapChangedEvent]. The map is expensive enough
  /// that nothing is fetched here: the MAP tab watches this counter and
  /// re-fetches only while it is on screen.
  int get networkMapRevision => _networkMapRevision;

  /// Live state of page/file fetches, keyed by fetch id and updated from
  /// [NomadFetchEvent]s. The browser tab watches its own fetch id here;
  /// terminal entries are removed once a consumer takes them.
  final Map<String, NomadFetchStatus> nomadFetches = {};

  /// The saved per-section color theme. Empty means every section renders
  /// stock; the shell resolves it per region via SectionTheme.
  ThemeSpec themeSpec = ThemeSpec.empty;

  /// Themes saved under a name, keyed by that name. Applying one is a local
  /// edit -- only [themeSpec] is what the app renders with.
  Map<String, ThemeSpec> themeLibrary = {};

  /// A theme the appearance editor handed to the compose box, waiting to be
  /// dropped into the draft. Nothing is sent until the user sends it.
  ({String name, String code})? pendingThemeShare;

  bool loading = true;
  String? error;

  /// Set by a failed mutating action (send, create, join, ...). Distinct from
  /// [error], which is fatal and takes over the whole screen on init failure --
  /// this one is transient and meant for a toast/snackbar, cleared on the next
  /// attempt. Single surface so every call site reports failures the same way.
  ///
  /// Read it with [takeActionError], never directly: main_window.dart shows
  /// whatever is left here in an app-wide snackbar, which is the right surface
  /// only for failures with no UI of their own.
  String? actionError;

  /// The last action error, claimed: taking it stops main_window.dart's
  /// app-wide snackbar from showing the same failure a second time.
  ///
  /// Every flow that renders the failure itself -- every dialog -- must read it
  /// this way. Nothing renders [actionError] directly, so no notification is
  /// needed to clear it.
  String? takeActionError() {
    final message = actionError;
    actionError = null;
    return message;
  }

  Channel? get selectedChannel {
    final hash = selectedChannelHash;
    return hash == null ? null : channelByHash(hash);
  }

  Channel? channelByHash(String hash) {
    for (final c in standaloneChannels) {
      if (c.hash == hash) return c;
    }
    for (final list in channelsByServer.values) {
      for (final c in list) {
        if (c.hash == hash) return c;
      }
    }
    return null;
  }

  Future<void> init() async {
    try {
      final me = await api.getMe();
      meHashHex = me['hash_hex'] as String;
      meDisplayName = me['display_name'] as String;

      await _reloadServersAndChannels();
      pendingInvites = await api.getInvites();

      selectedServerHash = servers.isNotEmpty ? servers.first.hash : null;
      final initialChannels = selectedServerHash != null
          ? channelsByServer[selectedServerHash] ?? []
          : standaloneChannels;
      selectedChannelHash = initialChannels.isNotEmpty
          ? initialChannels.first.hash
          : (standaloneChannels.isNotEmpty ? standaloneChannels.first.hash : null);

      if (selectedChannelHash != null) {
        await loadChannel(selectedChannelHash!);
      }

      await loadFriends();
      await loadFriendRequests();
      await loadDms();
      await loadPropagation();
      await loadVersion();
      await loadTheme();
      await loadThemeLibrary();
      await loadFileUsage();

      loading = false;
      notifyListeners();

      _socket.onReconnected = _onSocketReconnected;
      _socket.onConnStateChanged = _onConnStateChanged;
      _sub = _socket.events.listen(_onEvent);
      unawaited(ensureEmojiLoaded());
      // The backend session outlives client restarts; pick it up if live.
      await refreshVoiceStatus();
      if (voiceStatus.channel != null) _startVoicePoll();
    } catch (e) {
      error = e.toString();
      loading = false;
      notifyListeners();
    }
  }

  Future<void> loadChannel(String channelHashHex) async {
    try {
      final results = await Future.wait([
        api.getMembers(channelHashHex),
        api.getMessages(channelHashHex, limit: messagePageSize),
        api.getChannelPresence(channelHashHex),
        api.getChannelLinkQuality(channelHashHex),
        api.getMyPermissions(channelHashHex),
        api.getVoiceRoster(channelHashHex),
        api.getSyncState(channelHashHex),
      ]);
      membersByChannel[channelHashHex] = results[0] as List<Member>;
      final page = results[1] as List<Message>;
      messagesByChannel[channelHashHex] = page;
      hasMoreOlderByChannel[channelHashHex] = page.length >= messagePageSize;
      presenceByChannel[channelHashHex] = results[2] as List<PresenceEntry>;
      linkQualityByChannel[channelHashHex] = results[3] as ChannelLinkQuality;
      permissionsByChannel[channelHashHex] = results[4] as ChannelPermissions;
      voiceRosterByChannel[channelHashHex] = results[5] as List<VoiceParticipant>;
      syncStateByChannel[channelHashHex] = results[6] as String;
      notifyListeners();
    } catch (e) {
      _reportActionError(e);
    }
  }

  /// Loads a listed channel's permissions and sync state without opening it,
  /// so the row's context menu (Invite / Edit permissions) and sync badge are
  /// right before the channel has ever been visited. Idempotent and cheap:
  /// skips channels already loaded or already loading, and swallows failures
  /// (the menu simply hides perm-gated items until a later visit fills them).
  Future<void> ensureChannelMeta(Iterable<String> channelHashHexes) async {
    final missing = channelHashHexes
        .where((h) =>
            !permissionsByChannel.containsKey(h) && !_channelMetaLoading.contains(h))
        .toSet();
    if (missing.isEmpty) return;
    _channelMetaLoading.addAll(missing);
    await Future.wait(missing.map((h) async {
      try {
        final results = await Future.wait([
          api.getMyPermissions(h),
          api.getSyncState(h),
        ]);
        permissionsByChannel[h] = results[0] as ChannelPermissions;
        syncStateByChannel[h] = results[1] as String;
      } catch (_) {
        // Non-fatal: a later loadChannel fills these in.
      } finally {
        _channelMetaLoading.remove(h);
      }
    }));
    notifyListeners();
  }

  Future<void> selectServer(String serverHashHex) async {
    selectedServerHash = serverHashHex;
    final chans = channelsByServer[serverHashHex] ?? [];
    if (chans.isNotEmpty) {
      await selectChannel(chans.first.hash);
    } else {
      notifyListeners();
    }
  }

  Future<void> selectChannel(String channelHashHex) async {
    selectedChannelHash = channelHashHex;
    selectedDmHash = null;
    _clearUnread(channelHashHex);
    notifyListeners();
    if (!messagesByChannel.containsKey(channelHashHex)) {
      await loadChannel(channelHashHex);
    } else {
      // Refresh presence/link quality; message history is kept live via WS.
      unawaited(loadChannel(channelHashHex));
    }
  }

  Future<Uint8List?> avatarFor(String identityHashHex) async {
    if (avatarCache.containsKey(identityHashHex)) return avatarCache[identityHashHex];
    final data = await api.getPeerAvatar(identityHashHex);
    avatarCache[identityHashHex] = data;
    notifyListeners();
    return data;
  }

  /// Attachment bytes by `channelHash/messageId`. A cached null means the
  /// message's image was asked for and there was none to fetch, which stops
  /// the list refetching it on every rebuild.
  final Map<String, Uint8List?> attachmentCache = {};

  static String attachmentKey(String channelHashHex, String messageId) =>
      '$channelHashHex/$messageId';

  Future<Uint8List?> attachmentFor(String channelHashHex, String messageId) async {
    final key = attachmentKey(channelHashHex, messageId);
    if (attachmentCache.containsKey(key)) return attachmentCache[key];
    // Claim the key before awaiting so a row rebuilt mid-fetch does not start
    // a second request for the same image.
    attachmentCache[key] = null;
    final data = await api.getMessageImage(channelHashHex, messageId);
    attachmentCache[key] = data;
    notifyListeners();
    return data;
  }

  /// Refetches the server and channel lists (and each server's channels,
  /// member count and permissions) -- the same set the initial load builds.
  /// Shared by [init], reconnect resync, and [ChannelJoinedEvent].
  Future<void> _reloadServersAndChannels() async {
    servers = await api.getServers();
    standaloneChannels = await api.getChannels();
    for (final s in servers) {
      channelsByServer[s.hash] = await api.getServerChannels(s.hash);
      serverMemberCounts[s.hash] = (await api.getServerMembers(s.hash)).length;
      serverPermissionsByHash[s.hash] = await api.getMyServerPermissions(s.hash);
    }
    await refreshUnreadCounts();
  }

  /// Zeroes a channel's local unread and persists the read watermark, so the
  /// badge stays cleared across restarts. Fire-and-forget: a failed persist
  /// costs nothing but a stale badge on next launch.
  void _clearUnread(String channelHashHex) {
    unreadByChannel[channelHashHex] = 0;
    unawaited(api.markChannelRead(channelHashHex).catchError((_) => false));
  }

  /// Refetches per-channel unread counts. Non-fatal on failure (an older
  /// backend without the endpoint simply shows no badges), and the channel
  /// on screen always reads as caught up.
  Future<void> refreshUnreadCounts() async {
    try {
      unreadByChannel
        ..clear()
        ..addAll(await api.getChannelUnread());
      final selected = selectedChannelHash;
      if (selected != null) unreadByChannel[selected] = 0;
    } catch (_) {
      // Live WS bumps still maintain the in-session counts.
    }
  }

  /// Anything whose WS events may have been missed while the socket was down.
  /// The server/channel/friend lists are refetched too: a channel joined or a
  /// friend changed while the socket was down would otherwise stay stale until
  /// a full reload.
  void _onSocketReconnected() {
    unawaited(_resyncAfterReconnect());
  }

  Future<void> _resyncAfterReconnect() async {
    try {
      await _reloadServersAndChannels();
      notifyListeners();
    } catch (_) {
      // Non-fatal: the individual refreshes below still run, and a later
      // reconnect or reload catches the rest up.
    }
    final channelHash = selectedChannelHash;
    if (channelHash != null) unawaited(loadChannel(channelHash));
    unawaited(refreshInvites());
    unawaited(loadFriends());
    unawaited(refreshEmoji());
    unawaited(refreshVoiceStatus());
    if (nomadNodes.isNotEmpty) unawaited(loadNomadNodes());
  }

  Future<bool> sendMessage(String content,
      {String? replyTo,
      String? imageDataB64,
      String? fileName,
      String? fileDataB64}) async {
    final channelHashHex = selectedChannelHash;
    if (channelHashHex == null) return false;
    if (content.trim().isEmpty && imageDataB64 == null && fileDataB64 == null) {
      return false;
    }
    try {
      final result = await api.sendMessage(channelHashHex, content.trim(),
          replyTo: replyTo,
          imageDataB64: imageDataB64,
          fileName: fileName,
          fileDataB64: fileDataB64);
      if (result.ok) {
        // The WS event echoes it too; this covers a dropped socket so the
        // sender always sees their own message land.
        unawaited(refreshMessages(channelHashHex));
        return true;
      }
      actionError = sendRefusalMessage(result.reason);
      notifyListeners();
      return false;
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  /// Shares a picked file: the bytes are stored here and the message carries
  /// only the manifest, so nothing is pushed to anyone.
  Future<bool> shareFile(String name, Uint8List bytes,
          {String content = '', String? replyTo}) =>
      sendMessage(content,
          replyTo: replyTo, fileName: name, fileDataB64: base64Encode(bytes));

  /// Starts (or joins) the download of the file a message names. The card
  /// moves on the file_fetch events that follow; the snapshot this returns is
  /// applied straight away so a dropped socket still shows the state change.
  Future<bool> fetchFile(
      String channelHashHex, String fileHash, String messageId) async {
    try {
      final fetch = await api.fetchFile(channelHashHex, fileHash, messageId);
      if (fetch == null) {
        actionError = 'That file is no longer available here.';
        notifyListeners();
        return false;
      }
      _applyFileFetch(fetch.fileHash, fetch.messageIds, fetch.channels,
          fetch.state, fetch.progress, fetch.reason);
      return true;
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  /// Reads a held file back over the API and hands it to the save dialog. The
  /// bytes live in the backend's database, which may be on another machine,
  /// so they make the round trip rather than being read off local disk.
  Future<bool> saveFile(
      String channelHashHex, String fileHash, String fileName) async {
    final bytes = await api.getFileBytes(channelHashHex, fileHash);
    if (bytes == null) {
      actionError = 'That file is not here yet.';
      notifyListeners();
      return false;
    }
    try {
      await _saveFileBytes(fileName, bytes, guessMimeType(fileName));
      return true;
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  /// The largest file this backend will share, from GET /files/usage. The
  /// client's own default holds until it answers.
  int maxFileBytes = maxFileAttachmentBytes;

  /// Whether this reader may attach a file in the channel on screen. The
  /// client gate only: actions.share_file re-checks it, and the inbound
  /// handler drops a manifest from a peer without it.
  bool get canShareFiles {
    final hash = selectedChannelHash;
    if (hash == null || selectedDmHash != null) return false;
    return permissionsByChannel[hash]?.shareFiles ?? false;
  }

  Future<void> loadFileUsage() async {
    try {
      maxFileBytes = (await api.fileUsage()).maxFileBytes;
      notifyListeners();
    } catch (_) {
      // An older backend without the endpoint keeps the client's own ceiling.
    }
  }

  /// Moves every card showing this file to the state the download reached.
  /// Progress is chunks verified, so it never goes backwards; a state that
  /// arrives out of order cannot pull a bar back either.
  void _applyFileFetch(String fileHash, List<String> messageIds,
      List<String> channels, String state, double progress, String? reason) {
    var changed = false;
    final scope = channels.isEmpty ? messagesByChannel.keys : channels;
    for (final channelHash in scope) {
      final list = messagesByChannel[channelHash];
      if (list == null) continue;
      for (var i = 0; i < list.length; i++) {
        final file = list[i].file;
        if (file == null || file.hash != fileHash) continue;
        if (messageIds.isNotEmpty && !messageIds.contains(list[i].messageId)) {
          continue;
        }
        final next = progress < file.progress && state != fileStateDone
            ? file.progress
            : progress;
        list[i] = list[i].withFile(file.withFetch(state, next, reason));
        changed = true;
      }
    }
    if (changed) notifyListeners();
  }

  Future<void> refreshMessages(String channelHashHex) async {
    try {
      // Cover everything already on screen, not just the newest page, so a
      // live refresh never drops older pages the reader scrolled in.
      final loaded = messagesByChannel[channelHashHex]?.length ?? 0;
      final limit = loaded > messagePageSize ? loaded : messagePageSize;
      messagesByChannel[channelHashHex] =
          await api.getMessages(channelHashHex, limit: limit);
      notifyListeners();
    } catch (_) {
      // Next WS event or channel reload will catch it up.
    }
  }

  bool hasMoreOlder(String channelHashHex) => hasMoreOlderByChannel[channelHashHex] ?? false;
  bool loadingOlder(String channelHashHex) => loadingOlderByChannel[channelHashHex] ?? false;

  /// Fetches the next older page for [channelHashHex] and prepends it. No-op
  /// when a fetch is already running or the start of history is reached.
  Future<void> loadOlderMessages(String channelHashHex) async {
    if (loadingOlder(channelHashHex) || !hasMoreOlder(channelHashHex)) return;
    final current = messagesByChannel[channelHashHex] ?? const [];
    if (current.isEmpty) return;
    final oldestTs = current.map((m) => m.timestamp).reduce((a, b) => a < b ? a : b);

    loadingOlderByChannel[channelHashHex] = true;
    notifyListeners();
    try {
      final older = await api.getMessages(channelHashHex,
          limit: messagePageSize, beforeTs: oldestTs);
      final known = current.map((m) => m.messageId).toSet();
      final fresh = older.where((m) => !known.contains(m.messageId)).toList();
      if (fresh.isNotEmpty) {
        messagesByChannel[channelHashHex] = [...fresh, ...current];
      }
      hasMoreOlderByChannel[channelHashHex] = older.length >= messagePageSize;
    } catch (e) {
      _reportActionError(e);
    } finally {
      loadingOlderByChannel[channelHashHex] = false;
      notifyListeners();
    }
  }

  void _onConnStateChanged(TcConnState state) {
    connectionState = state;
    notifyListeners();
  }

  /// Standalone channels announced on the mesh but not yet joined. Refreshed
  /// on demand (join dialog open) and live on [ChannelDiscoveredEvent].
  Future<void> refreshDiscoveredChannels() async {
    try {
      discoveredChannels = await api.getDiscoveredChannels();
      notifyListeners();
    } catch (e) {
      _reportActionError(e);
    }
  }

  /// Discovered channels the Join dialog can actually offer: open-join and not
  /// already joined. Invite-only channels need an invite, so listing one here
  /// is a dead end -- a channel the user already left surfaces in discovery as
  /// an invite-only local row and can never be re-joined this way.
  List<Channel> get joinableDiscoveredChannels {
    final joined = standaloneChannels.map((c) => c.hash).toSet();
    return discoveredChannels
        .where((c) => c.openJoin && !joined.contains(c.hash))
        .toList();
  }

  /// Returns the new server's hash, or null (with [actionError] set) on failure.
  Future<String?> createServer(String name, String description) async {
    try {
      final hash = await api.createServer(name, description);
      servers = await api.getServers();
      channelsByServer.putIfAbsent(hash, () => []);
      serverMemberCounts[hash] = (await api.getServerMembers(hash)).length;
      notifyListeners();
      return hash;
    } catch (e) {
      _reportActionError(e);
      return null;
    }
  }

  /// Creates a channel inside [serverHashHex], inheriting the server's
  /// permissions. Returns the new channel's hash, or null (with
  /// [actionError] set -- e.g. missing create_channel permission) on failure.
  Future<String?> createChannelInServer(
      String serverHashHex, String name, String description) async {
    try {
      final hash = await api.createServerChannel(serverHashHex, name, description);
      channelsByServer[serverHashHex] = await api.getServerChannels(serverHashHex);
      selectedServerHash = serverHashHex;
      notifyListeners();
      await selectChannel(hash);
      return hash;
    } catch (e) {
      _reportActionError(e);
      return null;
    }
  }

  /// Creates a standalone channel with [access] `"public"` or `"invite"`.
  /// Returns the new channel's hash, or null (with [actionError] set) on failure.
  Future<String?> createStandaloneChannel(
      String name, String description, String access) async {
    try {
      final hash = await api.createChannel(name, description, access);
      standaloneChannels = await api.getChannels();
      notifyListeners();
      await selectChannel(hash);
      return hash;
    } catch (e) {
      _reportActionError(e);
      return null;
    }
  }

  /// Joins a previously-discovered standalone public channel.
  Future<bool> joinChannel(String channelHashHex) async {
    try {
      final ok = await api.joinChannel(channelHashHex);
      if (ok) {
        standaloneChannels = await api.getChannels();
        discoveredChannels = discoveredChannels.where((c) => c.hash != channelHashHex).toList();
        notifyListeners();
        await selectChannel(channelHashHex);
      }
      return ok;
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  /// Leaves a standalone channel: unsubscribes and drops it from the list,
  /// selecting whatever is left when it was the open one. Stored history is
  /// kept, so re-joining shows it again.
  Future<bool> leaveChannel(String channelHashHex) async {
    try {
      final ok = await api.leaveChannel(channelHashHex);
      if (!ok) return false;
      // Leaving the channel also ends its voice call, rather than stranding a
      // live session whose channel is gone in the voice panel.
      if (voiceStatus.channel == channelHashHex) {
        await leaveVoice();
      }
      standaloneChannels = await api.getChannels();
      if (selectedChannelHash == channelHashHex) {
        // The channel left is always standalone, so prefer another standalone
        // channel; fall back to the selected server's channels, then to the
        // empty/home state. Never keep a hash whose channel is gone, which
        // would leave a bare header with compose still enabled.
        final serverChannels =
            selectedServerHash != null ? channelsByServer[selectedServerHash] : null;
        final next = standaloneChannels.isNotEmpty
            ? standaloneChannels.first.hash
            : (serverChannels != null && serverChannels.isNotEmpty
                ? serverChannels.first.hash
                : null);
        selectedChannelHash = next;
        notifyListeners();
        if (next != null) await loadChannel(next);
        return true;
      }
      notifyListeners();
      return true;
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  /// Deselects the current server and returns to the plain DIRECT CHANNELS
  /// view, selecting the first direct channel if there is one.
  Future<void> selectHome() async {
    selectedServerHash = null;
    final next = standaloneChannels.isNotEmpty ? standaloneChannels.first.hash : null;
    selectedChannelHash = next;
    notifyListeners();
    if (next != null && !messagesByChannel.containsKey(next)) {
      await loadChannel(next);
    }
  }

  /// Leaves a server: drops the membership and removes it from the rail. When
  /// it was the selected server, falls back to the DIRECT CHANNELS view.
  Future<bool> leaveServer(String serverHashHex) async {
    try {
      final ok = await api.leaveServer(serverHashHex);
      if (!ok) return false;
      servers = servers.where((s) => s.hash != serverHashHex).toList();
      channelsByServer.remove(serverHashHex);
      serverMemberCounts.remove(serverHashHex);
      serverPermissionsByHash.remove(serverHashHex);
      if (selectedServerHash == serverHashHex) {
        await selectHome();
      } else {
        notifyListeners();
      }
      return true;
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  /// Sends an invite to a server. Returns true on success; on failure
  /// [actionError] is set.
  Future<bool> inviteToServer(String serverHashHex, String peerHashHex) async {
    try {
      await api.inviteToServer(serverHashHex, peerHashHex);
      return true;
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  /// Replaces a server's role-permission matrix. Returns false when the
  /// backend's MANAGE_CHANNEL gate dropped the change.
  Future<bool> updateServerPermissions(
      String serverHashHex, List<String> admin, List<String> member) async {
    try {
      return await api.updateServerPermissions(serverHashHex, admin, member);
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  /// Joins [channelHashHex]'s voice session. Returns false (with
  /// [actionError] set) when the backend refused the join.
  Future<bool> joinVoice(String channelHashHex) async {
    try {
      final ok = await api.joinVoice(channelHashHex);
      if (ok) {
        await refreshVoiceStatus();
        await refreshVoiceRoster(channelHashHex);
        _startVoicePoll();
        return true;
      }
      // The backend gives no machine-readable reason yet.
      actionError =
          "Couldn't join voice — no permission, already in a call, or the room is full.";
      notifyListeners();
      return false;
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  Future<bool> leaveVoice() async {
    final oldChannel = voiceStatus.channel;
    try {
      final ok = await api.leaveVoice();
      _stopVoicePoll();
      voiceStatus = VoiceStatus.idle;
      voiceAudioError = false;
      notifyListeners();
      if (oldChannel != null) unawaited(refreshVoiceRoster(oldChannel));
      return ok;
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  /// Optimistic: flips the local state immediately, reverts on failure.
  Future<bool> toggleVoiceMute() async {
    final target = !voiceMuted;
    voiceMuted = target;
    notifyListeners();
    try {
      await api.setVoiceMuted(target);
      return true;
    } catch (e) {
      voiceMuted = !target;
      _reportActionError(e);
      return false;
    }
  }

  Future<void> refreshVoiceRoster(String channelHashHex) async {
    try {
      voiceRosterByChannel[channelHashHex] = await api.getVoiceRoster(channelHashHex);
      notifyListeners();
    } catch (_) {
      // Next WS event or poll tick will catch it up.
    }
  }

  Future<void> refreshVoiceStatus() async {
    try {
      voiceStatus = await api.getVoiceStatus();
      voiceMuted = voiceStatus.muted;
      // Only meaningful inside a session: with no call up there is no pipeline
      // to run, and the backend reports that as unavailable too.
      voiceAudioError = voiceStatus.channel != null &&
          (!voiceStatus.audioAvailable ||
              !voiceStatus.inputOk ||
              !voiceStatus.outputOk);
      notifyListeners();
    } catch (_) {
      // Next poll tick will catch it up.
    }
  }

  /// Quality isn't pushed over WS, so poll /voice/status while in a session;
  /// the roster refresh also self-heals any missed voice_roster event.
  void _startVoicePoll() {
    _voicePollTimer ??= Timer.periodic(const Duration(seconds: 4), (_) {
      unawaited(refreshVoiceStatus());
      final channelHash = voiceStatus.channel;
      if (channelHash != null) unawaited(refreshVoiceRoster(channelHash));
    });
  }

  void _stopVoicePoll() {
    _voicePollTimer?.cancel();
    _voicePollTimer = null;
  }

  Future<void> refreshInvites() async {
    try {
      pendingInvites = await api.getInvites();
      notifyListeners();
    } catch (e) {
      _reportActionError(e);
    }
  }

  Future<void> loadFriends() async {
    try {
      friends = await api.getFriends();
      notifyListeners();
    } catch (e) {
      _reportActionError(e);
    }
  }

  Future<void> loadNomadNodes() async {
    try {
      final nodes = await api.getNomadNodes();
      nomadNodes
        ..clear()
        ..addEntries(nodes.map((n) => MapEntry(n.nodeHash, n)));
      notifyListeners();
    } catch (e) {
      _reportActionError(e);
    }
  }

  // --- friend requests ---

  Future<void> loadFriendRequests() async {
    try {
      friendRequests = await api.getFriendRequests();
      notifyListeners();
    } catch (e) {
      _reportActionError(e);
    }
  }

  Future<void> loadNomadBookmarks() async {
    try {
      nomadBookmarks = await api.getNomadBookmarks();
      notifyListeners();
    } catch (e) {
      _reportActionError(e);
    }
  }

  /// Opens a nomad URL, carrying the request data a page's input fields
  /// produced. Returns where it resolved to and the fetch id to watch in
  /// [nomadFetches] -- null when the backend answered from a cache the page
  /// itself declared, so there is nothing to wait for. Null overall means
  /// the URL was rejected.
  Future<({String? fetchId, String nodeHash, String path, bool cached})?>
      browseNomad(String url,
          {String? currentNode,
          Map<String, String>? data,
          bool refresh = false}) async {
    try {
      final result = await api.browseNomad(url,
          currentNode: currentNode, data: data, refresh: refresh);
      if (!result.ok) return null;
      final location = (
        fetchId: result.fetchId,
        nodeHash: result.nodeHash ?? '',
        path: result.path ?? '/page/index.mu',
        cached: result.cached,
      );
      if (result.fetchId == null) return location;
      // On a warm link the WS done event can beat this continuation; an
      // entry already present is fresher than "queued" and must survive.
      nomadFetches.putIfAbsent(
          result.fetchId!,
          () => NomadFetchStatus(
                nodeHash: location.nodeHash,
                path: location.path,
                status: 'queued',
                progress: 0,
              ));
      notifyListeners();
      return location;
    } catch (e) {
      _reportActionError(e);
      return null;
    }
  }

  /// Asks the backend how a fetch ended and applies the answer exactly as a
  /// [NomadFetchEvent] would. Fetch events are published over a socket that
  /// can drop while a page is in flight, and nothing replays them, so the
  /// browser polls this rather than waiting forever for an event that is
  /// already gone.
  Future<void> pollNomadFetch(String fetchId) async {
    try {
      final status = await api.getNomadFetch(fetchId);
      if (status == null) {
        // The backend no longer knows this fetch: report it terminally so the
        // browser stops waiting and can fall back to whatever it has cached.
        nomadFetches[fetchId] = NomadFetchStatus(
          nodeHash: nomadFetches[fetchId]?.nodeHash ?? '',
          path: nomadFetches[fetchId]?.path ?? '',
          status: 'failed',
          progress: 0,
          reason: 'forgotten',
        );
      } else {
        nomadFetches[fetchId] = status;
      }
      notifyListeners();
    } catch (_) {
      // A failed poll is not an outcome; the next tick tries again.
    }
  }

  /// Fetches one micron page and waits for it, returning its source.
  ///
  /// The browser's own navigation is event-driven, but a `` `{...} `` partial
  /// is a fetch nothing on screen is waiting on, so this is the one place
  /// that blocks until an answer arrives. refresh=true because the interval
  /// a partial declares is the author's, not the page cache's.
  Future<String?> loadNomadPartial(String url,
      {String? currentNode,
      Map<String, String>? data,
      Duration interval = const Duration(seconds: 1)}) async {
    final target = await browseNomad(url,
        currentNode: currentNode, data: data, refresh: true);
    if (target == null) return null;
    final fetchId = target.fetchId;
    if (fetchId != null) {
      final status = await awaitNomadFetch(fetchId, interval: interval);
      if (status?.status != 'done') return null;
    }
    final page = await fetchCachedNomadPage(target.nodeHash, target.path);
    return page?.source;
  }

  /// Waits for a fetch to end, polling when no event arrives. Null once the
  /// timeout passes. Either way the fetch's entry is taken, so a partial on
  /// a refresh timer cannot fill [nomadFetches] with its own history.
  Future<NomadFetchStatus?> awaitNomadFetch(String fetchId,
      {Duration timeout = const Duration(seconds: 90),
      Duration interval = const Duration(seconds: 1)}) async {
    final deadline = DateTime.now().add(timeout);
    while (DateTime.now().isBefore(deadline)) {
      final status = nomadFetches[fetchId];
      if (status != null && status.isTerminal) return takeNomadFetch(fetchId);
      await Future<void>.delayed(interval);
      await pollNomadFetch(fetchId);
    }
    takeNomadFetch(fetchId);
    return null;
  }

  /// Identify state per node, loaded when a node is opened. Absent means
  /// "not asked yet", which the UI shows as anonymous.
  final Map<String, NomadIdentify> nomadIdentify = {};

  /// Where the browser has been, and where in that it currently sits.
  ///
  /// Held here rather than in the NET tab's own State because switching to
  /// another tab unmounts that widget: keeping the trail in the tab meant
  /// every trip to CHAT and back landed on the node list again.
  final List<({String nodeHash, String path})> nomadHistory = [];
  int nomadHistoryIndex = -1;

  ({String nodeHash, String path})? get nomadLocation =>
      nomadHistoryIndex >= 0 && nomadHistoryIndex < nomadHistory.length
          ? nomadHistory[nomadHistoryIndex]
          : null;

  /// Records a visit, dropping any forward history as a browser does.
  void pushNomadLocation(String nodeHash, String path) {
    final current = nomadLocation;
    if (current != null &&
        current.nodeHash == nodeHash &&
        current.path == path) {
      return;
    }
    nomadHistory.removeRange(nomadHistoryIndex + 1, nomadHistory.length);
    nomadHistory.add((nodeHash: nodeHash, path: path));
    nomadHistoryIndex = nomadHistory.length - 1;
  }

  /// Moves back or forward, returning where that lands. Null when there is
  /// nothing that way.
  ({String nodeHash, String path})? stepNomadHistory(int delta) {
    final target = nomadHistoryIndex + delta;
    if (target < 0 || target >= nomadHistory.length) return null;
    nomadHistoryIndex = target;
    return nomadHistory[target];
  }

  void clearNomadHistory() {
    nomadHistory.clear();
    nomadHistoryIndex = -1;
  }

  Future<NomadIdentify?> loadNomadIdentify(String nodeHash) async {
    try {
      final status = await api.getNomadIdentify(nodeHash);
      nomadIdentify[nodeHash] = status;
      notifyListeners();
      return status;
    } catch (e) {
      _reportActionError(e);
      return null;
    }
  }

  /// Turns identifying to one node on or off. Opt-in per node: this is the
  /// only path that ever reveals our identity to a node operator.
  Future<NomadIdentify?> setNomadIdentify(String nodeHash, bool enabled) async {
    try {
      final status = await api.setNomadIdentify(nodeHash, enabled);
      nomadIdentify[nodeHash] = status;
      notifyListeners();
      return status;
    } catch (e) {
      _reportActionError(e);
      return null;
    }
  }

  Future<NomadPage?> fetchCachedNomadPage(String nodeHash, String path) async {
    try {
      return await api.getNomadPage(nodeHash, path);
    } catch (e) {
      _reportActionError(e);
      return null;
    }
  }

  /// Saves a contact from the LXMF address a bot or foreign client
  /// advertises. Null means the call failed; otherwise 'added' or
  /// 'resolving' -- an address only becomes a contact once the announce
  /// behind it has been heard.
  Future<String?> addLxmfAddress(String lxmfHash,
      {String nickname = '', String note = ''}) async {
    try {
      final state =
          await api.addLxmfAddress(lxmfHash, nickname: nickname, note: note);
      await loadFriends();
      return state;
    } catch (e) {
      _reportActionError(e);
      return null;
    }
  }

  /// Asks a peer to add us. Both sides have to hold the other before a direct
  /// message passes either way, so this is the start of that, not the end.
  Future<bool> sendFriendRequest(String identityHashHex,
      {String note = '', String nickname = ''}) async {
    try {
      final ok = await api.sendFriendRequest(identityHashHex,
          note: note, nickname: nickname);
      if (!ok) {
        actionError = 'That identity hash is not valid.';
        notifyListeners();
        return false;
      }
      await Future.wait([loadFriends(), loadFriendRequests()]);
      return true;
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  Future<bool> acceptFriendRequest(String identityHashHex,
      {String nickname = ''}) async {
    try {
      final ok = await api.acceptFriendRequest(identityHashHex, nickname: nickname);
      await Future.wait([loadFriends(), loadFriendRequests()]);
      return ok;
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  Future<bool> declineFriendRequest(String identityHashHex) async {
    try {
      final ok = await api.declineFriendRequest(identityHashHex);
      await loadFriendRequests();
      return ok;
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  Future<bool> cancelFriendRequest(String identityHashHex) async {
    try {
      final ok = await api.cancelFriendRequest(identityHashHex);
      await loadFriendRequests();
      return ok;
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  // --- direct messages ---

  Future<void> loadDms() async {
    try {
      dms = await api.getDms();
      notifyListeners();
    } catch (e) {
      _reportActionError(e);
    }
  }

  /// Opens (creating if needed) the conversation with a peer and selects it.
  /// Returns its hash, or null when they are not an accepted friend.
  Future<String?> openDm(String peerHashHex) async {
    try {
      final hash = await api.openDm(peerHashHex);
      await loadDms();
      await selectDm(hash);
      return hash;
    } catch (e) {
      _reportActionError(e);
      return null;
    }
  }

  /// Renames a bookmark. The backend upserts on (node, path), so saving the
  /// same location with a new label is the rename.
  Future<void> renameNomadBookmark(
      String nodeHash, String path, String label) async {
    try {
      await api.addNomadBookmark(nodeHash, path, label);
      await loadNomadBookmarks();
    } catch (e) {
      _reportActionError(e);
    }
  }

  bool isNomadBookmarked(String nodeHash, String path) => nomadBookmarks
      .any((b) => b.nodeHash == nodeHash && b.path == path);

  Future<void> toggleNomadBookmark(
      String nodeHash, String path, String label) async {
    try {
      if (isNomadBookmarked(nodeHash, path)) {
        await api.removeNomadBookmark(nodeHash, path);
      } else {
        await api.addNomadBookmark(nodeHash, path, label);
      }
      await loadNomadBookmarks();
    } catch (e) {
      _reportActionError(e);
    }
  }

  Future<void> selectDm(String conversationHashHex) async {
    selectedDmHash = conversationHashHex;
    selectedChannelHash = conversationHashHex;
    notifyListeners();
    if (!messagesByChannel.containsKey(conversationHashHex)) {
      await refreshMessages(conversationHashHex);
    } else {
      unawaited(refreshMessages(conversationHashHex));
    }
    await markDmRead(conversationHashHex);
  }

  Future<void> markDmRead(String conversationHashHex) async {
    try {
      await api.markDmRead(conversationHashHex);
      await loadDms();
    } catch (e) {
      _reportActionError(e);
    }
  }

  /// The browser tab claims a finished fetch here; removing it keeps
  /// [nomadFetches] from accumulating terminal entries.
  NomadFetchStatus? takeNomadFetch(String fetchId) {
    final status = nomadFetches[fetchId];
    if (status != null && status.isTerminal) {
      nomadFetches.remove(fetchId);
    }
    return status;
  }

  /// A nomad URL waiting for the browser tab to open it -- set when a link
  /// is tapped outside the tab (a chat message). Claimed once with
  /// [takePendingNomadUrl], the same take-style contract as [actionError].
  String? pendingNomadUrl;

  void openNomadUrl(String url) {
    pendingNomadUrl = url;
    notifyListeners();
  }

  String? takePendingNomadUrl() {
    final url = pendingNomadUrl;
    pendingNomadUrl = null;
    return url;
  }

  /// The peer on the other side of a conversation, or null if it isn't one.
  String? dmPeerFor(String conversationHashHex) {
    for (final d in dms) {
      if (d.hash == conversationHashHex) return d.peerHash;
    }
    return null;
  }

  DmConversation? dmFor(String conversationHashHex) {
    for (final d in dms) {
      if (d.hash == conversationHashHex) return d;
    }
    return null;
  }

  Future<bool> sendDirectMessage(String content,
      {String? replyTo, Uint8List? imageData}) async {
    final conversation = selectedDmHash;
    if (conversation == null) return false;
    final peer = dmPeerFor(conversation);
    if (peer == null) return false;
    if (content.trim().isEmpty && imageData == null) return false;
    try {
      await api.sendDm(peer, content.trim(), replyTo: replyTo, imageData: imageData);
      unawaited(refreshMessages(conversation));
      unawaited(loadDms());
      return true;
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  Future<bool> deleteDm(String conversationHashHex) async {
    try {
      final ok = await api.deleteDm(conversationHashHex);
      messagesByChannel.remove(conversationHashHex);
      if (selectedDmHash == conversationHashHex) {
        selectedDmHash = null;
        selectedChannelHash = standaloneChannels.isNotEmpty
            ? standaloneChannels.first.hash
            : null;
      }
      await loadDms();
      return ok;
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  // --- propagation node ---

  Future<void> loadPropagation() async {
    try {
      propagation = await api.getPropagation();
      notifyListeners();
    } catch (e) {
      _reportActionError(e);
    }
  }

  /// Pass an empty hash to go back to automatic selection.
  Future<bool> pinPropagationNode(String nodeHashHex) async {
    try {
      final ok = await api.pinPropagationNode(nodeHashHex);
      if (!ok) {
        actionError = 'That node address is not valid.';
        notifyListeners();
        return false;
      }
      await loadPropagation();
      return true;
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  /// Collects anything a propagation node is holding for us. Propagated
  /// messages are pulled, never pushed, so nothing arrives without this.
  Future<bool> collectPropagated() async {
    try {
      final ok = await api.collectPropagated();
      await loadPropagation();
      return ok;
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  /// Runs a directory search and caches the result in [directory] so live
  /// [DirectoryUpdatedEvent]s can patch it in place. [scope] narrows the
  /// result to friends or peers sharing a channel with this node.
  Future<void> loadDirectory(String query,
      {String scope = directoryScopeAll}) async {
    try {
      directory = await api.searchDirectory(query, scope: scope);
    } catch (_) {
      // Directory unavailable is not fatal; the manual-hash path still works.
      directory = [];
    }
    notifyListeners();
  }

  /// Best-effort display name for a peer, resolved across the directory,
  /// friends, presence rosters, and recent message authors. Returns null when
  /// nothing but the raw hash is known. Read-only; callers own the fallback.
  String? resolvePeerName(String identityHashHex) {
    for (final e in directory) {
      if (e.identityHash == identityHashHex && e.displayName.isNotEmpty) {
        return e.displayName;
      }
    }
    for (final f in friends) {
      if (f.identityHash == identityHashHex && f.displayName.isNotEmpty) {
        return f.displayName;
      }
    }
    for (final list in presenceByChannel.values) {
      for (final p in list) {
        if (p.identityHash == identityHashHex && (p.displayName?.isNotEmpty ?? false)) {
          return p.displayName;
        }
      }
    }
    for (final list in messagesByChannel.values) {
      for (final m in list) {
        if (m.senderHash == identityHashHex && m.senderName.isNotEmpty) {
          return m.senderName;
        }
      }
    }
    return null;
  }

  /// Accepts a pending invite and joins its channel/server. Returns true on
  /// success; on failure [actionError] is set.
  Future<bool> acceptInvite(String channelHashHex) async {
    try {
      final ok = await api.acceptInvite(channelHashHex);
      pendingInvites =
          pendingInvites.where((i) => i.channelHashHex != channelHashHex).toList();
      if (ok) {
        // The accepted scope may be a server or a standalone channel; refresh
        // both lists rather than guessing from scope_kind.
        servers = await api.getServers();
        standaloneChannels = await api.getChannels();
        for (final s in servers) {
          channelsByServer[s.hash] ??= await api.getServerChannels(s.hash);
        }
      }
      notifyListeners();
      return ok;
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  /// Returns true on success, or false (with [actionError] set) on failure.
  Future<bool> addFriend(String identityHashHex, String nickname, String note) async {
    try {
      final ok = await api.addFriend(identityHashHex, nickname, note);
      if (ok) await loadFriends();
      return ok;
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  Future<void> declineInvite(String channelHashHex) async {
    try {
      await api.declineInvite(channelHashHex);
      pendingInvites =
          pendingInvites.where((i) => i.channelHashHex != channelHashHex).toList();
      notifyListeners();
    } catch (e) {
      _reportActionError(e);
    }
  }

  /// Sends an invite for the channel. Returns true on success; on failure
  /// [actionError] is set.
  Future<bool> inviteToChannel(String channelHashHex, String peerHashHex) async {
    try {
      await api.inviteToChannel(channelHashHex, peerHashHex);
      return true;
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  /// Kick/promote/demote, then refresh the member list. Returns false when
  /// the backend's permission gate dropped the request.
  Future<bool> updateChannelRoles(
    String channelHashHex, {
    List<String> removeMembers = const [],
    List<String> addAdmins = const [],
    List<String> removeAdmins = const [],
  }) async {
    try {
      final ok = await api.updateChannelRoles(
        channelHashHex,
        removeMembers: removeMembers,
        addAdmins: addAdmins,
        removeAdmins: removeAdmins,
      );
      membersByChannel[channelHashHex] = await api.getMembers(channelHashHex);
      notifyListeners();
      return ok;
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  /// Partial update: omit [nickname] or [note] to leave it unchanged.
  Future<bool> updateFriend(String identityHashHex, {String? nickname, String? note}) async {
    try {
      final ok = await api.updateFriend(identityHashHex, nickname: nickname, note: note);
      if (ok) await loadFriends();
      return ok;
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  /// Replaces the role-permission matrix. Returns false when the backend's
  /// MANAGE_CHANNEL gate dropped the change.
  Future<bool> updateChannelPermissions(
      String channelHashHex, List<String> admin, List<String> member) async {
    try {
      return await api.updateChannelPermissions(channelHashHex, admin, member);
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  /// Loads the custom emoji library once; safe to call from build paths.
  Future<void> ensureEmojiLoaded() async {
    if (_emojisLoaded) return;
    _emojisLoaded = true;
    await refreshEmoji();
  }

  Future<void> refreshEmoji() async {
    try {
      final list = await api.getEmoji();
      customEmojis
        ..clear()
        ..addEntries(list.map((e) => MapEntry(e.emojiHash, e)));
      notifyListeners();
    } catch (_) {
      // A missing emoji library is cosmetic; chips fall back to hash text.
    }
  }

  /// Imports a custom emoji. Returns true on success; on failure
  /// [actionError] is set.
  Future<bool> importEmoji(String name, String imageDataB64) async {
    try {
      await api.importEmoji(name, imageDataB64);
      await refreshEmoji();
      return true;
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  /// Never fatal: the version is a note to the user, not something the client
  /// needs in order to run.
  Future<void> loadVersion() async {
    try {
      appVersion = await api.getVersion();
      notifyListeners();
    } catch (_) {
      appVersion = AppVersionInfo.unknown;
    }
  }

  /// Loads the saved per-section theme. Non-fatal by design: a backend that
  /// cannot serve one leaves the stock theme in place rather than blocking
  /// startup.
  Future<void> loadTheme() async {
    try {
      themeSpec = ThemeSpec.fromJson(await api.getUiTheme());
      notifyListeners();
    } catch (_) {
      themeSpec = ThemeSpec.empty;
    }
  }

  /// Saves [spec] and adopts it, rebuilding every themed section. On failure
  /// the previous theme stays in force and [actionError] carries the reason.
  Future<void> saveTheme(ThemeSpec spec) async {
    try {
      await api.setUiTheme(spec.toJson());
      themeSpec = spec;
      notifyListeners();
    } catch (e) {
      _reportActionError(e);
    }
  }

  /// Loads the named theme library, non-fatal on the same terms as
  /// [loadTheme]: a backend that cannot serve one leaves the library empty.
  Future<void> loadThemeLibrary() async {
    try {
      final themes = await api.getThemeLibrary();
      themeLibrary = {
        for (final entry in themes.entries)
          if (entry.value is Map<String, dynamic>)
            entry.key: ThemeSpec.fromJson(entry.value as Map<String, dynamic>),
      };
      notifyListeners();
    } catch (_) {
      themeLibrary = {};
    }
  }

  /// Saves [spec] under [name], replacing any theme already saved there.
  /// Returns true on success; on failure [actionError] carries the reason and
  /// the library is left as it was.
  Future<bool> saveThemeAs(String name, ThemeSpec spec) async {
    try {
      await api.saveThemeToLibrary(name, spec.toJson());
      themeLibrary = {...themeLibrary, name: spec};
      notifyListeners();
      return true;
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  /// Offers [code] to the compose box under [name], where it lands in the
  /// draft as a short token the user can still delete before sending.
  void stageThemeShare(String name, String code) {
    pendingThemeShare = (name: name, code: code);
    notifyListeners();
  }

  /// Takes the staged share and clears it. Deliberately silent: it is read
  /// while the compose box is already building.
  ({String name, String code})? consumePendingThemeShare() {
    final staged = pendingThemeShare;
    pendingThemeShare = null;
    return staged;
  }

  /// Removes a saved theme. Returns true on success; on failure [actionError]
  /// carries the reason.
  Future<bool> deleteSavedTheme(String name) async {
    try {
      await api.deleteThemeFromLibrary(name);
      themeLibrary = {...themeLibrary}..remove(name);
      notifyListeners();
      return true;
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  /// Saves the propagation-node settings. Returns true on success; on
  /// failure [actionError] is set.
  Future<bool> saveSettings(TcSettings settings) async {
    try {
      await api.updateSettings(settings);
      return true;
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  /// Saves the voice device choice (null = system default); the backend
  /// rebuilds a live pipeline in place. Returns true on success.
  Future<bool> setVoiceDevices({String? inputDevice, String? outputDevice}) async {
    try {
      await api.setVoiceDevices(
          inputDevice: inputDevice, outputDevice: outputDevice);
      return true;
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  /// Sets the display name and re-announces. Returns true on success.
  Future<bool> saveDisplayName(String displayName) async {
    try {
      await api.setDisplayName(displayName);
      meDisplayName = displayName;
      notifyListeners();
      return true;
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  Future<bool> removeFriend(String identityHashHex) async {
    try {
      final ok = await api.removeFriend(identityHashHex);
      if (ok) await loadFriends();
      return ok;
    } catch (e) {
      _reportActionError(e);
      return false;
    }
  }

  /// Puts a failure the client itself detected -- rather than one a request
  /// came back with -- on the same surface every other action error uses.
  void reportError(String message) {
    actionError = message;
    notifyListeners();
  }

  void _reportActionError(Object e) {
    actionError = e is ApiException ? e.message : e.toString();
    notifyListeners();
  }

  /// Unread bookkeeping for a message that just arrived live. The channel on
  /// screen stays caught up (watermark advanced backend-side); any other
  /// channel's badge bumps. A conversation's unread is backend-counted, so a
  /// DM refreshes the list instead.
  void _onNewMessage(String channelHash, Message message) {
    if (message.senderHash == meHashHex) return;
    if (channelHash == selectedChannelHash) {
      unawaited(api.markChannelRead(channelHash).catchError((_) => false));
      return;
    }
    if (dms.any((d) => d.hash == channelHash)) {
      unawaited(loadDms());
      return;
    }
    unreadByChannel[channelHash] = (unreadByChannel[channelHash] ?? 0) + 1;
  }

  /// Applies a socket event directly, so tests can exercise event handling
  /// without standing up a WebSocket.
  @visibleForTesting
  void applyEvent(TcEvent event) => _onEvent(event);

  /// Runs the reconnect resync directly, so tests can exercise it without a
  /// live WebSocket dropping and coming back.
  @visibleForTesting
  void simulateReconnect() => _onSocketReconnected();

  void _onEvent(TcEvent event) {
    switch (event) {
      case MessageEvent(:final channelHash, :final message):
        final list = messagesByChannel.putIfAbsent(channelHash, () => []);
        final idx = list.indexWhere((m) => m.messageId == message.messageId);
        if (idx >= 0) {
          list[idx] = message;
        } else {
          list.add(message);
          _onNewMessage(channelHash, message);
        }
        notifyListeners();
      case PresenceEvent(:final identityHash, :final isOnline):
        for (final entry in presenceByChannel.entries) {
          final list = entry.value;
          final idx = list.indexWhere((p) => p.identityHash == identityHash);
          if (idx >= 0) {
            list[idx] = PresenceEntry(
              identityHash: identityHash,
              isOnline: isOnline,
              displayName: list[idx].displayName,
            );
          }
        }
        final friendIdx = friends.indexWhere((f) => f.identityHash == identityHash);
        if (friendIdx >= 0) {
          final f = friends[friendIdx];
          friends[friendIdx] = Friend(
            identityHash: f.identityHash,
            nickname: f.nickname,
            note: f.note,
            displayName: f.displayName,
            addedAt: f.addedAt,
            lastSeenAt: f.lastSeenAt,
            isOnline: isOnline,
            state: f.state,
            nomadNodeHash: f.nomadNodeHash,
          );
        }
        // The DM sidebar reads its own snapshot of the same presence, so it
        // must move with the event too or it disagrees with the friends list.
        final dmIdx = dms.indexWhere((d) => d.peerHash == identityHash);
        if (dmIdx >= 0) {
          final d = dms[dmIdx];
          dms[dmIdx] = DmConversation(
            hash: d.hash,
            peerHash: d.peerHash,
            displayName: d.displayName,
            createdAt: d.createdAt,
            lastMessageAt: d.lastMessageAt,
            unread: d.unread,
            isOnline: isOnline,
            isFriend: d.isFriend,
            peerIsTrenchchat: d.peerIsTrenchchat,
          );
        }
        notifyListeners();
      case MemberListUpdatedEvent(:final channelHash):
        if (membersByChannel.containsKey(channelHash)) {
          unawaited(api.getMembers(channelHash).then((m) {
            membersByChannel[channelHash] = m;
            notifyListeners();
          }));
        }
      case DeliveryStatusEvent(:final channelHash, :final messageId, :final deliveryState):
        final list = messagesByChannel[channelHash];
        if (list != null) {
          final idx = list.indexWhere((m) => m.messageId == messageId);
          if (idx >= 0) {
            list[idx] = list[idx].withDeliveryState(deliveryState);
            notifyListeners();
          }
        }
      case ReactionUpdatedEvent(:final channelHash):
        _scheduleReactionRefresh(channelHash);
      case ChannelJoinedEvent():
        unawaited(_applyChannelJoined());
      case ServerJoinedEvent():
        unawaited(_applyChannelJoined());
      case ChannelDiscoveredEvent():
        unawaited(refreshDiscoveredChannels());
      case InviteReceivedEvent():
        unawaited(refreshInvites());
      case SyncStatusEvent(:final channelHash, :final state):
        syncStateByChannel[channelHash] = state;
        notifyListeners();
      case EmojiReceivedEvent():
        unawaited(refreshEmoji());
      case FriendUpdatedEvent():
        unawaited(loadFriends());
        unawaited(loadFriendRequests());
        unawaited(loadDms());
      case FriendRequestEvent():
        unawaited(loadFriendRequests());
      case PropagationNodeEvent():
        unawaited(loadPropagation());
      case AvatarUpdatedEvent(:final identityHash, :final avatarVersion):
        unawaited(_applyAvatarUpdated(identityHash, avatarVersion));
      case DirectoryUpdatedEvent(:final identityHash, :final displayName):
        _applyDirectoryUpdated(identityHash, displayName);
      case VoiceRosterEvent(:final channelHash):
        if (channelHash == selectedChannelHash ||
            channelHash == voiceStatus.channel ||
            voiceRosterByChannel.containsKey(channelHash)) {
          unawaited(refreshVoiceRoster(channelHash));
        }
      case VoiceSpeakingEvent(:final channelHash, :final identityHash, :final speaking):
        final roster = voiceRosterByChannel[channelHash];
        if (roster != null) {
          final idx = roster.indexWhere((p) => p.identityHash == identityHash);
          if (idx >= 0) {
            roster[idx] = roster[idx].copyWith(speaking: speaking);
            notifyListeners();
          }
        }
      case NetworkMapChangedEvent():
        _networkMapRevision++;
        notifyListeners();
      case NomadNodeEvent(:final nodeHash, :final displayName):
        final existing = nomadNodes[nodeHash];
        final now = DateTime.now().millisecondsSinceEpoch / 1000.0;
        nomadNodes[nodeHash] = NomadNode(
          nodeHash: nodeHash,
          displayName: displayName,
          firstSeen: existing?.firstSeen ?? now,
          lastSeen: now,
        );
        // A first-heard node may belong to a saved friend; re-fetching the
        // list is what makes their page button appear live.
        if (existing == null && friends.isNotEmpty) {
          unawaited(loadFriends());
        }
        notifyListeners();
      case FileFetchEvent(
          :final fileHash,
          :final messageIds,
          :final channels,
          :final state,
          :final progress,
          :final reason
        ):
        _applyFileFetch(fileHash, messageIds, channels, state, progress, reason);
      case NomadFetchEvent(
          :final fetchId,
          :final nodeHash,
          :final path,
          :final status,
          :final progress,
          :final reason
        ):
        nomadFetches[fetchId] = NomadFetchStatus(
          nodeHash: nodeHash,
          path: path,
          status: status,
          progress: progress,
          reason: reason,
        );
        notifyListeners();
      case UiThemeEvent(:final spec):
        // An event that only says what is already in force is what this
        // client's own save just produced; adopting it again would rebuild
        // every section for nothing.
        if (spec == themeSpec) break;
        themeSpec = spec;
        notifyListeners();
      case UiThemeLibraryEvent(:final library):
        if (mapEquals(library, themeLibrary)) break;
        themeLibrary = library;
        notifyListeners();
      case VoiceSessionEvent(:final state):
        switch (state) {
          case 'joined':
            unawaited(refreshVoiceStatus());
            _startVoicePoll();
          case 'left':
            _stopVoicePoll();
            voiceStatus = VoiceStatus.idle;
            voiceAudioError = false;
            notifyListeners();
          case 'audio_error':
            voiceAudioError = true;
            notifyListeners();
            // The event carries no detail; the status endpoint has the
            // pipeline's stated reason.
            unawaited(refreshVoiceStatus());
        }
    }
  }

  /// A channel or server was joined (accepted invite, or a join that completed
  /// on the backend) -- refetch the server and channel lists so it appears
  /// without a full reload.
  Future<void> _applyChannelJoined() async {
    try {
      await _reloadServersAndChannels();
      notifyListeners();
    } catch (_) {
      // Non-fatal: a later reconnect or reload catches it up.
    }
  }

  /// Busts the avatar cache for a peer whose avatar changed: a non-null
  /// version re-fetches with the version as a cache-buster; a null version
  /// drops the cached image so the initials fallback shows.
  Future<void> _applyAvatarUpdated(String identityHashHex, int? version) async {
    if (version == null) {
      avatarCache[identityHashHex] = null;
      notifyListeners();
      return;
    }
    final data = await api.getPeerAvatar(identityHashHex, version: version);
    avatarCache[identityHashHex] = data;
    notifyListeners();
  }

  /// Patches a peer's cached name in the directory and in any saved friend
  /// record, so both the invite picker and the FRIENDS tab reflect a rename
  /// live without a reload.
  void _applyDirectoryUpdated(String identityHashHex, String displayName) {
    final dirIdx = directory.indexWhere((e) => e.identityHash == identityHashHex);
    if (dirIdx >= 0) {
      final e = directory[dirIdx];
      directory[dirIdx] = DirectoryEntry(
        identityHash: e.identityHash,
        displayName: displayName,
        isOnline: e.isOnline,
      );
    }
    final friendIdx = friends.indexWhere((f) => f.identityHash == identityHashHex);
    if (friendIdx >= 0) {
      final f = friends[friendIdx];
      friends[friendIdx] = Friend(
        identityHash: f.identityHash,
        nickname: f.nickname,
        note: f.note,
        displayName: displayName,
        addedAt: f.addedAt,
        lastSeenAt: f.lastSeenAt,
        isOnline: f.isOnline,
      );
    }
    notifyListeners();
  }

  /// Re-fetch a channel's messages so updated reaction chips render, at most
  /// once per [_reactionRefreshWindow] however many reactions land in it.
  void _scheduleReactionRefresh(String channelHash) {
    if (!messagesByChannel.containsKey(channelHash)) return;
    if (_reactionRefreshTimers.containsKey(channelHash)) return;
    _reactionRefreshTimers[channelHash] = Timer(_reactionRefreshWindow, () {
      _reactionRefreshTimers.remove(channelHash);
      unawaited(refreshMessages(channelHash));
    });
  }

  @override
  void dispose() {
    for (final t in _reactionRefreshTimers.values) {
      t.cancel();
    }
    _reactionRefreshTimers.clear();
    _voicePollTimer?.cancel();
    _sub?.cancel();
    _socket.close();
    api.close();
    super.dispose();
  }
}
