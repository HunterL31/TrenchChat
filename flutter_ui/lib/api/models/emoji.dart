import 'dart:convert';
import 'dart:typed_data';

/// One entry of GET /emoji -- a custom emoji in the local library.
class CustomEmoji {
  const CustomEmoji({required this.emojiHash, required this.name, required this.imageBytes});

  final String emojiHash;
  final String name;
  final Uint8List imageBytes;

  factory CustomEmoji.fromJson(Map<String, dynamic> json) => CustomEmoji(
        emojiHash: json['emoji_hash'] as String,
        name: json['name'] as String? ?? '',
        imageBytes: base64Decode(json['image_data_b64'] as String? ?? ''),
      );
}

/// Role-permission matrix from GET /channels|servers/{h}/permissions.
class ScopePermissions {
  const ScopePermissions({
    required this.allPermissions,
    required this.grantable,
    required this.admin,
    required this.member,
  });

  final List<String> allPermissions;

  /// What each role may actually be granted on this scope. The core drops
  /// anything outside it on read and on write, so offering a control for one
  /// would show a checkbox that silently does nothing.
  final Map<String, List<String>> grantable;

  final List<String> admin;
  final List<String> member;

  /// Offered checkboxes for [role], falling back to the full list for a
  /// backend that predates the field.
  List<String> grantableFor(String role) =>
      grantable[role] ?? allPermissions;

  factory ScopePermissions.fromJson(Map<String, dynamic> json) => ScopePermissions(
        allPermissions: (json['all_permissions'] as List<dynamic>? ?? []).cast<String>(),
        grantable: ((json['grantable'] as Map<String, dynamic>?) ?? {}).map(
          (role, perms) =>
              MapEntry(role, (perms as List<dynamic>? ?? []).cast<String>()),
        ),
        admin: (json['admin'] as List<dynamic>? ?? []).cast<String>(),
        member: (json['member'] as List<dynamic>? ?? []).cast<String>(),
      );
}
