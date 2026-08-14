// 1a: avatar message rows, author grouping, date dividers, reaction chips.
import 'dart:typed_data';

import 'package:flutter/material.dart';

import '../../api/models/message.dart';
import '../../format.dart';
import '../../grouping.dart';
import '../../name_color.dart';
import '../../theme/tokens.dart';
import '../../widgets/avatar.dart';
import '../../widgets/badge.dart';

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
  });

  final List<Message> messages;
  final String meHashHex;
  final String Function(String identityHashHex, String fallback) displayNameFor;

  /// Synchronous cache read -- null until [ensureAvatarLoaded] has fetched it.
  final Uint8List? Function(String identityHashHex)? avatarBytesFor;

  /// Fire-and-forget: triggers the async fetch that populates the cache.
  final void Function(String identityHashHex)? ensureAvatarLoaded;

  final void Function(String messageId, String emojiHash)? onToggleReaction;

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
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 10),
      child: Row(
        children: [
          Expanded(child: Container(height: 1, color: TCColors.borderSubtle)),
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 12),
            child: Text(
              formatDateDivider(timestamp),
              style: TextStyle(
                fontSize: TCType.textMicro,
                color: TCColors.textTertiary,
                letterSpacing: TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWider),
              ),
            ),
          ),
          Expanded(child: Container(height: 1, color: TCColors.borderSubtle)),
        ],
      ),
    );
  }
}

class _MessageRowWidget extends StatelessWidget {
  const _MessageRowWidget({
    required this.message,
    required this.isContinuation,
    required this.isOwn,
    required this.displayName,
    this.avatarBytes,
    this.ensureAvatarLoaded,
    this.onToggleReaction,
  });

  final Message message;
  final bool isContinuation;
  final bool isOwn;
  final String displayName;
  final Uint8List? avatarBytes;
  final void Function(String identityHashHex)? ensureAvatarLoaded;
  final void Function(String messageId, String emojiHash)? onToggleReaction;

  @override
  Widget build(BuildContext context) {
    if (!isContinuation && avatarBytes == null) {
      ensureAvatarLoaded?.call(message.senderHash);
    }
    final bg = isOwn ? const Color.fromRGBO(255, 255, 255, 0.02) : Colors.transparent;
    final body = Text(
      message.content,
      style: TextStyle(
        fontSize: TCType.textBodyMd,
        height: TCType.leadingBody,
        color: TCColors.textPrimary,
      ),
    );

    // TODO(phase-b): message.receivedAt is always null until `received_at`
    // ships on _message_to_dict, so this marker never fires today.
    final isLate = message.receivedAt != null &&
        (message.receivedAt! - message.timestamp) > lateThresholdSecs;

    if (isContinuation) {
      return Container(
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
                  style: TextStyle(fontSize: TCType.textMicro, color: TCColors.textTertiary),
                ),
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  body,
                  if (message.reactions.isNotEmpty) _ReactionRow(message: message, onToggle: onToggleReaction),
                ],
              ),
            ),
          ],
        ),
      );
    }

    final color = nameColor(message.senderHash, isOwn: isOwn);
    return Container(
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
                      style: TextStyle(fontSize: TCType.textMicro, color: TCColors.textTertiary),
                    ),
                    const SizedBox(width: 8),
                    Text(
                      formatTs(message.timestamp),
                      style: TextStyle(fontSize: TCType.textMicro, color: TCColors.textTertiary),
                    ),
                    if (isLate) ...[
                      const SizedBox(width: 8),
                      Text(
                        '⟳ received late',
                        style: TextStyle(
                          fontSize: TCType.textMicro,
                          color: TCColors.textTertiary,
                          fontStyle: FontStyle.italic,
                        ),
                      ),
                    ],
                  ],
                ),
                body,
                if (message.reactions.isNotEmpty) _ReactionRow(message: message, onToggle: onToggleReaction),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _ReactionRow extends StatelessWidget {
  const _ReactionRow({required this.message, this.onToggle});
  final Message message;
  final void Function(String messageId, String emojiHash)? onToggle;

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
              onTap: onToggle == null ? null : () => onToggle!(message.messageId, r.emojiHash),
            ),
        ],
      ),
    );
  }
}
