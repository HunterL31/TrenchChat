// Picking an image off the local machine for the compose bar to send.
//
// The size ceiling here is the API's, not the mesh's: api.py caps a whole
// request body at MAX_REQUEST_BYTES (4 MB) and base64 inflates by a third, so
// a larger source is refused before the upload rather than by it. What the
// mesh will carry is a separate, much smaller limit that prepare_image
// enforces backend-side by downscaling and re-encoding.
import 'dart:typed_data';

import 'package:file_picker/file_picker.dart';

/// Largest source file offered to the backend, sized to fit base64-encoded
/// inside api.py's 4 MB request-body cap.
const int maxAttachmentBytes = 3 * 1024 * 1024;

/// An image the user picked but has not sent yet.
class PickedAttachment {
  const PickedAttachment({required this.name, required this.bytes});

  final String name;
  final Uint8List bytes;
}

/// What the picker came back with: at most one of these is non-null. Both
/// null means the user cancelled, which is not an error.
typedef AttachmentPick = ({PickedAttachment? attachment, String? error});

/// Opens the platform's file dialog on an image filter.
///
/// [pickFile] is injectable so widget tests never reach the plugin's method
/// channel.
Future<AttachmentPick> pickImageAttachment({
  Future<PlatformFile?> Function()? pickFile,
}) async {
  final PlatformFile? file;
  try {
    file = await (pickFile ??
        () => FilePicker.pickFile(
              dialogTitle: 'Attach an image',
              type: FileType.image,
            ))();
  } catch (e) {
    return (attachment: null, error: 'Could not open the file picker: $e');
  }
  if (file == null) return (attachment: null, error: null);

  try {
    // Checked before the read so an enormous pick is refused rather than
    // pulled into memory to be refused.
    if (await file.length() > maxAttachmentBytes) {
      return (
        attachment: null,
        error: 'That image is too large to send '
            '(limit ${maxAttachmentBytes ~/ (1024 * 1024)} MB).',
      );
    }
    final bytes = await file.readAsBytes();
    if (bytes.isEmpty) {
      return (attachment: null, error: 'That file is empty.');
    }
    return (
      attachment: PickedAttachment(name: file.name, bytes: bytes),
      error: null,
    );
  } catch (e) {
    return (attachment: null, error: 'Could not read that file: $e');
  }
}
