import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:flutter_ui/api/events.dart';
import 'package:flutter_ui/api/models/network_map.dart';
import 'package:flutter_ui/app_state.dart';
import 'package:flutter_ui/screens/main_window/map_tab.dart';
import 'package:flutter_ui/widgets/emoji_text.dart' show nomadUrlRe;

import '../fake_backend.dart';

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
  test('a node page URL is one the NET tab recognises', () {
    const hash = 'ffeeffeeffeeffeeffeeffeeffeeffee';
    MapNode node({String? nomadNodeHash}) => MapNode.fromJson({
          'id': hash,
          'label': 'n',
          'kind': 'peer',
          'hops': 1,
          'nomad': true,
          'nomad_node_hash': ?nomadNodeHash,
        });

    expect(mapNomadPageUrl(node()), '$hash:/page/index.mu');
    expect(nomadUrlRe.matchAsPrefix(mapNomadPageUrl(node())), isNotNull);

    // A collapsed node dials its page destination, not its messaging id.
    const pageHash = '00dd00dd00dd00dd00dd00dd00dd00dd';
    expect(mapNomadPageUrl(node(nomadNodeHash: pageHash)),
        '$pageHash:/page/index.mu');
  });

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

  test('a node parses every path detail the backend reports', () {
    final data = NetworkMapData.fromJson(_richTopology());
    final peer = data.nodes.firstWhere((n) => n.id == kPeerId);

    expect(peer.via, kRelayId);
    expect(peer.interfaceName, 'TrenchChat Hub');
    expect(peer.lastHeard, 1000.0);
    expect(peer.expires, 2000.0);
    expect(peer.rttMs, 42.5);
    expect(peer.online, isTrue);
    expect(peer.nomad, isTrue);
    expect(peer.propagation, isTrue);
    expect(peer.identityHex, kPeerIdentity);
    expect(data.onlinePeerCount, 2);
    expect(data.interfaces.first.bitrate, 115200);
    expect(data.edges.first.kind, 'interface');
    expect(data.edges.last.kind, 'path');
    expect(data.edges.last.hops, 2);
  });

  test('an old backend sending none of the new keys still parses', () {
    final data = NetworkMapData.fromJson({
      'nodes': [
        {'id': 'p', 'label': 'p', 'kind': 'peer', 'hops': 1},
      ],
      'edges': [
        {'src': 'self', 'dst': 'p', 'direct': true},
      ],
      'interfaces': [
        {'name': 'Hub', 'type': 'TCPClientInterface', 'status': true, 'rxb': 1, 'txb': 2},
      ],
      'stats': {'node_count': 1, 'path_count': 1, 'interface_count': 1},
    });
    final node = data.nodes.single;

    expect(node.via, isNull);
    expect(node.interfaceName, isNull);
    expect(node.lastHeard, isNull);
    expect(node.expires, isNull);
    expect(node.rttMs, isNull);
    expect(node.online, isNull);
    expect(node.nomad, isFalse);
    expect(node.propagation, isFalse);
    expect(node.identityHex, isNull);
    expect(data.onlinePeerCount, isNull);
    expect(data.interfaces.single.bitrate, isNull);
    expect(data.edges.single.hops, 0);
    expect(data.edges.single.kind, 'path');
  });

  test('network_map_changed parses and bumps the AppState revision', () {
    expect(TcEvent.tryParse('{"type": "network_map_changed"}'),
        isA<NetworkMapChangedEvent>());

    final backend = FakeBackend();
    final state = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
    addTearDown(state.dispose);

    final before = state.networkMapRevision;
    state.applyEvent(const NetworkMapChangedEvent());
    state.applyEvent(const NetworkMapChangedEvent());
    expect(state.networkMapRevision, before + 2);
  });

  test('the fit transform centers the layout and inverts back to content', () {
    final exact = mapFitFor(const Size(200, 100), const Size(200, 100));
    expect(exact.scale, 1.0);
    expect(exact.offset, Offset.zero);

    // Never magnifies: a small graph on a big canvas is centered at 1:1.
    final roomy = mapFitFor(const Size(400, 300), const Size(200, 100));
    expect(roomy.scale, 1.0);
    expect(roomy.offset, const Offset(100, 100));
    expect(roomy.toContent(roomy.toCanvas(const Offset(20, 30))), const Offset(20, 30));

    final tight = mapFitFor(const Size(100, 100), const Size(200, 100));
    expect(tight.scale, 0.5);
    expect(tight.toContent(const Offset(50, 50)), const Offset(100, 50));
  });

  test('max zoom compensates for the fit so nodes reach the same size', () {
    // A graph that fits keeps the plain ceiling.
    expect(mapMaxScale(const Size(400, 300), const Size(200, 100)), 4.0);

    // A graph shrunk to fit gets a proportionally higher ceiling: viewer
    // scale times fit scale -- the on-screen magnification -- stays 4.
    expect(mapMaxScale(const Size(500, 500), const Size(2000, 1000)), 16.0);
    expect(mapMaxScale(const Size(100, 100), const Size(200, 100)), 8.0);
  });

  test('hit testing picks the nearest node and ignores empty space', () {
    final layout = layoutMapNodes(_data());
    final peer = layout.positions['peer-a']!;

    expect(hitTestMapNode(layout, peer), 'peer-a');
    expect(hitTestMapNode(layout, peer + const Offset(6, 6)), 'peer-a');
    expect(hitTestMapNode(layout, peer + const Offset(80, 80), radius: 8), isNull);
    expect(hitTestMapNode(layout, layout.center), 'self');
  });

  test('the transition lerps shared nodes and keeps the new ones put', () {
    final from = layoutMapNodes(_data());
    final to = layoutMapNodes(NetworkMapData.fromJson({
      'nodes': [
        {'id': 'self', 'label': 'This device', 'kind': 'self', 'hops': 0},
        {'id': 'peer-a', 'label': 'peer-a', 'kind': 'peer', 'hops': 1},
        {'id': 'newcomer', 'label': 'newcomer', 'kind': 'peer', 'hops': 1},
      ],
      'edges': <Map<String, dynamic>>[],
      'interfaces': <Map<String, dynamic>>[],
      'stats': {'node_count': 3, 'path_count': 0, 'interface_count': 0},
    }));

    expect(lerpMapLayout(from, to, 1.0).positions, to.positions);
    expect(lerpMapLayout(null, to, 0.0).positions, to.positions);

    final half = lerpMapLayout(from, to, 0.5);
    expect(half.positions['peer-a'],
        Offset.lerp(from.positions['peer-a'], to.positions['peer-a'], 0.5));
    // A node the old layout never had cannot be interpolated: it starts where
    // it ends and the painter fades it in instead.
    expect(half.positions['newcomer'], to.positions['newcomer']);
    // Nothing the new data dropped leaks into the tween.
    expect(half.positions.containsKey('relay'), isFalse);
  });

  test('search matches label, id, and identity hex, case-insensitively', () {
    final node = MapNode.fromJson({
      'id': 'aa11bb22',
      'label': 'Alice',
      'kind': 'peer',
      'hops': 1,
      'identity_hex': 'ff00cc',
    });

    expect(mapNodeMatchesQuery(node, ''), isTrue);
    expect(mapNodeMatchesQuery(node, '   '), isTrue);
    expect(mapNodeMatchesQuery(node, 'ali'), isTrue);
    expect(mapNodeMatchesQuery(node, 'ALI'), isTrue);
    expect(mapNodeMatchesQuery(node, '11bb'), isTrue);
    expect(mapNodeMatchesQuery(node, 'FF00'), isTrue);
    expect(mapNodeMatchesQuery(node, 'bob'), isFalse);
    // What does not match dims rather than disappearing.
    expect(mapDimOpacity, lessThan(1.0));
  });

  test('a via hex resolves to the node it names, or a short hex', () {
    final data = NetworkMapData.fromJson(_richTopology());

    expect(mapViaLabel(data, kRelayId), 'relay');
    expect(mapViaLabel(data, kPeerIdentity), 'Alice');
    expect(mapViaLabel(data, 'deadbeefdeadbeefdead'), mapShortHex('deadbeefdeadbeefdead'));
  });

  test('a path heard within the recent window rings, an old one does not', () {
    final now = DateTime.now().millisecondsSinceEpoch / 1000;
    MapNode heardAt(double? ts) => MapNode.fromJson(
        {'id': 'p', 'label': 'p', 'kind': 'peer', 'hops': 1, 'last_heard': ts});

    expect(mapHeardRecently(heardAt(now - 5)), isTrue);
    expect(mapHeardRecently(heardAt(now - 600)), isFalse);
    expect(mapHeardRecently(heardAt(null)), isFalse);
  });

  group('child collapsing', () {
    String overflowId() => mapOverflowIdFor('hub');

    MapCollapse collapseOf(List<Map<String, dynamic>> kids, {String? keepVisible}) =>
        collapseMapData(NetworkMapData.fromJson(_hubTopology(kids)),
            keepVisible: keepVisible);

    List<String> hiddenIds(MapCollapse c) =>
        (c.hidden[overflowId()] ?? const []).map((n) => n.id).toList();

    test('a parent past the limit draws ten children and one overflow node', () {
      final collapse = collapseOf(_plainKids(12));
      final drawn = collapse.data.nodes.where((n) => n.id.startsWith('kid-'));

      expect(drawn, hasLength(10));
      expect(hiddenIds(collapse), ['kid-10', 'kid-11']);
      final overflow = collapse.data.nodes.singleWhere(mapIsOverflowNode);
      expect(overflow.id, overflowId());
      expect(overflow.label, '2 more');
      expect(
          collapse.data.edges.any((e) =>
              e.src == 'hub' && e.dst == overflow.id && e.kind == mapOverflowEdgeKind),
          isTrue);
      // The header reads the stats, which still count every node there is.
      expect(collapse.data.nodeCount, 14);
    });

    test('eleven children all draw: a group of one costs what it hides', () {
      final collapse = collapseOf(_plainKids(11));

      expect(collapse.hidden, isEmpty);
      expect(collapse.data.nodes.where(mapIsOverflowNode), isEmpty);
      expect(collapse.data.nodes.where((n) => n.id.startsWith('kid-')), hasLength(11));
    });

    test('the drawn ten rank subtrees first, then TrenchChat, presence, quality, id',
        () {
      // Thirteen children: the relay is kept ahead of every TrenchChat leaf
      // because hiding it would take its own peer off the map with it, and
      // its id sorts last so nothing else can explain the choice.
      final collapse = collapseOf([
        {'id': 'z-relay', 'trenchchat': false, 'sub': true},
        {'id': 'c-online', 'trenchchat': false, 'online': true},
        {'id': 'd-q4', 'trenchchat': false, 'quality': 4},
        {'id': 'e-q1', 'trenchchat': false, 'quality': 1},
        ..._plainKids(9, prefix: 'f'),
      ]);
      final drawn = collapse.data.nodes.map((n) => n.id).toSet();

      expect(hiddenIds(collapse), ['c-online', 'd-q4', 'e-q1']);
      expect(drawn, contains('z-relay'));
      expect(drawn, contains('z-relay-sub'));
    });

    test('the same data always collapses the same way', () {
      expect(hiddenIds(collapseOf(_plainKids(15))), hiddenIds(collapseOf(_plainKids(15))));
      expect(hiddenIds(collapseOf(_plainKids(15))),
          ['kid-10', 'kid-11', 'kid-12', 'kid-13', 'kid-14']);
    });

    test('the selected node keeps its place even when the ranking drops it', () {
      expect(hiddenIds(collapseOf(_plainKids(13))), ['kid-10', 'kid-11', 'kid-12']);

      final kept = collapseOf(_plainKids(13), keepVisible: 'kid-12');
      expect(kept.data.nodes.map((n) => n.id), contains('kid-12'));
      expect(hiddenIds(kept), ['kid-10', 'kid-11']);
      expect(kept.data.nodes.singleWhere(mapIsOverflowNode).label, '2 more');
    });

    test('an exemption that leaves one over draws it instead of a "1 more"', () {
      expect(collapseOf(_plainKids(12), keepVisible: 'kid-11').hidden, isEmpty);
    });

    test('the overflow node hangs off its parent, hidden nodes off nothing', () {
      final collapse = collapseOf(_plainKids(12));
      final layout = layoutMapNodes(collapse.data);
      double radiusOf(String id) => (layout.positions[id]! - layout.center).distance;

      expect(mapTreeFor(collapse.data).parentOf[overflowId()], 'hub');
      expect(layout.positions[overflowId()], isNotNull);
      expect(layout.labels[overflowId()], isNotNull);
      // The group sits on the ring of the children it stands in for, one out
      // from the hub, so it reads as one of them rather than a node of its own.
      final kidRadii = collapse.data.nodes
          .where((n) => n.id.startsWith('kid-'))
          .map((n) => radiusOf(n.id))
          .toList()
        ..sort();
      expect(radiusOf(overflowId()),
          inInclusiveRange(kidRadii.first - 1, kidRadii.last + 1));
      expect(radiusOf('hub'), lessThan(kidRadii.first));
      for (final gone in const ['kid-10', 'kid-11']) {
        expect(layout.positions.containsKey(gone), isFalse);
        expect(layout.labels.containsKey(gone), isFalse);
      }
    });

    test('an overflow node matches a search any child it hides matches', () {
      final hidden = collapseOf(_plainKids(12)).hidden[overflowId()];

      expect(mapGroupMatchesQuery(hidden, 'KID-11'), isTrue);
      expect(mapGroupMatchesQuery(hidden, 'lbl-kid-10'), isTrue);
      expect(mapGroupMatchesQuery(hidden, 'kid-00'), isFalse);
      expect(mapGroupMatchesQuery(hidden, '  '), isFalse);
      expect(mapGroupMatchesQuery(null, 'kid'), isFalse);
    });
  });

  group('overflow list panel', () {
    late FakeBackend backend;
    late AppState state;

    setUp(() {
      backend = FakeBackend();
      backend.routes['GET /network/map'] = _hubTopology(_plainKids(12));
      state = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
    });

    tearDown(() => state.dispose());

    Widget harness({void Function(String)? onOpenNomadPage}) => MaterialApp(
        home: Scaffold(
            body: MapTab(state: state, onOpenNomadPage: onOpenNomadPage)));

    Finder mapCanvas() => find.byWidgetPredicate((w) =>
        w is CustomPaint && w.painter.runtimeType.toString() == '_NetworkMapPainter');

    Future<void> tapOverflow(WidgetTester tester,
        {List<Map<String, dynamic>>? kids}) async {
      final canvas = mapCanvas();
      final collapse = collapseMapData(
          NetworkMapData.fromJson(_hubTopology(kids ?? _plainKids(12))));
      final layout = layoutMapNodes(collapse.data);
      final fit = mapFitFor(tester.getSize(canvas), layout.size);
      await tester.tapAt(tester.getTopLeft(canvas) +
          fit.toCanvas(layout.positions[mapOverflowIdFor('hub')]!));
      await settle(tester);
    }

    testWidgets('the chips keep true totals while the map draws fewer nodes',
        (tester) async {
      await tester.pumpWidget(harness());
      await settle(tester);

      expect(find.text('14 NODES'), findsOneWidget);
      expect(find.text('13 PATHS'), findsOneWidget);
      final painted = (tester.widget<CustomPaint>(mapCanvas()).painter as dynamic).data
          as NetworkMapData;
      // self + hub + ten children + the group standing for the other two.
      expect(painted.nodes, hasLength(13));
    });

    testWidgets('tapping the overflow node lists the peers it groups', (tester) async {
      await tester.pumpWidget(harness());
      await settle(tester);
      await tapOverflow(tester);

      expect(find.text('2 MORE VIA hub'), findsOneWidget);
      expect(find.text('lbl-kid-10'), findsOneWidget);
      expect(find.text('lbl-kid-11'), findsOneWidget);
      expect(find.text('lbl-kid-00'), findsNothing);
      expect(tester.takeException(), isNull);
    });

    testWidgets('tapping a row opens its details, marked as grouped', (tester) async {
      await tester.pumpWidget(harness());
      await settle(tester);
      await tapOverflow(tester);

      await tester.tap(find.text('lbl-kid-11'));
      await settle(tester);
      expect(find.textContaining('GROUPED UNDER 2 MORE'), findsOneWidget);
      expect(find.text('HOPS'), findsOneWidget);
      expect(find.text('QUALITY'), findsOneWidget);

      await tester.tap(find.text('BACK'));
      await settle(tester);
      expect(find.text('2 MORE VIA hub'), findsOneWidget);
      expect(find.textContaining('GROUPED UNDER'), findsNothing);
      expect(tester.takeException(), isNull);
    });

    testWidgets('a refresh that ungroups the peer keeps its details without the note',
        (tester) async {
      await tester.pumpWidget(harness());
      await settle(tester);
      await tapOverflow(tester);
      await tester.tap(find.text('lbl-kid-11'));
      await settle(tester);
      expect(find.textContaining('GROUPED UNDER'), findsOneWidget);

      backend.routes['GET /network/map'] =
          _hubTopology(_plainKids(12).where((k) => k['id'] != 'kid-10').toList());
      await tester.tap(find.text('REFRESH'));
      await settle(tester);

      expect(find.textContaining('GROUPED UNDER'), findsNothing);
      expect(find.text('HOPS'), findsOneWidget);
      expect(tester.takeException(), isNull);
    });

    testWidgets('a refresh that dissolves the group leaves the gone note', (tester) async {
      await tester.pumpWidget(harness());
      await settle(tester);
      await tapOverflow(tester);
      expect(find.text('2 MORE VIA hub'), findsOneWidget);

      backend.routes['GET /network/map'] = _hubTopology(_plainKids(11));
      await tester.tap(find.text('REFRESH'));
      await settle(tester);

      expect(find.text('2 MORE VIA hub'), findsNothing);
      expect(find.textContaining('NO LONGER VISIBLE'), findsOneWidget);
      expect(tester.takeException(), isNull);
    });

    testWidgets('a peer opened out of the group offers its page too',
        (tester) async {
      final kids = _plainKids(12);
      kids[11] = {...kids[11], 'nomad': true};
      backend.routes['GET /network/map'] = _hubTopology(kids);
      final opened = <String>[];
      await tester.pumpWidget(harness(onOpenNomadPage: opened.add));
      await settle(tester);
      await tapOverflow(tester, kids: kids);

      await tester.tap(find.text('lbl-kid-11'));
      await settle(tester);
      expect(find.text('OPEN PAGE'), findsOneWidget);

      await tester.tap(find.text('OPEN PAGE'));
      await settle(tester);
      expect(opened, ['kid-11:/page/index.mu']);
      expect(tester.takeException(), isNull);
    });
  });

  group('map tab widget', () {
    late FakeBackend backend;
    late AppState state;

    setUp(() {
      backend = FakeBackend();
      backend.routes['GET /network/map'] = _richTopology();
      state = AppState(baseUrl: backend.baseUrl, httpClient: backend.client());
    });

    tearDown(() => state.dispose());

    Widget harness({void Function(String)? onOpenNomadPage}) => MaterialApp(
        home: Scaffold(
            body: MapTab(state: state, onOpenNomadPage: onOpenNomadPage)));

    int mapFetches() =>
        backend.requests.where((r) => r.path == '/network/map').length;

    Finder mapCanvas() => find.byWidgetPredicate((w) =>
        w is CustomPaint && w.painter.runtimeType.toString() == '_NetworkMapPainter');

    /// Taps the node [id] where the fit transform actually puts it on screen.
    Future<void> tapNode(WidgetTester tester, String id) async {
      final canvas = mapCanvas();
      final origin = tester.getTopLeft(canvas);
      final layout = layoutMapNodes(NetworkMapData.fromJson(_richTopology()));
      final fit = mapFitFor(tester.getSize(canvas), layout.size);
      await tester.tapAt(origin + fit.toCanvas(layout.positions[id]!));
      await settle(tester);
    }

    testWidgets('a network_map_changed event refetches the map', (tester) async {
      await tester.pumpWidget(harness());
      await settle(tester);
      expect(mapFetches(), 1);

      state.applyEvent(const NetworkMapChangedEvent());
      await settle(tester);
      expect(mapFetches(), 2);

      // An unrelated notification must not cost a fetch.
      state.reportError('unrelated');
      await settle(tester);
      expect(mapFetches(), 2);
    });

    testWidgets('the ONLINE chip shows only when the backend counts presence',
        (tester) async {
      await tester.pumpWidget(harness());
      await settle(tester);
      expect(find.text('2 ONLINE'), findsOneWidget);

      final noStats = _richTopology();
      (noStats['stats'] as Map<String, dynamic>).remove('online_peer_count');
      backend.routes['GET /network/map'] = noStats;
      await tester.tap(find.text('REFRESH'));
      await settle(tester);
      expect(find.text('2 ONLINE'), findsNothing);
    });

    testWidgets('tapping a node opens its details, tapping away closes them',
        (tester) async {
      await tester.pumpWidget(harness());
      await settle(tester);
      expect(find.text('HOPS'), findsNothing);

      await tapNode(tester, kPeerId);

      expect(find.text('Alice'), findsOneWidget);
      expect(find.text('TRENCHCHAT'), findsWidgets);
      expect(find.text('NOMAD'), findsWidgets);
      expect(find.text('PROPAGATION'), findsOneWidget);
      expect(find.text('HOPS'), findsOneWidget);
      expect(find.text('2'), findsOneWidget);
      expect(find.text('VIA'), findsOneWidget);
      expect(find.text('relay'), findsOneWidget);
      expect(find.text('INTERFACE'), findsOneWidget);
      expect(find.text('QUALITY'), findsOneWidget);
      expect(find.text('EXCELLENT'), findsWidgets);
      expect(find.text('RTT'), findsOneWidget);
      expect(find.textContaining(' ms'), findsOneWidget);
      expect(find.text(kPeerIdentity), findsOneWidget);
      expect(find.text('LAST HEARD'), findsOneWidget);
      expect(find.text('PATH EXPIRES'), findsOneWidget);
      expect(find.text('COPY'), findsWidgets);

      // Empty space deselects.
      await tester.tapAt(tester.getTopLeft(mapCanvas()) + const Offset(4, 4));
      await settle(tester);
      expect(find.text('HOPS'), findsNothing);
      expect(tester.takeException(), isNull);
    });

    testWidgets('an interface node shows its transport stats', (tester) async {
      await tester.pumpWidget(harness());
      await settle(tester);

      await tapNode(tester, '__iface__TrenchChat Hub');

      expect(find.text('TYPE'), findsOneWidget);
      expect(find.text('TCPClientInterface'), findsOneWidget);
      expect(find.text('BITRATE'), findsOneWidget);
      expect(find.text('115.2 kbps'), findsOneWidget);
      expect(find.text('RX'), findsOneWidget);
      expect(find.text('148.8 KB'), findsOneWidget);
      expect(find.text('TX'), findsOneWidget);
    });

    testWidgets('a node dropped by a refresh keeps the panel with a note',
        (tester) async {
      await tester.pumpWidget(harness());
      await settle(tester);
      await tapNode(tester, kPeerId);
      expect(find.text('HOPS'), findsOneWidget);

      final shrunk = _richTopology();
      final nodes = (shrunk['nodes'] as List<dynamic>).cast<Map<String, dynamic>>();
      final edges = (shrunk['edges'] as List<dynamic>).cast<Map<String, dynamic>>();
      shrunk['nodes'] = nodes.where((n) => n['id'] != kPeerId).toList();
      shrunk['edges'] =
          edges.where((e) => e['src'] != kPeerId && e['dst'] != kPeerId).toList();
      backend.routes['GET /network/map'] = shrunk;
      await tester.tap(find.text('REFRESH'));
      await settle(tester);

      expect(find.text('HOPS'), findsNothing);
      expect(find.textContaining('NO LONGER VISIBLE'), findsOneWidget);
      expect(tester.takeException(), isNull);
    });

    testWidgets('a nomad node opens its index page, a plain one offers nothing',
        (tester) async {
      final opened = <String>[];
      await tester.pumpWidget(harness(onOpenNomadPage: opened.add));
      await settle(tester);

      await tapNode(tester, kRelayId);
      expect(find.text('OPEN PAGE'), findsNothing);

      await tapNode(tester, kPeerId);
      expect(find.text('OPEN PAGE'), findsOneWidget);
      await tester.tap(find.text('OPEN PAGE'));
      await settle(tester);

      expect(opened, ['$kPeerId:/page/index.mu']);
      expect(tester.takeException(), isNull);
    });

    testWidgets('no callback means no page button', (tester) async {
      await tester.pumpWidget(harness());
      await settle(tester);
      await tapNode(tester, kPeerId);

      expect(find.text('NOMAD'), findsWidgets);
      expect(find.text('OPEN PAGE'), findsNothing);
    });

    testWidgets('typing in the search box leaves the map painting', (tester) async {
      await tester.pumpWidget(harness());
      await settle(tester);

      await tester.enterText(find.byType(TextField), 'alice');
      await settle(tester);

      expect(mapCanvas(), findsOneWidget);
      expect(tester.takeException(), isNull);
    });
  });
}

