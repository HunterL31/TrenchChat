// Stress tests for the MAP tab: the radial layout under a large topology
// (120 nodes -- the backend's _MAX_NODES cap) and the link-quality color
// path from tier value to painted pixel, across every built-in theme.
import 'dart:math' as math;
import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/models/network_map.dart';
import 'package:flutter_ui/app_state.dart';
import 'package:flutter_ui/screens/main_window/map_tab.dart';
import 'package:flutter_ui/theme/quality_tiers.dart';
import 'package:flutter_ui/theme/theme_presets.dart';
import 'package:flutter_ui/theme/theme_spec.dart';

import '../fake_backend.dart';

/// Deterministic gather_network_data-shaped topology: [interfaces] up/down
/// interfaces, [directPeers] 1-hop peers, [relays] transports each carrying
/// [peersPerRelay] peers at hops 2-3, and [unknowns] unresolved hashes.
/// Qualities cycle through every tier and a quarter of the labels are long.
/// Every seventh direct peer is a plain LXMF node (trenchchat: false), and
/// only relay-1 among the relays is itself a TrenchChat client.
Map<String, dynamic> stressTopology({
  int interfaces = 3,
  int directPeers = 30,
  int relays = 6,
  int peersPerRelay = 12,
  int unknowns = 8,
}) {
  final nodes = <Map<String, dynamic>>[
    {'id': 'self', 'label': 'This device', 'kind': 'self', 'hops': 0, 'quality': 4},
  ];
  final edges = <Map<String, dynamic>>[];
  final ifaces = <Map<String, dynamic>>[];
  for (var i = 0; i < interfaces; i++) {
    final up = i % 2 == 0;
    final q = up ? 4 : 1;
    nodes.add({
      'id': '__iface__IF$i',
      'label': '● IF$i (TCP)',
      'kind': 'interface',
      'hops': 0,
      'quality': q,
      'trenchchat': false,
    });
    edges.add(
        {'src': 'self', 'dst': '__iface__IF$i', 'hops': 0, 'direct': true, 'quality': q});
    ifaces.add(
        {'name': 'IF$i', 'type': 'TCPClientInterface', 'status': up, 'rxb': i, 'txb': i});
  }
  for (var i = 0; i < directPeers; i++) {
    final q = i % 5;
    final label = i % 4 == 0 ? 'unreasonably long operator callsign $i' : 'peer-$i';
    nodes.add({'id': 'direct-$i', 'label': label, 'kind': 'peer', 'hops': 1, 'quality': q,
               'trenchchat': i % 7 != 6});
    edges.add({'src': 'self', 'dst': 'direct-$i', 'hops': 1, 'direct': true, 'quality': q});
  }
  for (var r = 0; r < relays; r++) {
    final rq = r % 4 + 1;
    nodes.add({'id': 'relay-$r', 'label': 'relay-$r', 'kind': 'transport', 'hops': 1,
               'quality': rq, 'trenchchat': r == 1});
    edges.add({'src': 'self', 'dst': 'relay-$r', 'hops': 1, 'direct': true, 'quality': rq});
    for (var p = 0; p < peersPerRelay; p++) {
      final hops = p % 3 == 0 ? 3 : 2;
      final q = (r + p) % 5;
      final id = 'relay-$r-peer-$p';
      nodes.add({'id': id, 'label': id, 'kind': 'peer', 'hops': hops, 'quality': q,
                 'trenchchat': true});
      edges.add({'src': 'relay-$r', 'dst': id, 'hops': hops, 'direct': false, 'quality': q});
    }
  }
  for (var u = 0; u < unknowns; u++) {
    final id = 'unknown-$u';
    nodes.add({'id': id, 'label': 'f00${u}baa71e57…', 'kind': 'unknown', 'hops': 1,
               'quality': 0, 'trenchchat': false});
    edges.add({'src': 'self', 'dst': id, 'hops': 1, 'direct': true, 'quality': 0});
  }
  return {
    'nodes': nodes,
    'edges': edges,
    'interfaces': ifaces,
    'stats': {
      'node_count': nodes.length,
      'path_count': edges.length,
      'interface_count': interfaces,
    },
  };
}

