// Emoji import -- port of emoji_picker.py's EmojiImportDialog. The Qt dialog
// uses a native file picker; this spike takes a typed path instead (desktop
// target, no file-picker plugin) and reads it directly.
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter/material.dart';

import '../../app_state.dart';
import '../../theme/section_theme.dart';
import '../../theme/theme_spec.dart';
import '../../theme/tokens.dart';
import '../../widgets/peer_image.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_dialog.dart';
import '../../widgets/tc_text_field.dart';

/// Mirror of trenchchat/core/reaction.py's MAX_EMOJI_BYTES mesh-friendly cap.
const int maxEmojiBytes = 65536;

Future<void> showEmojiImportDialog(BuildContext context, AppState state) {
  return showTcDialog<void>(
    context: context,
    builder: (context) => SectionTheme(
      spec: state.themeSpec,
      section: TCSection.dialogs,
      child: _EmojiImportContent(state: state),
    ),
  );
}

class _EmojiImportContent extends StatefulWidget {
  const _EmojiImportContent({required this.state});
  final AppState state;

  @override
  State<_EmojiImportContent> createState() => _EmojiImportContentState();
}

class _EmojiImportContentState extends State<_EmojiImportContent> {
  final _path = TextEditingController();
  final _name = TextEditingController();

  Uint8List? _imageBytes;
  String? _error;
  bool _busy = false;

  @override
  void dispose() {
    _path.dispose();
    _name.dispose();
    super.dispose();
  }

  Future<void> _loadFile() async {
    final path = _path.text.trim();
    if (path.isEmpty) return;
    try {
      final bytes = await File(path).readAsBytes();
      if (bytes.length > maxEmojiBytes) {
        setState(() {
          _imageBytes = null;
          _error = 'Emoji image must be under ${maxEmojiBytes ~/ 1024} KB '
              '(file is ${bytes.length ~/ 1024} KB).';
        });
        return;
      }
      setState(() {
        _imageBytes = bytes;
        _error = null;
        if (_name.text.trim().isEmpty) {
          final base = path.split(Platform.pathSeparator).last;
          final dot = base.lastIndexOf('.');
          _name.text = (dot > 0 ? base.substring(0, dot) : base)
              .toLowerCase()
              .replaceAll(' ', '_');
        }
      });
    } on FileSystemException catch (e) {
      setState(() {
        _imageBytes = null;
        _error = 'Could not read file: ${e.message}';
      });
    }
  }

  Future<void> _submit() async {
    final name = _name.text.trim();
    if (name.isEmpty) {
      setState(() => _error = 'Enter a short name for the emoji.');
      return;
    }
    final bytes = _imageBytes;
    if (bytes == null) {
      setState(() => _error = 'Load an image file first.');
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    final ok = await widget.state.importEmoji(name, base64Encode(bytes));
    if (!mounted) return;
    if (!ok) {
      setState(() {
        _busy = false;
        _error = widget.state.actionError ?? 'Could not import the emoji.';
      });
      return;
    }
    Navigator.pop(context);
  }

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    return TcDialogShell(
      title: 'Import Emoji',
      width: 360,
      errorText: _error,
      actions: [
        TcGhostButton(label: 'CANCEL', onPressed: () => Navigator.pop(context)),
        TcPrimaryButton(
          label: _busy ? 'IMPORTING…' : 'IMPORT',
          onPressed: _busy ? null : _submit,
        ),
      ],
      children: [
        TcTextField(
          label: 'Image file path',
          controller: _path,
          hintText: '/path/to/emoji.png',
          autofocus: true,
          onSubmitted: (_) => _loadFile(),
        ),
        const SizedBox(height: 8),
        Align(
          alignment: Alignment.centerLeft,
          child: TcGhostButton(label: 'LOAD', onPressed: _loadFile),
        ),
        const SizedBox(height: 10),
        Center(
          child: Container(
            width: 64,
            height: 64,
            alignment: Alignment.center,
            decoration: BoxDecoration(
              color: tc.bgInset,
              border: Border.all(color: tc.borderDefault),
            ),
            child: _imageBytes != null
                ? peerImage(_imageBytes!, size: 56)
                : Text(
                    'NO IMAGE',
                    style: TextStyle(
                        fontSize: TCType.textMicro, color: tc.textTertiary),
                  ),
          ),
        ),
        const SizedBox(height: 10),
        TcTextField(
          label: 'Short name (e.g. salute)',
          controller: _name,
          hintText: 'emoji_name',
        ),
      ],
    );
  }
}
