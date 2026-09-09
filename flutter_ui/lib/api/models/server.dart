class Server {
  const Server({
    required this.hash,
    required this.name,
    required this.description,
    required this.creatorHash,
    required this.createdAt,
  });

  final String hash;
  final String name;
  final String description;
  final String creatorHash;
  final double createdAt;

  factory Server.fromJson(Map<String, dynamic> json) => Server(
        hash: json['hash'] as String,
        name: json['name'] as String,
        description: json['description'] as String? ?? '',
        creatorHash: json['creator_hash'] as String,
        createdAt: (json['created_at'] as num).toDouble(),
      );
}

class Channel {
  const Channel({
    required this.hash,
    required this.name,
    required this.description,
    required this.creatorHash,
    required this.createdAt,
    required this.serverHash,
  });

  final String hash;
  final String name;
  final String description;
  final String creatorHash;
  final double createdAt;
  final String? serverHash;


  factory Channel.fromJson(Map<String, dynamic> json) => Channel(
        hash: json['hash'] as String,
        name: json['name'] as String,
        description: json['description'] as String? ?? '',
        creatorHash: json['creator_hash'] as String,
        createdAt: (json['created_at'] as num).toDouble(),
        serverHash: json['server_hash'] as String?,
      );
}
