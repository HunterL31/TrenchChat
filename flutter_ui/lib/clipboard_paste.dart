// The one type both paste routes deliver, kept apart from either so each can
// import it without importing the other's platform library.
import 'dart:typed_data';

/// An image the user pasted, before it has been through the size gate.
typedef PastedImage = ({String name, Uint8List bytes});
