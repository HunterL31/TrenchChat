// Web paste: the browser's own paste event, which carries the image with it.
import 'dart:js_interop';

import 'package:flutter/foundation.dart';
import 'package:web/web.dart' as web;

import 'clipboard_paste.dart';

/// Fallback name for a pasted image the browser did not name. A screenshot
/// pasted from the system clipboard usually arrives as an unnamed blob.
const String pastedImageName = 'pasted-image.png';

/// Listens for a paste anywhere in the document and hands [onImage] the first
/// image it carries. Returns the disposer that stops listening.
VoidCallback watchClipboardImagePaste(void Function(PastedImage) onImage) {
  void listener(web.Event event) {
    final transfer = (event as web.ClipboardEvent).clipboardData;
    if (transfer == null) return;
    final items = transfer.items;
    for (var i = 0; i < items.length; i++) {
      final item = items[i];
      if (!item.type.startsWith('image/')) continue;
      final file = item.getAsFile();
      if (file == null) continue;
      _deliver(file, onImage);
      return;
    }
  }

  final callback = listener.toJS;
  web.document.addEventListener('paste', callback);
  return () => web.document.removeEventListener('paste', callback);
}

Future<void> _deliver(web.File file, void Function(PastedImage) onImage) async {
  final Uint8List bytes;
  try {
    final buffer = await file.arrayBuffer().toDart;
    bytes = buffer.toDart.asUint8List();
  } catch (_) {
    return; // A blob that will not read is not a failure worth reporting.
  }
  if (bytes.isEmpty) return;
  final name = file.name.isEmpty ? pastedImageName : file.name;
  onImage((name: name, bytes: bytes));
}