/// A hub owning [kids] children, each map merged over a plain TrenchChat
/// peer. A kid carrying 'sub': true owns a peer of its own, so the
/// descendants rule has a subtree to protect.
Map<String, dynamic> _hubTopology(List<Map<String, dynamic>> kids) {
  final nodes = <Map<String, dynamic>>[
    {'id': 'self', 'label': 'This device', 'kind': 'self', 'hops': 0},
    {'id': 'hub', 'label': 'hub', 'kind': 'transport', 'hops': 1, 'quality': 3},
  ];
  final edges = <Map<String, dynamic>>[
    {'src': 'self', 'dst': 'hub', 'hops': 1, 'direct': true},
  ];
  for (final kid in kids) {
    final id = kid['id'] as String;
    nodes.add({
      'label': 'lbl-$id',
      'kind': 'peer',
      'hops': 2,
      'quality': 0,
      'trenchchat': true,
      ...kid,
    });
    edges.add({'src': 'hub', 'dst': id, 'hops': 2, 'direct': false});
    if (kid['sub'] == true) {
      nodes.add({'id': '$id-sub', 'label': 'lbl-$id-sub', 'kind': 'peer', 'hops': 3});
      edges.add({'src': id, 'dst': '$id-sub', 'hops': 3, 'direct': false});
    }
  }
  return {
    'nodes': nodes,
    'edges': edges,
    'interfaces': <Map<String, dynamic>>[],
    'stats': {
      'node_count': nodes.length,
      'path_count': edges.length,
      'interface_count': 0,
    },
  };
}

