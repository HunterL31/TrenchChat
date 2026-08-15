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
  });

  final List<MapNode> nodes;
  final List<MapEdge> edges;
  final List<MapInterface> interfaces;
  final int nodeCount;
  final int pathCount;
  final int interfaceCount;

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
  });

  final String id;
  final String label;
  final MapNodeKind kind;
  final int hops;
  final int quality;

  factory MapNode.fromJson(Map<String, dynamic> json) => MapNode(
        id: json['id'] as String,
        // gather_network_data prefixes interface labels with an up/down dot
        // glyph the app fonts don't cover; the node marker shows kind anyway.
        label: (json['label'] as String? ?? '')
            .replaceFirst(RegExp(r'^[●○]\s*'), ''),
        kind: switch (json['kind'] as String?) {
          'self' => MapNodeKind.self,
          'interface' => MapNodeKind.interface_,
          'transport' => MapNodeKind.transport,
          'peer' => MapNodeKind.peer,
          _ => MapNodeKind.unknown,
        },
        hops: (json['hops'] as num?)?.toInt() ?? 0,
        quality: (json['quality'] as num?)?.toInt() ?? 0,
      );
}

class MapEdge {
  const MapEdge({required this.src, required this.dst, required this.direct});

  final String src;
  final String dst;
  final bool direct;

  factory MapEdge.fromJson(Map<String, dynamic> json) => MapEdge(
        src: json['src'] as String,
        dst: json['dst'] as String,
        direct: json['direct'] as bool? ?? false,
      );
}

class MapInterface {
  const MapInterface({
    required this.name,
    required this.type,
    required this.status,
    required this.rxb,
    required this.txb,
  });

  final String name;
  final String type;
  final bool status;
  final int rxb;
  final int txb;

  factory MapInterface.fromJson(Map<String, dynamic> json) => MapInterface(
        name: json['name'] as String? ?? '?',
        type: json['type'] as String? ?? '',
        status: json['status'] as bool? ?? false,
        rxb: (json['rxb'] as num?)?.toInt() ?? 0,
        txb: (json['txb'] as num?)?.toInt() ?? 0,
      );
}
