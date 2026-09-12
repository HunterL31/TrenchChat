// Renaming a bookmark. A bookmark's label is the only part of it the user
// writes -- the node and path are fixed by where it points -- so this is one
// field and nothing else.
import 'package:flutter/material.dart';

import '../../app_state.dart';
import '../../theme/section_theme.dart';
import '../../theme/theme_spec.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_dialog.dart';
import '../../widgets/tc_text_field.dart';

/// Asks for a new label, prefilled with [current]. Null when dismissed;
/// an empty string is a real answer, and falls back to the path on display.
Future<String?> showRenameBookmarkDialog(
  BuildContext context,
  AppState state, {
  required String current,
}) {
  return showTcDialog<String>(
    context: context,
    builder: (context) => SectionTheme(
      spec: state.themeSpec,
      section: TCSection.dialogs,
      child: _RenameBookmarkContent(current: current),
    ),
  );
}

class _RenameBookmarkContent extends StatefulWidget {
  const _RenameBookmarkContent({required this.current});

  final String current;

  @override
  State<_RenameBookmarkContent> createState() => _RenameBookmarkContentState();
}

class _RenameBookmarkContentState extends State<_RenameBookmarkContent> {
  late final _controller = TextEditingController(text: widget.current);

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return TcDialogShell(
      title: 'Rename Bookmark',
      actions: [
        TcGhostButton(label: 'CANCEL', onPressed: () => Navigator.pop(context)),
        TcPrimaryButton(
          label: 'SAVE',
          onPressed: () => Navigator.pop(context, _controller.text),
        ),
      ],
      children: [
        TcTextField(
          label: 'Name',
          controller: _controller,
          hintText: 'what to call this page',
          autofocus: true,
          onSubmitted: (value) => Navigator.pop(context, value),
        ),
      ],
    );
  }
}
