import 'dart:ui';

import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/models/network_map.dart';
import 'package:flutter_ui/screens/main_window/map_tab.dart';

NetworkMapData _data() => NetworkMapData.fromJson({
      'nodes': [
        {'id': 'self', 'label': 'This device', 'kind': 'self', 'hops': 0},
        {'id': '__iface__Hub', 'label': 'Hub', 'kind': 'interface', 'hops': 0},
        {'id': 'peer-a', 'label': 'peer-a', 'kind': 'peer', 'hops': 1},
        {'id': 'peer-b', 'label': 'peer-b', 'kind': 'peer', 'hops': 1},
        {'id': 'far-peer', 'label': 'far-peer', 'kind': 'peer', 'hops': 3},
        {'id': 'relay', 'label': 'relay', 'kind': 'transport', 'hops': 1},
      ],
      'edges': [
        {'src': 'self', 'dst': '__iface__Hub', 'hops': 0, 'direct': true},
        {'src': 'self', 'dst': 'peer-a', 'hops': 1, 'direct': true},
        {'src': 'relay', 'dst': 'far-peer', 'hops': 3, 'direct': false},
      ],
      'interfaces': [
        {'name': 'Hub', 'type': 'TCPClientInterface', 'status': true, 'rxb': 1, 'txb': 2},
      ],
      'stats': {'node_count': 6, 'path_count': 4, 'interface_count': 1},
    });

void main() {
  const size = Size(800, 600);
  final center = Offset(size.width / 2, size.height / 2);

  test('parses nodes, edges, and stats from the gather_network_data shape', () {
    final data = _data();
    expect(data.nodes, hasLength(6));
    expect(data.nodes.first.kind, MapNodeKind.self);
    expect(data.edges, hasLength(3));
    expect(data.edges.first.direct, isTrue);
    expect(data.nodeCount, 6);
    expect(data.pathCount, 4);
    expect(data.interfaceCount, 1);
  });

  test('self sits at the center and every node gets a position', () {
    final positions = layoutMapNodes(_data(), size);
    expect(positions['self'], center);
    expect(positions, hasLength(6));
  });

  test('interfaces sit closer to the center than peers, and rings grow with hops', () {
    final positions = layoutMapNodes(_data(), size);
    double distance(String id) => (positions[id]! - center).distance;

    expect(distance('__iface__Hub'), lessThan(distance('peer-a')));
    expect(distance('peer-a'), closeTo(distance('peer-b'), 1e-6));
    expect(distance('far-peer'), greaterThan(distance('peer-a')));
  });

  test('layout is deterministic across calls', () {
    expect(layoutMapNodes(_data(), size), layoutMapNodes(_data(), size));
  });
}
