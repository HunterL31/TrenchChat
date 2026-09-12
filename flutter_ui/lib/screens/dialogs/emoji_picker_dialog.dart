// Emoji picker -- port of emoji_picker.py's EmojiPicker popup: search box,
// emoji grid, import footer. Presented as a compact dialog rather than an
// anchored popup. Reactions key on the raw string (a unicode char for
// built-ins, the SHA-256 hash for customs), so both grids return one
// EmojiSelection the caller can react or compose with.
import 'package:flutter/material.dart';

import '../../app_state.dart';
import '../../theme/effects.dart';
import '../../theme/section_theme.dart';
import '../../theme/shape.dart';
import '../../theme/theme_spec.dart';
import '../../theme/tokens.dart';
import '../../widgets/peer_image.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_dialog.dart';
import '../../widgets/tc_icon.dart';
import '../../widgets/tc_text_field.dart';
import '../../widgets/tc_tooltip.dart';
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

Future<EmojiSelection?> showEmojiPickerDialog(BuildContext context, AppState state,
    {String title = 'React'}) {
  return showTcDialog<EmojiSelection>(
    context: context,
    builder: (context) => SectionTheme(
      spec: state.themeSpec,
      section: TCSection.dialogs,
      child: _EmojiPickerContent(state: state, title: title),
    ),
  );
}

class _EmojiPickerContent extends StatefulWidget {
  const _EmojiPickerContent({required this.state, required this.title});
  final AppState state;
  final String title;

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
    final tc = SectionTheme.of(context);
    final query = _search.text.trim().toLowerCase();
    return AnimatedBuilder(
      animation: state,
      builder: (context, _) {
        final customs = state.customEmojis.values
            .where((e) => query.isEmpty || e.name.toLowerCase().contains(query))
            .toList()
          ..sort((a, b) => a.name.compareTo(b.name));
        return TcDialogShell(
          title: widget.title,
          width: _pickerWidth,
          actions: [
            TcGhostButton(
              icon: TcIcons.plus,
              label: 'IMPORT EMOJI',
              onPressed: () async {
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
                      child: Text(e, style: const TextStyle(fontSize: _glyphSize)),
                    ),
                ],
              ),
              const SizedBox(height: 10),
              Container(height: 1, color: tc.borderSubtle),
              const SizedBox(height: TCSpace.space4),
            ],
            if (customs.isEmpty)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 10),
                child: Text(
                  query.isEmpty ? 'No custom emojis yet.' : 'No matches.',
                  style: TextStyle(fontSize: TCType.textCaption, color: tc.textTertiary),
                ),
              )
            else
              Container(
                constraints: const BoxConstraints(maxHeight: 220),
                child: SingleChildScrollView(
                  padding: EdgeInsets.only(right: scrollbarInset(context)),
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
                          child: peerImage(e.imageBytes, size: _glyphSize),
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

/// Wide enough that the eight-cell grid survives the scrollbar inset instead
/// of reflowing to seven.
const double _pickerWidth = 380;

/// One glyph size for a built-in and a custom emoji, so their cells' contents
/// share a left edge.
const double _glyphSize = 22;

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
    final tc = SectionTheme.of(context);
    return TcTooltip(
      message: widget.tooltip,
      child: MouseRegion(
        cursor: SystemMouseCursors.click,
        onEnter: (_) => setState(() => _hover = true),
        onExit: (_) => setState(() => _hover = false),
        child: GestureDetector(
          onTap: widget.onTap,
          child: AnimatedContainer(
            duration: TCEffects.durationFast,
            curve: TCEffects.easeTerminal,
            width: 34,
            height: 34,
            alignment: Alignment.center,
            decoration: BoxDecoration(
              color: _hover ? tc.bgHover : Colors.transparent,
              border: Border.all(
                  color: _hover ? tc.borderStrong : Colors.transparent),
              borderRadius: tcCorners(context, scale: 0.5),
            ),
            child: widget.child,
          ),
        ),
      ),
    );
  }
}


