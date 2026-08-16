// Emoji picker -- port of emoji_picker.py's EmojiPicker popup: search box,
// emoji grid, import footer. Presented as a compact dialog rather than an
// anchored popup. Reactions key on the raw string (a unicode char for
// built-ins, the SHA-256 hash for customs), so both grids return one
// EmojiSelection the caller can react or compose with.
import 'package:flutter/material.dart';

import '../../app_state.dart';
import '../../theme/effects.dart';
import '../../theme/tokens.dart';
import '../../widgets/tc_dialog.dart';
import '../../widgets/tc_text_field.dart';
import 'emoji_import_dialog.dart';

/// What the user picked: [reactionKey] goes to the reactions endpoint;
/// [composeToken] is what gets inserted into the compose field.
class EmojiSelection {
  const EmojiSelection({required this.reactionKey, required this.composeToken});

  final String reactionKey;
  final String composeToken;
}

const List<String> _builtinEmoji = [
  '👍', '👎', '❤️', '😂', '😮', '😢', '🔥', '🎉',
  '✅', '❌', '👀', '🫡', '🤔', '🙏', '💯', '🚀',
];

Future<EmojiSelection?> showEmojiPickerDialog(BuildContext context, AppState state) {
  return showTcDialog<EmojiSelection>(
    context: context,
    builder: (context) => _EmojiPickerContent(state: state),
  );
}

class _EmojiPickerContent extends StatefulWidget {
  const _EmojiPickerContent({required this.state});
  final AppState state;

  @override
  State<_EmojiPickerContent> createState() => _EmojiPickerContentState();
}

class _EmojiPickerContentState extends State<_EmojiPickerContent> {
  final _search = TextEditingController();

  @override
  void initState() {
    super.initState();
    widget.state.ensureEmojiLoaded();
    _search.addListener(() => setState(() {}));
  }

  @override
  void dispose() {
    _search.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final state = widget.state;
    final query = _search.text.trim().toLowerCase();
    return AnimatedBuilder(
      animation: state,
      builder: (context, _) {
        final customs = state.customEmojis.values
            .where((e) => query.isEmpty || e.name.toLowerCase().contains(query))
            .toList()
          ..sort((a, b) => a.name.compareTo(b.name));
        return TcDialogShell(
          title: 'React',
          width: 340,
          actions: [
            _FooterButton(
              label: '+ IMPORT EMOJI',
              onTap: () async {
                await showEmojiImportDialog(context, state);
              },
            ),
          ],
          children: [
            TcTextField(
              label: 'Search',
              controller: _search,
              hintText: 'Search emojis…',
              autofocus: true,
            ),
            const SizedBox(height: 12),
            if (query.isEmpty) ...[
              Wrap(
                spacing: 4,
                runSpacing: 4,
                children: [
                  for (final e in _builtinEmoji)
                    _EmojiCell(
                      tooltip: e,
                      onTap: () => Navigator.pop(
                          context, EmojiSelection(reactionKey: e, composeToken: e)),
                      child: Text(e, style: const TextStyle(fontSize: 20)),
                    ),
                ],
              ),
              const SizedBox(height: 10),
              Container(height: 1, color: TCColors.borderSubtle),
              const SizedBox(height: 10),
            ],
            if (customs.isEmpty)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 10),
                child: Text(
                  query.isEmpty ? 'No custom emojis yet.' : 'No matches.',
                  style: TextStyle(fontSize: TCType.textCaption, color: TCColors.textTertiary),
                ),
              )
            else
              Container(
                constraints: const BoxConstraints(maxHeight: 220),
                child: SingleChildScrollView(
                  child: Wrap(
                    spacing: 4,
                    runSpacing: 4,
                    children: [
                      for (final e in customs)
                        _EmojiCell(
                          tooltip: ':${e.name}:',
                          onTap: () => Navigator.pop(
                            context,
                            EmojiSelection(
                              reactionKey: e.emojiHash,
                              composeToken: ':${e.name}@${e.emojiHash}:',
                            ),
                          ),
                          child: Image.memory(e.imageBytes,
                              width: 24, height: 24, filterQuality: FilterQuality.medium),
                        ),
                    ],
                  ),
                ),
              ),
          ],
        );
      },
    );
  }
}

class _EmojiCell extends StatefulWidget {
  const _EmojiCell({required this.child, required this.tooltip, required this.onTap});

  final Widget child;
  final String tooltip;
  final VoidCallback onTap;

  @override
  State<_EmojiCell> createState() => _EmojiCellState();
}

class _EmojiCellState extends State<_EmojiCell> {
  bool _hover = false;

  @override
  Widget build(BuildContext context) {
    return Tooltip(
      message: widget.tooltip,
      child: MouseRegion(
        cursor: SystemMouseCursors.click,
        onEnter: (_) => setState(() => _hover = true),
        onExit: (_) => setState(() => _hover = false),
        child: GestureDetector(
          onTap: widget.onTap,
          child: AnimatedContainer(
            duration: TCEffects.durationFast,
            width: 34,
            height: 34,
            alignment: Alignment.center,
            decoration: BoxDecoration(
              color: _hover ? TCColors.bgHover : Colors.transparent,
              border: Border.all(
                  color: _hover ? TCColors.borderStrong : Colors.transparent),
            ),
            child: widget.child,
          ),
        ),
      ),
    );
  }
}

class _FooterButton extends StatelessWidget {
  const _FooterButton({required this.label, required this.onTap});

  final String label;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: MouseRegion(
        cursor: SystemMouseCursors.click,
        child: Text(
          label,
          style: TextStyle(
            fontSize: TCType.textCaption,
            color: TCColors.textSecondary,
            letterSpacing: TCType.letterSpacingFor(TCType.textCaption, TCType.trackingWide),
          ),
        ),
      ),
    );
  }
}
