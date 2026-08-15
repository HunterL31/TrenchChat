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
    required this.admin,
    required this.member,
  });

  final List<String> allPermissions;
  final List<String> admin;
  final List<String> member;

  factory ScopePermissions.fromJson(Map<String, dynamic> json) => ScopePermissions(
        allPermissions: (json['all_permissions'] as List<dynamic>? ?? []).cast<String>(),
        admin: (json['admin'] as List<dynamic>? ?? []).cast<String>(),
        member: (json['member'] as List<dynamic>? ?? []).cast<String>(),
      );
}
