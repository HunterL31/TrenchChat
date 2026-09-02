/// Typed view of GET /network/map -- the topology gather_network_data()
/// assembles from the RNS path table and interface stats.
class NetworkMapData {
  const NetworkMapData({
    required this.nodes,
    required this.edges,
    required this.interfaces,
    required this.nodeCount,
    required this.pathCount,
    required this.interfaceCount,
    this.onlinePeerCount,
  });

  final List<MapNode> nodes;
  final List<MapEdge> edges;
  final List<MapInterface> interfaces;
  final int nodeCount;
  final int pathCount;
  final int interfaceCount;

  /// How many mapped peers presence says are online, or null on a backend
  /// that doesn't report it.
  final int? onlinePeerCount;

  factory NetworkMapData.fromJson(Map<String, dynamic> json) {
    final stats = json['stats'] as Map<String, dynamic>? ?? {};
    return NetworkMapData(
      nodes: (json['nodes'] as List<dynamic>? ?? [])
          .map((e) => MapNode.fromJson(e as Map<String, dynamic>))
          .toList(),
      edges: (json['edges'] as List<dynamic>? ?? [])
          .map((e) => MapEdge.fromJson(e as Map<String, dynamic>))
          .toList(),
      interfaces: (json['interfaces'] as List<dynamic>? ?? [])
          .map((e) => MapInterface.fromJson(e as Map<String, dynamic>))
          .toList(),
      nodeCount: stats['node_count'] as int? ?? 0,
      pathCount: stats['path_count'] as int? ?? 0,
      interfaceCount: stats['interface_count'] as int? ?? 0,
      onlinePeerCount: (stats['online_peer_count'] as num?)?.toInt(),
    );
  }
}

enum MapNodeKind { self, interface_, transport, peer, unknown }

class MapNode {
  const MapNode({
    required this.id,
    required this.label,
    required this.kind,
    required this.hops,
    required this.quality,
    required this.isTrenchChat,
    this.via,
    this.interfaceName,
    this.lastHeard,
    this.expires,
    this.rttMs,
    this.online,
    this.nomad = false,
    this.propagation = false,
    this.identityHex,
  });

  final String id;
  final String label;
  final MapNodeKind kind;
  final int hops;
  final int quality;

  /// True when the node is known to run TrenchChat (directory announce or a
  /// shared channel), not just any resolvable Reticulum identity.
  final bool isTrenchChat;

  /// Next-hop destination hex the path routes through; null when direct.
  final String? via;

  /// Name of the interface the path was learned through.
  final String? interfaceName;

  /// Unix seconds the path was learned, and when it expires.
  final double? lastHeard;
  final double? expires;

  /// Round-trip time of an established link, in milliseconds.
  final double? rttMs;

  /// Presence: true online, false offline, null unknown.
  final bool? online;

  /// The node announced itself as a Nomad Network node / an LXMF
  /// propagation node.
  final bool nomad;
  final bool propagation;

  /// The node's identity hash, which differs from [id] (a destination hash).
  final String? identityHex;

  factory MapNode.fromJson(Map<String, dynamic> json) {
    final kind = switch (json['kind'] as String?) {
      'self' => MapNodeKind.self,
      'interface' => MapNodeKind.interface_,
      'transport' => MapNodeKind.transport,
      'peer' => MapNodeKind.peer,
      _ => MapNodeKind.unknown,
    };
    return MapNode(
      id: json['id'] as String,
      // gather_network_data prefixes interface labels with an up/down dot
      // glyph the app fonts don't cover; the node marker shows kind anyway.
      label: (json['label'] as String? ?? '')
          .replaceFirst(RegExp(r'^[●○]\s*'), ''),
      kind: kind,
      hops: (json['hops'] as num?)?.toInt() ?? 0,
      quality: (json['quality'] as num?)?.toInt() ?? 0,
      // Backends that predate the flag get the old filter behavior.
      isTrenchChat: json['trenchchat'] as bool? ?? kind == MapNodeKind.peer,
      via: json['via'] as String?,
      interfaceName: json['interface'] as String?,
      lastHeard: (json['last_heard'] as num?)?.toDouble(),
      expires: (json['expires'] as num?)?.toDouble(),
      rttMs: (json['rtt_ms'] as num?)?.toDouble(),
      online: json['online'] as bool?,
      nomad: json['nomad'] as bool? ?? false,
      propagation: json['propagation'] as bool? ?? false,
      identityHex: json['identity_hex'] as String?,
    );
  }
}

class MapEdge {
  const MapEdge({
    required this.src,
    required this.dst,
    required this.direct,
    required this.quality,
    this.hops = 0,
    this.kind = 'path',
  });

  final String src;
  final String dst;
  final bool direct;
  final int quality;

  /// Hop count of the path this edge stands for.
  final int hops;

  /// 'interface' for a self-to-interface link, 'path' for a routed one.
  final String kind;

  factory MapEdge.fromJson(Map<String, dynamic> json) => MapEdge(
        src: json['src'] as String,
        dst: json['dst'] as String,
        direct: json['direct'] as bool? ?? false,
        quality: (json['quality'] as num?)?.toInt() ?? 0,
        hops: (json['hops'] as num?)?.toInt() ?? 0,
        kind: json['kind'] as String? ?? 'path',
      );
}

class MapInterface {
  const MapInterface({
    required this.name,
    required this.type,
    required this.status,
    required this.rxb,
    required this.txb,
    this.bitrate,
  });

  final String name;
  final String type;
  final bool status;
  final int rxb;
  final int txb;

  /// Configured bitrate in bits per second, when the interface reports one.
  final int? bitrate;

  factory MapInterface.fromJson(Map<String, dynamic> json) => MapInterface(
        name: json['name'] as String? ?? '?',
        type: json['type'] as String? ?? '',
        status: json['status'] as bool? ?? false,
        rxb: (json['rxb'] as num?)?.toInt() ?? 0,
        txb: (json['txb'] as num?)?.toInt() ?? 0,
        bitrate: (json['bitrate'] as num?)?.toInt(),
      );
}
