// 1a: avatar message rows, author grouping, date dividers, reaction chips.
import 'dart:typed_data';

import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';

import '../../api/models/emoji.dart';
import '../../api/models/message.dart';
import '../../format.dart';
import '../../grouping.dart';
import '../../theme/section_theme.dart';
import '../../theme/shape.dart';
import '../../theme/theme_spec.dart';
import '../../theme/tokens.dart';
import '../../mentions.dart';
import '../../name_color.dart';
import '../../widgets/avatar.dart';
import '../../widgets/badge.dart';
import '../../widgets/emoji_text.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_context_menu.dart';
import '../../widgets/tc_icon.dart';
import '../../widgets/tc_tooltip.dart';
import '../../widgets/theme_share.dart';

/// Bounds for an inline attachment. The decode cap keeps the raster tracking
/// the drawn width rather than whatever dimensions the file declares -- the
/// same reason peer_image.dart caps its own.
const double _attachmentMaxWidth = 400;
const double _attachmentMaxHeight = 300;
const int _attachmentDecodeCap = 1200;

sealed class _Row {}

class _DateDividerRow extends _Row {
  _DateDividerRow(this.timestamp);
  final double timestamp;
}

class _MessageRow extends _Row {
  _MessageRow(this.message, {required this.isContinuation});
  final Message message;
  final bool isContinuation;
}

List<_Row> _buildRows(List<Message> messages) {
  final sorted = [...messages]..sort((a, b) => a.timestamp.compareTo(b.timestamp));
  final rows = <_Row>[];
  Message? prev;
  for (final msg in sorted) {
    if (prev != null && !isSameLocalDay(prev.timestamp, msg.timestamp)) {
      rows.add(_DateDividerRow(msg.timestamp));
    }
    final isContinuation = prev != null &&
        prev.senderHash == msg.senderHash &&
        isSameLocalDay(prev.timestamp, msg.timestamp) &&
        (msg.timestamp - prev.timestamp) < groupWindowSecs;
    rows.add(_MessageRow(msg, isContinuation: isContinuation));
    prev = msg;
  }
  return rows;
}

class MessageList extends StatefulWidget {
  const MessageList({
    super.key,
    required this.messages,
    required this.meHashHex,
    required this.displayNameFor,
    this.avatarBytesFor,
    this.ensureAvatarLoaded,
    this.attachmentBytesFor,
    this.ensureAttachmentLoaded,
    this.onToggleReaction,
    this.onReact,
    this.emojiLibrary = const {},
    this.friendHashes = const {},
    this.onAddFriend,
    this.onAddTheme,
    this.onApplyTheme,
    this.themeLibrary = const {},
    this.onLoadOlder,
    this.hasMoreOlder = false,
    this.loadingOlder = false,
    this.onReply,
    this.onOpenLink,
    this.resolveMentionName,
  });

  final List<Message> messages;
  final String meHashHex;
  final String Function(String identityHashHex, String fallback) displayNameFor;

  /// The name this client knows an identity by, for an `@<hash>` mention in
  /// message text. Null, or a null return, renders the mention as a short
  /// hash: a name nothing here can vouch for is not one to invent.
  final String? Function(String identityHashHex)? resolveMentionName;

  /// Starts a reply to a message, from its row menu. Null hides the action.
  final void Function(Message message)? onReply;

  /// Opens a tapped URL in message content. Null leaves links styled but inert.
  final void Function(String url)? onOpenLink;

  /// Fetches the next older page when the reader scrolls to the top. Null
  /// disables load-on-scroll (e.g. isolated widget tests).
  final VoidCallback? onLoadOlder;

  /// Whether an older page may still exist; the top-of-list loader hides when
  /// history's start has been reached.
  final bool hasMoreOlder;

  /// Whether an older-page fetch is in flight, so the trigger fires once.
  final bool loadingOlder;

  /// Synchronous cache read -- null until [ensureAvatarLoaded] has fetched it.
  final Uint8List? Function(String identityHashHex)? avatarBytesFor;

  /// Fire-and-forget: triggers the async fetch that populates the cache.
  final void Function(String identityHashHex)? ensureAvatarLoaded;

