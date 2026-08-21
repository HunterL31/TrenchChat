// 1a: avatar message rows, author grouping, date dividers, reaction chips.
import 'dart:typed_data';

import 'package:flutter/material.dart';

import '../../api/models/emoji.dart';
import '../../api/models/message.dart';
import '../../format.dart';
import '../../grouping.dart';
import '../../name_color.dart';
import '../../theme/section_theme.dart';
import '../../theme/theme_spec.dart';
import '../../theme/tokens.dart';
import '../../widgets/avatar.dart';
import '../../widgets/badge.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_context_menu.dart';
import '../../widgets/tc_icon.dart';
import '../../widgets/theme_share.dart';

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
    this.onToggleReaction,
    this.onReact,
    this.emojiLibrary = const {},
    this.friendHashes = const {},
    this.onAddFriend,
    this.onAddTheme,
    this.onApplyTheme,
    this.themeLibrary = const {},
  });

  final List<Message> messages;
  final String meHashHex;
  final String Function(String identityHashHex, String fallback) displayNameFor;

  /// Synchronous cache read -- null until [ensureAvatarLoaded] has fetched it.
  final Uint8List? Function(String identityHashHex)? avatarBytesFor;

  /// Fire-and-forget: triggers the async fetch that populates the cache.
  final void Function(String identityHashHex)? ensureAvatarLoaded;

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

class _MessageListState extends State<MessageList> {
  final ScrollController _controller = ScrollController();

  @override
  void didUpdateWidget(covariant MessageList oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.messages.length != widget.messages.length) {
      WidgetsBinding.instance.addPostFrameCallback((_) => _jumpToBottom());
    }
  }

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) => _jumpToBottom());
  }

  void _jumpToBottom() {
    if (!_controller.hasClients) return;
    _controller.jumpTo(_controller.position.maxScrollExtent);
  }

  @override
  Widget build(BuildContext context) {
    final rows = _buildRows(widget.messages);
    return ListView.builder(
      controller: _controller,
      padding: const EdgeInsets.symmetric(vertical: 12),
      itemCount: rows.length,
      itemBuilder: (context, i) {
        final row = rows[i];
        return switch (row) {
          _DateDividerRow() => _DateDivider(timestamp: row.timestamp),
          _MessageRow() => _MessageRowWidget(
              message: row.message,
              isContinuation: row.isContinuation,
              isOwn: row.message.senderHash == widget.meHashHex,
              displayName: widget.displayNameFor(row.message.senderHash, row.message.senderName),
              avatarBytes: widget.avatarBytesFor?.call(row.message.senderHash),
              ensureAvatarLoaded: widget.ensureAvatarLoaded,
              onToggleReaction: widget.onToggleReaction,
              onReact: widget.onReact,
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

class _MessageRowWidget extends StatefulWidget {
  const _MessageRowWidget({
    required this.message,
    required this.isContinuation,
    required this.isOwn,
    required this.displayName,
    this.avatarBytes,
    this.ensureAvatarLoaded,
    this.onToggleReaction,
    this.onReact,
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
  final String displayName;
  final Uint8List? avatarBytes;
  final void Function(String identityHashHex)? ensureAvatarLoaded;
  final void Function(String messageId, String emojiHash)? onToggleReaction;
  final void Function(String messageId)? onReact;
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
    final tc = SectionTheme.of(context);
    final bg = isOwn ? const Color.fromRGBO(255, 255, 255, 0.02) : Colors.transparent;
    final bodyText = Text.rich(
      TextSpan(
        children: messageContentSpans(
          message.content,
          widget.emojiLibrary,
          TextStyle(
            fontSize: TCType.textBodyMd,
            height: TCType.leadingBody,
            color: tc.textPrimary,
          ),
        ),
      ),
    );

    final sharedThemes = themeCodesIn(message.content);

    // An attachment we refused leaves the text intact, so without this the
    // message reads as though it never had one.
    final body = message.imageStripped || sharedThemes.isNotEmpty
        ? Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              bodyText,
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
                  body,
                  if (message.reactions.isNotEmpty)
                    _ReactionRow(
                        message: message,
                        onToggle: onToggleReaction,
                        emojiLibrary: widget.emojiLibrary),
                ],
              ),
            ),
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
                  body,
                  if (message.reactions.isNotEmpty)
                    _ReactionRow(
                        message: message,
                        onToggle: onToggleReaction,
                        emojiLibrary: widget.emojiLibrary),
                ],
              ),
            ),
          ],
        ),
      );
    }

    return _withReactButton(TcContextMenuRegion(
      items: [
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
