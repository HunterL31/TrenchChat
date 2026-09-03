// The size gate every attachment goes through, whichever way it arrived.
import 'dart:typed_data';

import 'package:file_picker/file_picker.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:flutter_ui/attachments.dart';

/// A pick the plugin never sees. [length] is what the size gate reads, and
/// [reads] records whether the bytes were pulled into memory, which an
/// oversized pick must never cause.
final class _FakePick extends PlatformFile {
  _FakePick({required this.name, required this.byteLength, required this.data});

  @override
  final String name;

  final int byteLength;
  final Uint8List data;
  var reads = 0;

  @override
  Future<int> length() async => byteLength;

  @override
  Future<Uint8List> readAsBytes() async {
    reads++;
    return data;
  }

  // The gate uses name, length and readAsBytes only; the rest of the
  // interface is forwarded so this fake stays as small as what it stands in
  // for.
  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}


void main() {
  test('bytes within the limit become an attachment', () {
    final result = attachmentFromBytes('shot.png', Uint8List(1024));
    expect(result.error, isNull);
    expect(result.attachment?.name, 'shot.png');
    expect(result.attachment?.bytes, hasLength(1024));
  });

  test('bytes over the limit are refused, not truncated', () {
    final result =
        attachmentFromBytes('huge.png', Uint8List(maxAttachmentBytes + 1));
    expect(result.attachment, isNull);
    expect(result.error, contains('too large'));
  });

  test('the limit itself is accepted', () {
    final result =
        attachmentFromBytes('exact.png', Uint8List(maxAttachmentBytes));
    expect(result.attachment, isNotNull);
    expect(result.error, isNull);
  });

  test('empty bytes are refused', () {
    final result = attachmentFromBytes('empty.png', Uint8List(0));
    expect(result.attachment, isNull);
    expect(result.error, isNotNull);
  });

  group('the file picker', () {
    test('a file within the ceiling is picked as a file, not an image', () async {
      final pick =
          _FakePick(name: 'notes.bin', byteLength: 2048, data: Uint8List(2048));

      final result = await pickFileAttachment(pickFile: () async => pick);

      expect(result.error, isNull);
      expect(result.attachment?.name, 'notes.bin');
      expect(result.attachment?.kind, AttachmentKind.file);
      expect(result.attachment?.bytes, hasLength(2048));
    });

    test('an oversized file is refused before its bytes are read', () async {
      final pick = _FakePick(
          name: 'huge.bin',
          byteLength: maxFileAttachmentBytes + 1,
          data: Uint8List(0));

      final result = await pickFileAttachment(pickFile: () async => pick);

      expect(result.attachment, isNull);
      expect(result.error, contains('too large'));
      expect(pick.reads, 0);
    });

    test('the ceiling the backend reports is the one applied', () async {
      final pick =
          _FakePick(name: 'notes.bin', byteLength: 4096, data: Uint8List(4096));

      final result =
          await pickFileAttachment(pickFile: () async => pick, maxBytes: 1024);

      expect(result.attachment, isNull);
      expect(result.error, contains('limit'));
      expect(pick.reads, 0);
    });

    test('a cancelled pick is not an error', () async {
      final result = await pickFileAttachment(pickFile: () async => null);

      expect(result.attachment, isNull);
      expect(result.error, isNull);
    });

    test('an empty file is refused', () async {
      final pick =
          _FakePick(name: 'empty.bin', byteLength: 0, data: Uint8List(0));

      final result = await pickFileAttachment(pickFile: () async => pick);

      expect(result.attachment, isNull);
      expect(result.error, isNotNull);
    });

    test('an image pick still reads as an image', () async {
      final pick =
          _FakePick(name: 'shot.png', byteLength: 16, data: Uint8List(16));

      final result = await pickImageAttachment(pickFile: () async => pick);

      expect(result.attachment?.kind, AttachmentKind.image);
    });
  });

  group('guessMimeType', () {
    test('names a known extension', () {
      expect(guessMimeType('notes.json'), 'application/json');
      expect(guessMimeType('MAP.PNG'), 'image/png');
      expect(guessMimeType('archive.tar.gz'), 'application/gzip');
    });

    test('anything else stays opaque', () {
      expect(guessMimeType('notes.bin'), 'application/octet-stream');
      expect(guessMimeType('README'), 'application/octet-stream');
      expect(guessMimeType('trailing.'), 'application/octet-stream');
    });
  });
}