  /// Same pair as the avatar one, for a message's attached image: a
  /// synchronous cache read and the fetch that fills it.
  final Uint8List? Function(String messageId)? attachmentBytesFor;
  final void Function(String messageId)? ensureAttachmentLoaded;

  final void Function(String messageId, String emojiHash)? onToggleReaction;

  /// Opens the emoji picker for a message (the hover react button).
  final void Function(String messageId)? onReact;

  /// Custom emoji by hash, for reaction chips and inline :name@hash: tokens.
  final Map<String, CustomEmoji> emojiLibrary;

  /// Identity hashes already saved as a friend -- drives the "Add friend…"
  /// vs "Edit friend…" context menu label.
  final Set<String> friendHashes;

  /// Fired with a sender's identity hash when "Add/Edit friend…" is chosen
  /// from a message row's right-click menu.
  final void Function(String identityHashHex)? onAddFriend;

  /// Saves a theme shared in chat to the named library. Null hides the ADD
  /// button on the theme card.
  final Future<bool> Function(String name, ThemeSpec spec)? onAddTheme;

  /// Makes a shared theme the active one. Null hides the APPLY button.
  final Future<bool> Function(ThemeSpec spec)? onApplyTheme;

  /// The reader's saved themes, so a shared one never quietly replaces a
  /// theme of the same name.
  final Map<String, ThemeSpec> themeLibrary;

  @override
  State<MessageList> createState() => _MessageListState();
}

/// Distance from the top, in pixels, at which scrolling triggers the next
/// older-page fetch -- a little ahead of the very edge so the page is loading
/// before the reader reaches blank space.
const double _loadOlderThreshold = 120;

/// How close to the bottom (in pixels) still counts as "at the bottom", so a
/// new message auto-scrolls; past this the reader is reading history and the
/// view is left put.
const double _nearBottomThreshold = 80;

/// How near the reported extent counts as already at it, so a re-jump stops
/// rather than chasing a fractional remainder.
const double _atBottomEpsilon = 0.5;

class _MessageListState extends State<MessageList> {
  final ScrollController _controller = ScrollController();

  /// What the message set looked like last time we reacted to it, so a change
  /// is detected even when the parent mutates the same list instance in place.
  int _trackedCount = 0;
  String? _trackedNewestId;
  double? _trackedOldestTs;

  /// A new message arrived while the reader was scrolled up: the affordance
  /// that jumps them back to the newest.
  bool _showNewPill = false;

  /// Whether the view belongs at the newest message. A lazily built list of
  /// variable-height rows only estimates its extent from the rows laid out so
  /// far, so one jump lands short of the real bottom; while this holds, every
  /// extent change jumps again.
  bool _pinnedToBottom = true;

  /// True while a scroll of our own is running, so it is not read as the
  /// reader moving away from the bottom.
  bool _programmaticScroll = false;

  /// A re-jump is already queued for the end of this frame.
  bool _bottomJumpScheduled = false;

  @override
  void initState() {
    super.initState();
    _controller.addListener(_onScroll);
    _updateTracked(widget.messages);
    WidgetsBinding.instance.addPostFrameCallback((_) => _jumpToBottom());
  }

  @override
  void didUpdateWidget(covariant MessageList oldWidget) {
    super.didUpdateWidget(oldWidget);
    _reactToMessageChange();
  }

