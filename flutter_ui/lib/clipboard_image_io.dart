// Desktop paste: a key handler, then a clipboard read.
import 'package:flutter/services.dart';
import 'package:pasteboard/pasteboard.dart';

import 'clipboard_paste.dart';

/// What a pasted image is called when it reaches the compose bar. The system
/// pasteboard carries bytes with no name of their own.
const String pastedImageName = 'pasted-image.png';

/// Watches for Ctrl/Cmd+V and hands [onImage] whatever image the clipboard
/// held. Returns the disposer that stops watching.
///
/// The key event is never swallowed: whether the clipboard holds an image is
/// only known after an async read, and a paste of ordinary text has to reach
/// the field either way.
VoidCallback watchClipboardImagePaste(void Function(PastedImage) onImage) {
  bool handler(KeyEvent event) {
    if (event is! KeyDownEvent || event.logicalKey != LogicalKeyboardKey.keyV) {
      return false;
    }
    final keyboard = HardwareKeyboard.instance;
    if (!keyboard.isControlPressed && !keyboard.isMetaPressed) return false;
    _deliverClipboardImage(onImage);
    return false;
  }

  HardwareKeyboard.instance.addHandler(handler);
  return () => HardwareKeyboard.instance.removeHandler(handler);
}

Future<void> _deliverClipboardImage(void Function(PastedImage) onImage) async {
  final Uint8List? bytes;
  try {
    bytes = await Pasteboard.image;
  } catch (_) {
    return; // A clipboard holding something unreadable is not a failure.
  }
  if (bytes == null || bytes.isEmpty) return;
  onImage((name: pastedImageName, bytes: bytes));
}
