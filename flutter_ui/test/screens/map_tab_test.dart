import 'dart:math' as math;

import 'package:flutter/painting.dart';
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

  test('self sits at the layout center and every node gets a position', () {
    final layout = layoutMapNodes(_data());
    expect(layout.positions['self'], layout.center);
    expect(layout.positions, hasLength(6));
    expect(layout.labels, hasLength(6));
  });

  test('interfaces sit closer to the center than peers, and rings grow with hops', () {
    final layout = layoutMapNodes(_data());
    double distance(String id) => (layout.positions[id]! - layout.center).distance;

    expect(distance('__iface__Hub'), lessThan(distance('peer-a')));
    expect(distance('peer-a'), closeTo(distance('peer-b'), 1e-6));
    expect(distance('far-peer'), greaterThan(distance('peer-a')));
  });

  test('layout is deterministic across calls', () {
    final first = layoutMapNodes(_data());
    final second = layoutMapNodes(_data());
    expect(first.positions, second.positions);
    expect(first.size, second.size);
  });

  test('label boxes never overlap', () {
    final labels = layoutMapNodes(_data()).labels.entries.toList();
    for (var i = 0; i < labels.length; i++) {
      for (var j = i + 1; j < labels.length; j++) {
        expect(labels[i].value.rect.overlaps(labels[j].value.rect), isFalse,
            reason: '${labels[i].key} overlaps ${labels[j].key}');
      }
    }
  });

  test('a multi-hop peer lands in its relay sector, keeping the edge radial', () {
    final layout = layoutMapNodes(_data());
    double angleOf(String id) {
      final v = layout.positions[id]! - layout.center;
      return math.atan2(v.dy, v.dx);
    }

    expect(angleOf('far-peer'), closeTo(angleOf('relay'), 1e-6));
  });

  test('a distant outlier does not squeeze the inner rings', () {
    final data = NetworkMapData.fromJson({
      'nodes': [
        {'id': 'self', 'label': 'This device', 'kind': 'self', 'hops': 0},
        {'id': '__iface__Hub', 'label': 'Hub', 'kind': 'interface', 'hops': 0},
        {'id': 'far', 'label': 'far', 'kind': 'peer', 'hops': 6},
      ],
      'edges': [
        {'src': 'self', 'dst': '__iface__Hub', 'hops': 0, 'direct': true},
        {'src': 'self', 'dst': 'far', 'hops': 6, 'direct': false},
      ],
      'interfaces': [],
      'stats': {'node_count': 3, 'path_count': 1, 'interface_count': 1},
    });
    final layout = layoutMapNodes(data);
    double distance(String id) => (layout.positions[id]! - layout.center).distance;

    // The hop-6 peer occupies the next ring out, not six rings out.
    expect(distance('__iface__Hub'), greaterThan(60));
    expect(distance('far'), lessThan(distance('__iface__Hub') * 3));
  });

  test('the peers-only filter keeps self and peers, drops infrastructure', () {
    final byId = {for (final n in _data().nodes) n.id: n};
    expect(isPeerNode(byId['self']!), isTrue);
    expect(isPeerNode(byId['peer-a']!), isTrue);
    expect(isPeerNode(byId['__iface__Hub']!), isFalse);
    expect(isPeerNode(byId['relay']!), isFalse);
  });

  test('quality tiers map to distinct colors, unknown included', () {
    final colors = [4, 3, 2, 1, 0].map(mapQualityColor).toSet();
    expect(colors, hasLength(5));
  });

  test('a TrenchChat peer paints filled, any other Reticulum node outlined', () {
    MapNode peer({required bool trenchchat}) => MapNode.fromJson(
        {'id': 'p', 'label': 'p', 'kind': 'peer', 'hops': 1, 'trenchchat': trenchchat});

    expect(mapPeerStyle(peer(trenchchat: true)), PaintingStyle.fill);
    expect(mapPeerStyle(peer(trenchchat: false)), PaintingStyle.stroke);
  });

  test('only a TrenchChat transport gets the accent dot', () {
    MapNode node(String kind, {required bool trenchchat}) => MapNode.fromJson(
        {'id': 'n', 'label': 'n', 'kind': kind, 'hops': 1, 'trenchchat': trenchchat});

    expect(showsTrenchChatDot(node('transport', trenchchat: true)), isTrue);
    expect(showsTrenchChatDot(node('transport', trenchchat: false)), isFalse);
    // A peer carries the distinction in its fill, not a second marker.
    expect(showsTrenchChatDot(node('peer', trenchchat: true)), isFalse);
  });
}