  void _reactToMessageChange() {
    final msgs = widget.messages;
    final newestMsg = _newest(msgs);
    final newestId = newestMsg?.messageId;
    final oldestTs = _oldestTs(msgs);
    final count = msgs.length;

    // A rebuild that left the message set alone (a reaction or presence
    // update) -- nothing to scroll.
    if (count == _trackedCount && newestId == _trackedNewestId) return;

    final prevCount = _trackedCount;
    final prevNewestId = _trackedNewestId;
    final prevOldestTs = _trackedOldestTs;
    _trackedCount = count;
    _trackedNewestId = newestId;
    _trackedOldestTs = oldestTs;

    // An older page landed at the top: hold the view on the same message
    // rather than jerking it.
    final isPrepend = prevNewestId != null &&
        count > prevCount &&
        newestId == prevNewestId &&
        prevOldestTs != null &&
        oldestTs != null &&
        oldestTs < prevOldestTs;
    if (isPrepend) {
      final before = _controller.hasClients ? _controller.position.maxScrollExtent : 0.0;
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (!_controller.hasClients) return;
        final after = _controller.position.maxScrollExtent;
        _controller.jumpTo(_controller.position.pixels + (after - before));
      });
      return;
    }

    // A wholesale swap (channel switch, first load) always shows the newest;
    // an incremental arrival only does when the reader is at the bottom or it
    // is their own message. Otherwise raise the "new messages" affordance.
    final isIncremental =
        prevNewestId != null && msgs.any((m) => m.messageId == prevNewestId);
    final newestIsOwn = newestMsg != null && newestMsg.senderHash == widget.meHashHex;
    if (!isIncremental || newestIsOwn || _isNearBottom()) {
      if (_showNewPill) setState(() => _showNewPill = false);
      WidgetsBinding.instance.addPostFrameCallback((_) => _jumpToBottom());
    } else if (!_showNewPill) {
      setState(() => _showNewPill = true);
    }
  }

  void _updateTracked(List<Message> msgs) {
    _trackedCount = msgs.length;
    _trackedNewestId = _newest(msgs)?.messageId;
    _trackedOldestTs = _oldestTs(msgs);
  }

  Message? _newest(List<Message> m) {
    if (m.isEmpty) return null;
    var best = m.first;
    for (final e in m) {
      if (e.timestamp > best.timestamp) best = e;
    }
    return best;
  }

  double? _oldestTs(List<Message> m) {
    if (m.isEmpty) return null;
    var v = m.first.timestamp;
    for (final e in m) {
      if (e.timestamp < v) v = e.timestamp;
    }
    return v;
  }

  bool _isNearBottom() {
    if (!_controller.hasClients) return true;
    final pos = _controller.position;
    return (pos.maxScrollExtent - pos.pixels) <= _nearBottomThreshold;
  }

  void _onScroll() {
    if (!_controller.hasClients) return;
    if (!_programmaticScroll) _pinnedToBottom = _isNearBottom();
    if (_showNewPill && _isNearBottom()) setState(() => _showNewPill = false);
    if (widget.onLoadOlder == null || !widget.hasMoreOlder || widget.loadingOlder) return;
    if (_controller.position.pixels <= _loadOlderThreshold) {
      widget.onLoadOlder!();
    }
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  void _jumpToBottom() {
    if (!_controller.hasClients) return;
    _pinnedToBottom = true;
    _programmaticScroll = true;
    _controller.jumpTo(_controller.position.maxScrollExtent);
    _programmaticScroll = false;
  }

  /// Jumps again when the extent grew under a view that belongs at the newest
  /// message, which is how the estimate an unscrolled list reports converges
  /// on the real one. Returns false so the notification keeps travelling.
  bool _keepPinnedToBottom() {
    if (!_pinnedToBottom || _bottomJumpScheduled || !_controller.hasClients) return false;
    final pos = _controller.position;
    if ((pos.maxScrollExtent - pos.pixels) <= _atBottomEpsilon) return false;
    _bottomJumpScheduled = true;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _bottomJumpScheduled = false;
      if (mounted && _pinnedToBottom) _jumpToBottom();
    });
    return false;
  }

  void _animateToBottom() {
    setState(() => _showNewPill = false);
    if (!_controller.hasClients) return;
    _pinnedToBottom = true;
    _programmaticScroll = true;
    _controller
        .animateTo(
          _controller.position.maxScrollExtent,
          duration: const Duration(milliseconds: 200),
          curve: Curves.easeOut,
        )
        .whenComplete(() => _programmaticScroll = false);
  }

  @override
  Widget build(BuildContext context) {
    final rows = _buildRows(widget.messages);
    final showLoader = widget.loadingOlder;
    if (rows.isEmpty && !showLoader) {
      final tc = SectionTheme.of(context);
      return Center(
        child: Text(
          'No messages yet — say something.',
          style: TextStyle(fontSize: TCType.textBodySm, color: tc.textTertiary),
        ),
      );
    }
    final byId = {for (final m in widget.messages) m.messageId: m};
    final list = ListView.builder(
      controller: _controller,
      padding: const EdgeInsets.symmetric(vertical: 12),
      itemCount: rows.length + (showLoader ? 1 : 0),
      itemBuilder: (context, i) {
        if (showLoader && i == 0) return const _OlderLoader();
        final row = rows[showLoader ? i - 1 : i];
        return switch (row) {
          _DateDividerRow() => _DateDivider(timestamp: row.timestamp),
          _MessageRow() => _MessageRowWidget(
              message: row.message,
              isContinuation: row.isContinuation,
              isOwn: row.message.senderHash == widget.meHashHex,
              meHashHex: widget.meHashHex,
              resolveMentionName: widget.resolveMentionName,
              displayName: widget.displayNameFor(row.message.senderHash, row.message.senderName),
              parent: row.message.replyTo == null ? null : byId[row.message.replyTo!],
              parentDisplayName: () {
                final p = row.message.replyTo == null ? null : byId[row.message.replyTo!];
                return p == null ? '' : widget.displayNameFor(p.senderHash, p.senderName);
              }(),
              parentIsOwn: () {
                final p = row.message.replyTo == null ? null : byId[row.message.replyTo!];
                return p != null && p.senderHash == widget.meHashHex;
              }(),
              avatarBytes: widget.avatarBytesFor?.call(row.message.senderHash),
              ensureAvatarLoaded: widget.ensureAvatarLoaded,
              attachmentBytes:
                  widget.attachmentBytesFor?.call(row.message.messageId),
              ensureAttachmentLoaded: widget.ensureAttachmentLoaded,
              onToggleReaction: widget.onToggleReaction,
              onReact: widget.onReact,
              onReply: widget.onReply,
              onOpenLink: widget.onOpenLink,
              emojiLibrary: widget.emojiLibrary,
              friendHashes: widget.friendHashes,
              onAddFriend: widget.onAddFriend,
              onAddTheme: widget.onAddTheme,
              onApplyTheme: widget.onApplyTheme,
              themeLibrary: widget.themeLibrary,
            ),
        };
      },
    );
    return Stack(
      children: [
        // Selectable so a code, a hash or a link someone sent can be copied
        // out. The pill stays outside it: a button you can drag-select is a
        // button that fights you.
        Positioned.fill(
          child: NotificationListener<ScrollMetricsNotification>(
            onNotification: (_) => _keepPinnedToBottom(),
            child: SelectionArea(child: list),
          ),
        ),
        if (_showNewPill)
          Positioned(
            left: 0,
            right: 0,
            bottom: 12,
            child: Center(child: _NewMessagesPill(onTap: _animateToBottom)),
          ),
      ],
    );
  }
}

