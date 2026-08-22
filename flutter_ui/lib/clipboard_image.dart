// Paste-to-attach, which reaches the two targets by different routes.
//
// On desktop the clipboard is something to read: Ctrl/Cmd+V is a key event,
// and the image is pulled out of the system pasteboard afterwards. On web it
// is not readable that way -- navigator.clipboard.read() needs a permission
// and a secure context, and the client is routinely served over plain http on
// a LAN or a tunnel. The browser's own paste event carries the image with it
// and needs neither, so that is what the web build listens to.
//
// Both routes fire for a paste anywhere in the app; it is the compose bar
// that decides whether it had focus.
export 'clipboard_image_io.dart'
    if (dart.library.js_interop) 'clipboard_image_web.dart';