/// One direct edge per quality tier, for pixel-sampling the painter. Self
/// gets an empty label so its text and glow can't bleed onto the edges.
Map<String, dynamic> tierTopology() => {
      'nodes': [
        {'id': 'self', 'label': '', 'kind': 'self', 'hops': 0, 'quality': 4},
        for (var q = 0; q <= 4; q++)
          {'id': 'peer-q$q', 'label': 'q$q', 'kind': 'peer', 'hops': 1, 'quality': q},
      ],
      'edges': [
        for (var q = 0; q <= 4; q++)
          {'src': 'self', 'dst': 'peer-q$q', 'hops': 1, 'direct': true, 'quality': q},
      ],
      'interfaces': <Map<String, dynamic>>[],
      'stats': {'node_count': 6, 'path_count': 5, 'interface_count': 0},
    };

void main() {
  final data = NetworkMapData.fromJson(stressTopology());

  test('every node in a 120-node graph gets a position and a label', () {
    expect(data.nodes, hasLength(120));
    final layout = layoutMapNodes(data);
    for (final node in data.nodes) {
      expect(layout.positions[node.id], isNotNull, reason: 'no position for ${node.id}');
      expect(layout.labels[node.id], isNotNull, reason: 'no label for ${node.id}');
    }
  });

  test('layout is deterministic at scale', () {
    final first = layoutMapNodes(data);
    final second = layoutMapNodes(data);
    expect(first.positions, second.positions);
    expect(first.size, second.size);
    for (final id in first.labels.keys) {
      expect(first.labels[id]!.rect, second.labels[id]!.rect);
    }
  });

  test('layout stays finite and bounded at scale', () {
    final layout = layoutMapNodes(data);
    expect(layout.size.width.isFinite, isTrue);
    expect(layout.size.height.isFinite, isTrue);
    expect(layout.size.longestSide, lessThan(8000));
    for (final pos in layout.positions.values) {
      expect(pos.dx.isFinite && pos.dy.isFinite, isTrue);
    }
  });

  test('rings stay ordered: interfaces, then hop 1, 2, 3', () {
    final layout = layoutMapNodes(data);
    double radius(String id) => (layout.positions[id]! - layout.center).distance;
    double ringOf(Iterable<MapNode> nodes) {
      final radii = nodes.map((n) => radius(n.id)).toList();
      // Every node of the group sits on one exact ring.
      for (final r in radii) {
        expect(r, closeTo(radii.first, 1e-6));
      }
      return radii.first;
    }

    final byGroup = <int, List<MapNode>>{};
    for (final n in data.nodes) {
      if (n.kind == MapNodeKind.self) continue;
      byGroup.putIfAbsent(n.kind == MapNodeKind.interface_ ? 0 : n.hops, () => []).add(n);
    }
    final r0 = ringOf(byGroup[0]!);
    final r1 = ringOf(byGroup[1]!);
    final r2 = ringOf(byGroup[2]!);
    final r3 = ringOf(byGroup[3]!);
    expect(r0, lessThan(r1));
    expect(r1, lessThan(r2));
    expect(r2, lessThan(r3));
  });

  test('node markers never overlap at scale', () {
    final layout = layoutMapNodes(data);
    // Two markers are visually merged under ~14px (node half-extent 7 incl.
    // the diamond point, times two).
    const minSeparation = 14.0;
    final entries = layout.positions.entries.toList();
    for (var i = 0; i < entries.length; i++) {
      for (var j = i + 1; j < entries.length; j++) {
        final d = (entries[i].value - entries[j].value).distance;
        expect(d, greaterThanOrEqualTo(minSeparation),
            reason: '${entries[i].key} and ${entries[j].key} are ${d.toStringAsFixed(1)}px apart');
      }
    }
  });

  test('labels stay beside their icons at scale', () {
    // Crowded rings must get room from ring sizing, not from pushing labels
    // away until they no longer read as belonging to their node.
    final layout = layoutMapNodes(data);
    for (final entry in layout.labels.entries) {
      final pos = layout.positions[entry.key]!;
      final rect = entry.value.rect;
      final dx = math.max(0.0, math.max(rect.left - pos.dx, pos.dx - rect.right));
      final dy = math.max(0.0, math.max(rect.top - pos.dy, pos.dy - rect.bottom));
      final gap = math.sqrt(dx * dx + dy * dy);
      expect(gap, lessThanOrEqualTo(48),
          reason: '${entry.key} label sits ${gap.toStringAsFixed(1)}px from its node');
    }
  });

  test('label boxes never overlap at scale', () {
    final labels = layoutMapNodes(data).labels.entries.toList();
    for (var i = 0; i < labels.length; i++) {
      for (var j = i + 1; j < labels.length; j++) {
        expect(labels[i].value.rect.overlaps(labels[j].value.rect), isFalse,
            reason: '${labels[i].key} overlaps ${labels[j].key}');
      }
    }
  });

  test('the trenchchat flag parses, defaulting to the old behavior for peers', () {
    final byId = {for (final n in data.nodes) n.id: n};
    expect(byId['direct-0']!.isTrenchChat, isTrue);
    expect(byId['direct-6']!.isTrenchChat, isFalse);   // plain LXMF node
    expect(byId['relay-1']!.isTrenchChat, isTrue);     // client that also relays
    expect(byId['relay-0']!.isTrenchChat, isFalse);
    expect(byId['__iface__IF0']!.isTrenchChat, isFalse);

    // Old backends send no flag: peers keep the old filter behavior.
    final legacy = MapNode.fromJson({'id': 'x', 'label': 'x', 'kind': 'peer', 'hops': 1});
    expect(legacy.isTrenchChat, isTrue);
    final legacyRelay =
        MapNode.fromJson({'id': 'y', 'label': 'y', 'kind': 'transport', 'hops': 1});
    expect(legacyRelay.isTrenchChat, isFalse);
  });

  test('peers-only keeps TrenchChat clients, drops infrastructure and LXMF-only nodes',
      () {
    final byId = {for (final n in data.nodes) n.id: n};
    expect(isPeerNode(byId['self']!), isTrue);
    expect(isPeerNode(byId['direct-0']!), isTrue);
    expect(isPeerNode(byId['direct-6']!), isFalse);        // LXMF, no TrenchChat
    expect(isPeerNode(byId['relay-1']!), isTrue);          // client acting as relay
    expect(isPeerNode(byId['relay-0']!), isFalse);
    expect(isPeerNode(byId['__iface__IF0']!), isFalse);
    expect(isPeerNode(byId['unknown-0']!), isFalse);
  });

  test('quality tiers keep five distinct colors in every built-in preset', () {
    for (final preset in themePresets) {
      for (final section in TCSection.values) {
        final tc = preset.spec.resolve(section);
        final colors = [4, 3, 2, 1, 0].map((q) => tcQualityColor(q, tc)).toList();
        expect(colors.toSet(), hasLength(5),
            reason: '${preset.name}/${section.wireId} collapses tiers: $colors');
      }
    }
  });

  group('map tab widget', () {
    late FakeBackend backend;
    late AppState state;

    setUp(() {
      backend = FakeBackend();
      state = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
    });

    tearDown(() => state.dispose());

    Widget harness() => MaterialApp(home: Scaffold(body: MapTab(state: state)));

    CustomPaint mapPaint(WidgetTester tester) => tester.widget<CustomPaint>(
          find.byWidgetPredicate((w) =>
              w is CustomPaint &&
              w.painter.runtimeType.toString() == '_NetworkMapPainter'),
        );

    testWidgets('renders the 120-node stress graph without errors', (tester) async {
      backend.routes['GET /network/map'] = stressTopology();
      await tester.pumpWidget(harness());
      await settle(tester);

      expect(find.text('${data.nodeCount} NODES'), findsOneWidget);
      expect(find.text('${data.pathCount} PATHS'), findsOneWidget);
      expect(find.text('${data.interfaceCount} IFACES'), findsOneWidget);
      expect(mapPaint(tester), isNotNull);
      expect(tester.takeException(), isNull);
    });

    testWidgets('peers-only filter drops infrastructure and its edges at scale',
        (tester) async {
      backend.routes['GET /network/map'] = stressTopology();
      await tester.pumpWidget(harness());
      await settle(tester);

      await tester.tap(find.text('PEERS ONLY'));
      await settle(tester);

      final painted = (mapPaint(tester).painter as dynamic).data as NetworkMapData;
      expect(painted.nodes.every(isPeerNode), isTrue);
      // 30 direct minus 4 LXMF-only, plus 72 relay-subtree peers.
      expect(painted.nodes.where((n) => n.kind == MapNodeKind.peer).length, 98);
      // relay-1 is a TrenchChat client that happens to relay: it stays.
      expect(painted.nodes.any((n) => n.id == 'relay-1'), isTrue);
      expect(painted.nodes.any((n) => n.id == 'relay-0'), isFalse);
      final kept = painted.nodes.map((n) => n.id).toSet();
      for (final e in painted.edges) {
        expect(kept.contains(e.src) && kept.contains(e.dst), isTrue,
            reason: 'dangling edge ${e.src} -> ${e.dst}');
      }
      expect(tester.takeException(), isNull);
    });

    testWidgets('direct edges paint in their quality tier color', (tester) async {
      final topo = tierTopology();
      backend.routes['GET /network/map'] = topo;
      await tester.pumpWidget(harness());
      await settle(tester);

      final painter = mapPaint(tester).painter!;
      final parsed = NetworkMapData.fromJson(topo);
      final layout = layoutMapNodes(parsed);
      final width = layout.size.width.ceil();
      final height = layout.size.height.ceil();

      // Paint at exactly the layout size so fit == 1 and layout coordinates
      // equal pixel coordinates, then read the raster back.
      final byteData = await tester.runAsync(() async {
        final recorder = ui.PictureRecorder();
        painter.paint(Canvas(recorder), Size(width.toDouble(), height.toDouble()));
        final image = await recorder.endRecording().toImage(width, height);
        return image.toByteData(format: ui.ImageByteFormat.rawStraightRgba);
      });
      expect(byteData, isNotNull);

      // The strongest pixel in a small window around an edge's midpoint.
      Color? sample(Offset at) {
        Color? best;
        var bestAlpha = 0;
        for (var dy = -1; dy <= 1; dy++) {
          for (var dx = -1; dx <= 1; dx++) {
            final x = at.dx.round() + dx;
            final y = at.dy.round() + dy;
            if (x < 0 || y < 0 || x >= width || y >= height) continue;
            final i = (y * width + x) * 4;
            final a = byteData!.getUint8(i + 3);
            if (a > bestAlpha) {
              bestAlpha = a;
              best = Color.fromARGB(255, byteData.getUint8(i), byteData.getUint8(i + 1),
                  byteData.getUint8(i + 2));
            }
          }
        }
        return best;
      }

      // Antialiasing and straight-alpha readback can shift a channel by a
      // few units; anything larger means the wrong tier color was used.
      void expectTierColor(Color? painted, Color expected, String what) {
        expect(painted, isNotNull, reason: 'no paint found on $what');
        final p = painted!.toARGB32();
        final e = expected.toARGB32();
        for (var shift = 0; shift < 24; shift += 8) {
          expect(((p >> shift) & 0xFF) - ((e >> shift) & 0xFF), inInclusiveRange(-3, 3),
              reason: '$what painted $painted, legend says $expected');
        }
      }

      for (var q = 0; q <= 4; q++) {
        final a = layout.positions['self']!;
        final b = layout.positions['peer-q$q']!;
        expectTierColor(sample(Offset.lerp(a, b, 0.5)!), mapQualityColor(q),
            'the quality-$q edge');
      }
      expect(tester.takeException(), isNull);
    });

    testWidgets('a TrenchChat peer square paints filled, another node hollow',
        (tester) async {
      final topo = {
        'nodes': [
          {'id': 'self', 'label': '', 'kind': 'self', 'hops': 0, 'quality': 4},
          {'id': 'tc', 'label': 'tc', 'kind': 'peer', 'hops': 1, 'quality': 4,
           'trenchchat': true},
          {'id': 'plain', 'label': 'plain', 'kind': 'peer', 'hops': 1, 'quality': 4,
           'trenchchat': false},
        ],
        'edges': <Map<String, dynamic>>[],
        'interfaces': <Map<String, dynamic>>[],
        'stats': {'node_count': 3, 'path_count': 0, 'interface_count': 0},
      };
      backend.routes['GET /network/map'] = topo;
      await tester.pumpWidget(harness());
      await settle(tester);

      final painter = mapPaint(tester).painter!;
      final layout = layoutMapNodes(NetworkMapData.fromJson(topo));
      final width = layout.size.width.ceil();
      final height = layout.size.height.ceil();
      final byteData = await tester.runAsync(() async {
        final recorder = ui.PictureRecorder();
        painter.paint(Canvas(recorder), Size(width.toDouble(), height.toDouble()));
        final image = await recorder.endRecording().toImage(width, height);
        return image.toByteData(format: ui.ImageByteFormat.rawStraightRgba);
      });
      expect(byteData, isNotNull);

      int alphaAt(String id) {
        final at = layout.positions[id]!;
        final i = (at.dy.round() * width + at.dx.round()) * 4;
        return byteData!.getUint8(i + 3);
      }

      // No edges in this topology, so a node's own center is the only thing
      // that can paint there: opaque means the square was filled.
      expect(alphaAt('tc'), 255);
      expect(alphaAt('plain'), 0);
    });

    testWidgets('legend swatches show the five tier colors', (tester) async {
      backend.routes['GET /network/map'] = tierTopology();
      await tester.pumpWidget(harness());
      await settle(tester);

      for (final (label, quality) in const [
        ('EXCELLENT', 4),
        ('GOOD', 3),
        ('FAIR', 2),
        ('POOR', 1),
        ('UNKNOWN', 0),
      ]) {
        final row = find.ancestor(of: find.text(label), matching: find.byType(Row)).first;
        final swatch = tester.widget<Container>(find
            .descendant(of: row, matching: find.byType(Container))
            .first);
        expect(swatch.color, mapQualityColor(quality), reason: 'swatch for $label');
      }
    });

    testWidgets('the legend distinguishes TrenchChat nodes from other nodes',
        (tester) async {
      backend.routes['GET /network/map'] = tierTopology();
      await tester.pumpWidget(harness());
      await settle(tester);

      BoxDecoration swatchFor(String label) {
        final row = find.ancestor(of: find.text(label), matching: find.byType(Row)).first;
        final swatch = tester.widget<Container>(
            find.descendant(of: row, matching: find.byType(Container)).first);
        return swatch.decoration! as BoxDecoration;
      }

      expect(swatchFor('TRENCHCHAT').color, isNotNull);
      expect(swatchFor('OTHER NODE').color, isNull);
      expect(swatchFor('OTHER NODE').border, isNotNull);
    });
  });

  test('label estimate stays inside the painter max width', () {
    // The layout reserves _labelMaxWidth at most; a longer string must not
    // widen the reserved box past what the ellipsized TextPainter will use.
    final long = NetworkMapData.fromJson({
      'nodes': [
        {'id': 'self', 'label': 'This device', 'kind': 'self', 'hops': 0, 'quality': 4},
        {
          'id': 'p',
          'label': 'x' * 400,
          'kind': 'peer',
          'hops': 1,
          'quality': 4,
        },
      ],
      'edges': [
        {'src': 'self', 'dst': 'p', 'hops': 1, 'direct': true, 'quality': 4},
      ],
      'interfaces': <Map<String, dynamic>>[],
      'stats': {'node_count': 2, 'path_count': 1, 'interface_count': 0},
    });
    final layout = layoutMapNodes(long);
    expect(layout.labels['p']!.rect.width, lessThanOrEqualTo(140));
    expect(layout.size.longestSide, lessThan(1000));
  });

  test('stress layout keeps subtree peers in their relay sector', () {
    final layout = layoutMapNodes(data);
    double angleOf(String id) {
      final v = layout.positions[id]! - layout.center;
      return math.atan2(v.dy, v.dx);
    }

    // Each relay's first child sits in the relay's angular sector, never on
    // the far side of the circle.
    for (var r = 0; r < 6; r++) {
      final spread = (angleOf('relay-$r-peer-0') - angleOf('relay-$r')).abs();
      final wrapped = math.min(spread, 2 * math.pi - spread);
      expect(wrapped, lessThan(math.pi / 2),
          reason: 'relay-$r subtree drifted out of its sector');
    }
  });
}