class _NewMessagesPill extends StatelessWidget {
  const _NewMessagesPill({required this.onTap});
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    return MouseRegion(
      cursor: SystemMouseCursors.click,
      child: GestureDetector(
        onTap: onTap,
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
          decoration: BoxDecoration(
            color: tc.accentPrimary,
            border: Border.all(color: tc.borderAccent),
            borderRadius: tcCorners(context, scale: 1.5),
          ),
          child: Text(
            '↓ NEW MESSAGES',
            style: TextStyle(
              fontSize: TCType.textMicro,
              color: tc.textOnAccent,
              fontWeight: FontWeight.w600,
              letterSpacing: TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWide),
            ),
          ),
        ),
      ),
    );
  }
}

class _OlderLoader extends StatelessWidget {
  const _OlderLoader();

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 10),
      child: Center(
        child: Text(
          'LOADING EARLIER MESSAGES…',
          style: TextStyle(
            fontSize: TCType.textMicro,
            color: tc.textTertiary,
            letterSpacing: TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWider),
          ),
        ),
      ),
    );
  }
}

class _DateDivider extends StatelessWidget {
  const _DateDivider({required this.timestamp});
  final double timestamp;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 10),
      child: Row(
        children: [
          Expanded(child: Container(height: 1, color: tc.borderSubtle)),
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 12),
            child: Text(
              formatDateDivider(timestamp),
              style: TextStyle(
                fontSize: TCType.textMicro,
                color: tc.textTertiary,
                letterSpacing: TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWider),
              ),
            ),
          ),
          Expanded(child: Container(height: 1, color: tc.borderSubtle)),
        ],
      ),
    );
  }
}

