// The size gate every attachment goes through, whichever way it arrived.
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:flutter_ui/attachments.dart';

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
}