List<Map<String, dynamic>> _plainKids(int count, {String prefix = 'kid'}) => [
      for (var i = 0; i < count; i++) {'id': '$prefix-${i.toString().padLeft(2, '0')}'},
    ];

const String kPeerId = 'aa11bb22cc33';
const String kPeerIdentity = 'ff00cc11dd22';
const String kRelayId = 'bb22cc33dd44';

/// A gather_network_data payload carrying every key the current backend
/// reports, so the panel and the parser are exercised against a full node.
Map<String, dynamic> _richTopology() => {
      'nodes': [
        {'id': 'self', 'label': 'This device', 'kind': 'self', 'hops': 0, 'quality': 4,
         'identity_hex': '0011223344'},
        {'id': '__iface__TrenchChat Hub', 'label': '● TrenchChat Hub (TCP)',
         'kind': 'interface', 'hops': 0, 'quality': 4},
        {
          'id': kPeerId,
          'label': 'Alice',
          'kind': 'peer',
          'hops': 2,
          'quality': 4,
          'trenchchat': true,
          'via': kRelayId,
          'interface': 'TrenchChat Hub',
          'last_heard': 1000.0,
          'expires': 2000.0,
          'rtt_ms': 42.5,
          'online': true,
          'nomad': true,
          'propagation': true,
          'identity_hex': kPeerIdentity,
        },
        {'id': kRelayId, 'label': 'relay', 'kind': 'transport', 'hops': 1, 'quality': 3},
      ],
      'edges': [
        {'src': 'self', 'dst': '__iface__TrenchChat Hub', 'hops': 0, 'direct': true,
         'quality': 4, 'kind': 'interface'},
        {'src': 'self', 'dst': kRelayId, 'hops': 1, 'direct': true, 'quality': 3,
         'kind': 'path'},
        {'src': kRelayId, 'dst': kPeerId, 'hops': 2, 'direct': false, 'quality': 4,
         'kind': 'path'},
      ],
      'interfaces': [
        {'name': 'TrenchChat Hub', 'type': 'TCPClientInterface', 'status': true,
         'rxb': 152400, 'txb': 38200, 'bitrate': 115200},
      ],
      'stats': {
        'node_count': 4,
        'path_count': 3,
        'interface_count': 1,
        'online_peer_count': 2,
      },
    };