/// How much accent a row that pings the reader picks up, and how much sits
/// behind the mention itself. Low enough that a busy channel does not turn
/// into a wall of highlight.
const double _mentionRowTint = 0.06;
const double _mentionSelfTint = 0.16;

class _MessageRowWidget extends StatefulWidget {
  const _MessageRowWidget({
    required this.message,
    required this.isContinuation,
    required this.isOwn,
    required this.meHashHex,
    required this.displayName,
    this.resolveMentionName,
    this.parent,
    this.parentDisplayName = '',
    this.parentIsOwn = false,
    this.avatarBytes,
    this.ensureAvatarLoaded,
    this.attachmentBytes,
    this.ensureAttachmentLoaded,
    this.onToggleReaction,
    this.onReact,
    this.onReply,
    this.onOpenLink,
    this.emojiLibrary = const {},
    this.friendHashes = const {},
    this.onAddFriend,
    this.onAddTheme,
    this.onApplyTheme,
    this.themeLibrary = const {},
  });

  final Message message;
  final bool isContinuation;
  final bool isOwn;
  final String meHashHex;
  final String displayName;

  /// See [MessageList.resolveMentionName].
  final String? Function(String identityHashHex)? resolveMentionName;

  /// The message this one replies to, if it is loaded; null when this is not a
  /// reply, or the parent is not on screen.
  final Message? parent;
  final String parentDisplayName;
  final bool parentIsOwn;

  final Uint8List? avatarBytes;
  final void Function(String identityHashHex)? ensureAvatarLoaded;
  final Uint8List? attachmentBytes;
  final void Function(String messageId)? ensureAttachmentLoaded;
  final void Function(String messageId, String emojiHash)? onToggleReaction;
  final void Function(String messageId)? onReact;
  final void Function(Message message)? onReply;
  final void Function(String url)? onOpenLink;
  final Map<String, CustomEmoji> emojiLibrary;

  /// Identity hashes already saved as a friend -- drives the "Add friend…"
  /// vs "Edit friend…" context menu label.
  final Set<String> friendHashes;

  /// Fired with the sender's identity hash when "Add/Edit friend…" is chosen
  /// from the row's right-click menu.
  final void Function(String identityHashHex)? onAddFriend;

  /// Saves a theme shared in this message to the named library.
  final Future<bool> Function(String name, ThemeSpec spec)? onAddTheme;

  /// Makes a shared theme the active one.
  final Future<bool> Function(ThemeSpec spec)? onApplyTheme;

  /// The reader's saved themes, which decide the name a shared one lands
  /// under.
  final Map<String, ThemeSpec> themeLibrary;

  @override
  State<_MessageRowWidget> createState() => _MessageRowWidgetState();
}

class _MessageRowWidgetState extends State<_MessageRowWidget> {
  bool _hover = false;

  /// Tap recognizers for the link spans in the current build; rebuilt and
  /// disposed each build so none leak.
  final List<TapGestureRecognizer> _linkRecognizers = [];

  /// The URL currently under the pointer, painted with linkHoverColor.
  String? _hoveredLink;

  @override
  void dispose() {
    for (final r in _linkRecognizers) {
      r.dispose();
    }
    super.dispose();
  }

  Message get message => widget.message;
  bool get isContinuation => widget.isContinuation;
  bool get isOwn => widget.isOwn;
  String get displayName => widget.displayName;
  Uint8List? get avatarBytes => widget.avatarBytes;
  void Function(String identityHashHex)? get ensureAvatarLoaded => widget.ensureAvatarLoaded;
  void Function(String messageId, String emojiHash)? get onToggleReaction =>
      widget.onToggleReaction;
  Set<String> get friendHashes => widget.friendHashes;
  void Function(String identityHashHex)? get onAddFriend => widget.onAddFriend;

