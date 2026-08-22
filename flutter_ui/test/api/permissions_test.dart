import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/models/permissions.dart';

void main() {
  test('fromJson reads send_message when present', () {
    final denied = ChannelPermissions.fromJson(const {'send_message': false});
    expect(denied.sendMessage, isFalse);

    final allowed = ChannelPermissions.fromJson(const {'send_message': true});
    expect(allowed.sendMessage, isTrue);
  });

  test('fromJson defaults send_message to true when the key is absent', () {
    final perms = ChannelPermissions.fromJson(const {});
    expect(perms.sendMessage, isTrue);
  });
}
