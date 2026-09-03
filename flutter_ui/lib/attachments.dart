// Picking an image or a file off the local machine for the compose bar to
// send, and handing a downloaded file back to the user's own filesystem.
//
// The size ceilings here are the API's, not the mesh's: api.py caps a whole
// request body at MAX_REQUEST_BYTES and base64 inflates by a third, so a
// larger source is refused before the upload rather than by it. An image is
// downscaled and re-encoded backend-side by prepare_image; a file is shared
// as a manifest and never travels inside the message at all.
import 'dart:typed_data';

import 'package:file_picker/file_picker.dart';

/// Largest source image offered to the backend, sized to fit base64-encoded
/// inside api.py's request-body cap.
const int maxAttachmentBytes = 3 * 1024 * 1024;

/// Largest file offered to the backend, matching MAX_SHARED_FILE_BYTES.
/// AppState reads the backend's own figure from GET /files/usage and passes
/// it in; this is what holds until it answers.
const int maxFileAttachmentBytes = 5 * 1024 * 1024;

/// Which of the two attachment paths a pick belongs to. An image is rendered
/// in the transcript and rides inside the message; a file is shared as a
/// manifest and fetched on request.
enum AttachmentKind { image, file }

/// Something the user picked but has not sent yet.
class PickedAttachment {
  const PickedAttachment({
    required this.name,
    required this.bytes,
    this.kind = AttachmentKind.image,
  });

  final String name;
  final Uint8List bytes;
  final AttachmentKind kind;

  bool get isFile => kind == AttachmentKind.file;
}

/// What the picker came back with: at most one of these is non-null. Both
/// null means the user cancelled, which is not an error.
typedef AttachmentPick = ({PickedAttachment? attachment, String? error});

/// Writes [bytes] wherever the user chooses to put them. Injected into
/// AppState so widget tests never reach the plugin.
typedef FileSaver = Future<void> Function(
    String fileName, Uint8List bytes, String mimeType);

/// The refusal shown for anything over [maxAttachmentBytes], wherever the
/// image came from.
String get tooLargeMessage =>
    'That image is too large to send '
    '(limit ${maxAttachmentBytes ~/ (1024 * 1024)} MB).';

/// The refusal shown for a file over the share ceiling in force.
String fileTooLargeMessage(int maxBytes) =>
    'That file is too large to share (limit ${maxBytes ~/ (1024 * 1024)} MB).';

/// Wraps bytes that arrived whole -- a paste, rather than a file the picker
/// could measure before reading -- in the same size gate the picker applies.
AttachmentPick attachmentFromBytes(String name, Uint8List bytes) {
  if (bytes.isEmpty) return (attachment: null, error: 'That image is empty.');
  if (bytes.length > maxAttachmentBytes) {
    return (attachment: null, error: tooLargeMessage);
  }
  return (attachment: PickedAttachment(name: name, bytes: bytes), error: null);
}

/// Opens the platform's file dialog on an image filter.
///
/// [pickFile] is injectable so widget tests never reach the plugin's method
/// channel.
Future<AttachmentPick> pickImageAttachment({
  Future<PlatformFile?> Function()? pickFile,
}) async {
  return _pick(
    pickFile: pickFile ??
        () => FilePicker.pickFile(
              dialogTitle: 'Attach an image',
              type: FileType.image,
            ),
    kind: AttachmentKind.image,
    maxBytes: maxAttachmentBytes,
    tooLarge: tooLargeMessage,
  );
}

/// Opens the platform's file dialog on no filter at all, for a file shared as
/// a manifest. [maxBytes] is the ceiling in force, which the backend reports
/// and AppState passes in.
Future<AttachmentPick> pickFileAttachment({
  Future<PlatformFile?> Function()? pickFile,
  int maxBytes = maxFileAttachmentBytes,
}) async {
  return _pick(
    pickFile: pickFile ??
        () => FilePicker.pickFile(
              dialogTitle: 'Attach a file',
              type: FileType.any,
            ),
    kind: AttachmentKind.file,
    maxBytes: maxBytes,
    tooLarge: fileTooLargeMessage(maxBytes),
  );
}

Future<AttachmentPick> _pick({
  required Future<PlatformFile?> Function() pickFile,
  required AttachmentKind kind,
  required int maxBytes,
  required String tooLarge,
}) async {
  final PlatformFile? file;
  try {
    file = await pickFile();
  } catch (e) {
    return (attachment: null, error: 'Could not open the file picker: $e');
  }
  if (file == null) return (attachment: null, error: null);

  try {
    // Checked before the read so an enormous pick is refused rather than
    // pulled into memory to be refused.
    if (await file.length() > maxBytes) {
      return (attachment: null, error: tooLarge);
    }
    final bytes = await file.readAsBytes();
    if (bytes.isEmpty) {
      return (attachment: null, error: 'That file is empty.');
    }
    return (
      attachment: PickedAttachment(name: file.name, bytes: bytes, kind: kind),
      error: null,
    );
  } catch (e) {
    return (attachment: null, error: 'Could not read that file: $e');
  }
}

const Map<String, String> _mimeTypes = {
  'txt': 'text/plain',
  'md': 'text/markdown',
  'csv': 'text/csv',
  'json': 'application/json',
  'xml': 'application/xml',
  'html': 'text/html',
  'pdf': 'application/pdf',
  'zip': 'application/zip',
  'gz': 'application/gzip',
  'tar': 'application/x-tar',
  '7z': 'application/x-7z-compressed',
  'png': 'image/png',
  'jpg': 'image/jpeg',
  'jpeg': 'image/jpeg',
  'gif': 'image/gif',
  'webp': 'image/webp',
  'svg': 'image/svg+xml',
  'mp3': 'audio/mpeg',
  'ogg': 'audio/ogg',
  'opus': 'audio/opus',
  'wav': 'audio/wav',
  'flac': 'audio/flac',
  'mp4': 'video/mp4',
  'webm': 'video/webm',
};

/// The MIME type a name's extension suggests. Peer bytes are never trusted to
/// declare their own type, so an unknown extension stays the opaque
/// application/octet-stream the backend served them as.
String guessMimeType(String fileName) {
  final dot = fileName.lastIndexOf('.');
  if (dot < 0 || dot == fileName.length - 1) return 'application/octet-stream';
  final ext = fileName.substring(dot + 1).toLowerCase();
  return _mimeTypes[ext] ?? 'application/octet-stream';
}

/// The real save path for both targets: on web the plugin wraps the bytes in a
/// blob the browser downloads, on desktop it opens the native save dialog and
/// writes them to the chosen path.
Future<void> saveBytesToFile(
    String fileName, Uint8List bytes, String mimeType) async {
  await FilePicker.saveFile(fileName: fileName, bytes: bytes, mimeType: mimeType);
}