  /// The message's attached image, once it has been fetched. Sized to a
  /// bounded box rather than the file's own dimensions, which are a peer's
  /// claim about bytes the backend stores without requiring them to parse.
  Widget _attachment(TCSectionColors tc) {
    final bytes = widget.attachmentBytes;
    if (bytes == null) {
      return Container(
        width: _attachmentMaxWidth,
        height: 120,
        decoration: BoxDecoration(
          color: tc.bgInset,
          borderRadius: tcCorners(context),
        ),
      );
    }
    return ClipRRect(
      borderRadius: tcCorners(context) ?? BorderRadius.zero,
      child: ConstrainedBox(
        constraints: const BoxConstraints(
          maxWidth: _attachmentMaxWidth,
          maxHeight: _attachmentMaxHeight,
        ),
        child: Image.memory(
          bytes,
          fit: BoxFit.contain,
          alignment: Alignment.topLeft,
          filterQuality: FilterQuality.medium,
          cacheWidth: _attachmentDecodeCap,
          errorBuilder: (context, error, stack) => Text(
            'Attachment could not be displayed',
            style: TextStyle(fontSize: TCType.textMicro, color: tc.textTertiary),
          ),
        ),
      ),
    );
  }

  /// Wraps a row in the hover tracker and the react affordance.
  Widget _withReactButton(Widget row) {
    return MouseRegion(
      onEnter: (_) => setState(() => _hover = true),
      onExit: (_) => setState(() => _hover = false),
      child: Stack(
        children: [
          row,
          if (_hover && widget.onReact != null)
            Positioned(
              right: 20,
              top: 2,
              child: TcIconButton(
                icon: TcIcons.emoji,
                tooltip: 'React',
                size: 22,
                onPressed: () => widget.onReact!(message.messageId),
              ),
            ),
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    if (!isContinuation && avatarBytes == null) {
      ensureAvatarLoaded?.call(message.senderHash);
    }
    if (message.hasImage && widget.attachmentBytes == null) {
      widget.ensureAttachmentLoaded?.call(message.messageId);
    }
    final tc = SectionTheme.of(context);
    // A ping the reader wrote themselves is not one to shout back at them.
    final pingsMe = !isOwn && contentMentions(message.content, widget.meHashHex);
    final bg = pingsMe
        ? tc.accentPrimary.withValues(alpha: _mentionRowTint)
        : isOwn
            ? const Color.fromRGBO(255, 255, 255, 0.02)
            : Colors.transparent;
    final baseStyle = TextStyle(
      fontSize: TCType.textBodyMd,
      height: TCType.leadingBody,
      color: tc.textPrimary,
    );
    // An all-emoji message renders jumbo, so a sent emoji is unmistakably
    // larger than the reaction chips.
    final jumboEmoji = !message.hasImage &&
        emojiOnlyCount(message.content, widget.emojiLibrary) != null;
    final bodyStyle = jumboEmoji
        ? baseStyle.copyWith(fontSize: jumboEmojiFontSize, height: 1.2)
        : baseStyle;
    for (final r in _linkRecognizers) {
      r.dispose();
    }
    _linkRecognizers.clear();
    final links = InlineLinkConfig(
      style: baseStyle.copyWith(color: tc.linkColor, decoration: TextDecoration.underline),
      hoverStyle:
          baseStyle.copyWith(color: tc.linkHoverColor, decoration: TextDecoration.underline),
      recognizers: _linkRecognizers,
      onTap: widget.onOpenLink,
      hoveredUrl: _hoveredLink,
      onHover: (url) => setState(() => _hoveredLink = url),
    );
    // Deltas, not whole styles: an all-emoji message renders jumbo, and a
    // mention inside one keeps that run's size.
    final mentions = MentionConfig(
      style: TextStyle(color: tc.accentSecondary, fontWeight: FontWeight.w600),
      selfStyle: TextStyle(
        color: tc.accentPrimary,
        fontWeight: FontWeight.w600,
        backgroundColor: tc.accentPrimary.withValues(alpha: _mentionSelfTint),
      ),
      resolveName: widget.resolveMentionName ?? (_) => null,
      selfHash: widget.meHashHex,
    );
    final bodyText = Text.rich(
      TextSpan(
        children: messageContentSpans(
          message.content,
          widget.emojiLibrary,
          bodyStyle,
          links: links,
          mentions: mentions,
          emojiSize: jumboEmoji ? jumboEmojiSize : inlineEmojiSize,
        ),
      ),
    );

    final sharedThemes = themeCodesIn(message.content);

    // An attachment we refused leaves the text intact, so without this the
    // message reads as though it never had one.
    final body = message.hasImage || message.imageStripped || sharedThemes.isNotEmpty
        ? Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              if (message.content.isNotEmpty) bodyText,
              if (message.hasImage) ...[
                if (message.content.isNotEmpty) const SizedBox(height: 6),
                _attachment(tc),
              ],
              if (message.imageStripped) ...[
                const SizedBox(height: 4),
                Text(
                  'Attachment removed \u2014 it could not be displayed safely',
                  style: TextStyle(
                    fontSize: TCType.textMicro,
                    color: tc.accentSecondary,
                  ),
                ),
              ],
              for (final theme in sharedThemes)
                ThemeCodeCard(
                  key: ValueKey('theme-card:${message.messageId}:${theme.code}'),
                  name: theme.name,
                  spec: theme.spec,
                  library: widget.themeLibrary,
                  onAdd: widget.onAddTheme,
                  onApply: widget.onApplyTheme,
                ),
            ],
          )
        : bodyText;

    final Widget? replyPreview = message.replyTo == null
        ? null
        : _ReplyPreview(
            parent: widget.parent,
            authorName: widget.parentDisplayName,
            isOwn: widget.parentIsOwn,
          );

    // TODO(phase-b): message.receivedAt is always null until `received_at`
    // ships on _message_to_dict, so this marker never fires today.
    final isLate = message.receivedAt != null &&
        (message.receivedAt! - message.timestamp) > lateThresholdSecs;

    final Widget content;
    if (isContinuation) {
      content = Container(
        color: bg,
        padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 1),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            SizedBox(
              width: 34,
              child: Padding(
                padding: const EdgeInsets.only(top: 4),
                child: Text(
                  formatTsShort(message.timestamp),
                  textAlign: TextAlign.right,
                  style: TextStyle(fontSize: TCType.textMicro, color: tc.textTertiary),
                ),
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  ?replyPreview,
                  body,
                  if (message.reactions.isNotEmpty)
                    _ReactionRow(
                        message: message,
                        onToggle: onToggleReaction,
                        emojiLibrary: widget.emojiLibrary),
                ],
              ),
            ),
            if (isOwn && message.deliveryState != null)
              _DeliveryIndicator(state: message.deliveryState!),
          ],
        ),
      );
    } else {
      final color = nameColor(message.senderHash, isOwn: isOwn);
      content = Container(
        color: bg,
        padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 6),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Avatar(name: displayName, imageBytes: avatarBytes, size: 34),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    crossAxisAlignment: CrossAxisAlignment.baseline,
                    textBaseline: TextBaseline.alphabetic,
                    children: [
                      Text(
                        isOwn ? 'you' : displayName,
                        style: TextStyle(fontSize: 13, fontWeight: FontWeight.w600, color: color),
                      ),
                      const SizedBox(width: 8),
                      Text(
                        '[${message.senderHash.substring(0, message.senderHash.length >= 8 ? 8 : message.senderHash.length)}]',
                        style: TextStyle(fontSize: TCType.textMicro, color: tc.textTertiary),
                      ),
                      const SizedBox(width: 8),
                      Text(
                        formatTs(message.timestamp),
                        style: TextStyle(fontSize: TCType.textMicro, color: tc.textTertiary),
                      ),
                      if (isLate) ...[
                        const SizedBox(width: 8),
                        Text(
                          '⟳ received late',
                          style: TextStyle(
                            fontSize: TCType.textMicro,
                            color: tc.textTertiary,
                            fontStyle: FontStyle.italic,
                          ),
                        ),
                      ],
                    ],
                  ),
                  ?replyPreview,
                  body,
                  if (message.reactions.isNotEmpty)
                    _ReactionRow(
                        message: message,
                        onToggle: onToggleReaction,
                        emojiLibrary: widget.emojiLibrary),
                ],
              ),
            ),
            if (isOwn && message.deliveryState != null)
              _DeliveryIndicator(state: message.deliveryState!),
          ],
        ),
      );
    }

    return _withReactButton(TcContextMenuRegion(
      items: [
        if (widget.onReply != null)
          TcContextMenuItem(
            label: 'Reply…',
            onTap: () => widget.onReply!(message),
          ),
        // The hover-only react button above is unreachable without a mouse,
        // so touch users get the same action from the long-press menu.
        if (widget.onReact != null)
          TcContextMenuItem(
            label: 'React…',
            onTap: () => widget.onReact!(message.messageId),
          ),
        // Never on your own message: the backend refuses befriending yourself,
        // so offering it here could only ever fail.
        if (onAddFriend != null && !isOwn)
          TcContextMenuItem(
            label: friendHashes.contains(message.senderHash) ? 'Edit friend…' : 'Add friend…',
            onTap: () => onAddFriend!(message.senderHash),
          ),
      ],
      child: content,
    ));
  }
}

/// The quoted line above a reply: the parent's author and a one-line snippet,
/// or a graceful fallback when the parent is not loaded locally.
class _ReplyPreview extends StatelessWidget {
  const _ReplyPreview({required this.parent, required this.authorName, required this.isOwn});
  final Message? parent;
  final String authorName;
  final bool isOwn;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    return Padding(
      padding: const EdgeInsets.only(bottom: 3),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Container(width: 2, height: 14, color: tc.borderStrong),
          const SizedBox(width: 6),
          Expanded(
            child: parent == null
                ? Text(
                    '↩ original message',
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(
                      fontSize: TCType.textMicro,
                      color: tc.textTertiary,
                      fontStyle: FontStyle.italic,
                    ),
                  )
                : RichText(
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    text: TextSpan(children: [
                      TextSpan(
                        text: '${isOwn ? 'you' : authorName} ',
                        style: TextStyle(
                          fontSize: TCType.textMicro,
                          fontWeight: FontWeight.w600,
                          color: nameColor(parent!.senderHash, isOwn: isOwn),
                        ),
                      ),
                      TextSpan(
                        text: parent!.content.replaceAll('\n', ' '),
                        style: TextStyle(fontSize: TCType.textMicro, color: tc.textTertiary),
                      ),
                    ]),
                  ),
          ),
        ],
      ),
    );
  }
}

/// A subtle status glyph on the reader's own outbound messages: a message to
/// an unreachable peer must not look identical to a delivered one. Shown only
/// when the backend tracks a state (null for peers' messages and untracked
/// sends -- see Message.deliveryState).
class _DeliveryIndicator extends StatelessWidget {
  const _DeliveryIndicator({required this.state});
  final String state;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final (String glyph, Color color, String tip) = switch (state) {
      'pending' => ('◷', tc.textTertiary, 'Queued — waiting to deliver'),
      'failed' => ('⚠', tc.accentSecondary, 'Delivery failed'),
      'delivered' => ('✓', tc.textTertiary, 'Delivered'),
      _ => ('', tc.textTertiary, ''),
    };
    if (glyph.isEmpty) return const SizedBox.shrink();
    return Padding(
      padding: const EdgeInsets.only(left: 8, top: 4),
      child: Tooltip(
        decoration: tcTooltipDecoration(context),
        textStyle: tcTooltipTextStyle(context),
        message: tip,
        child: Text(
          glyph,
          style: TextStyle(fontSize: TCType.textMicro, color: color),
        ),
      ),
    );
  }
}

class _ReactionRow extends StatelessWidget {
  const _ReactionRow({required this.message, this.onToggle, this.emojiLibrary = const {}});
  final Message message;
  final void Function(String messageId, String emojiHash)? onToggle;
  final Map<String, CustomEmoji> emojiLibrary;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(top: 5),
      child: Wrap(
        spacing: 4,
        children: [
          for (final r in message.reactions)
            ReactionChip(
              emoji: r.emojiHash,
              count: r.count,
              reactedByMe: r.reactedByMe,
              imageBytes: emojiLibrary[r.emojiHash]?.imageBytes,
              onTap: onToggle == null ? null : () => onToggle!(message.messageId, r.emojiHash),
            ),
        ],
      ),
    );
  }
}
