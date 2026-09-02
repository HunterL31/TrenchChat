// MAP tab -- port of network_map.py's NetworkMapWidget over GET /network/map.
// The Qt widget runs a force-directed layout; this uses a deterministic radial
// one (self centered, interfaces on the inner ring, peers ringed by hop count)
// so the picture is stable across refreshes and testable. Nodes are placed in
// angular sectors under the node they route through, ring radii grow to fit
// their labels, and labels anchor on the side of the node facing away from
// center so they stay off the edge lines.
//
// The layout stays pure: it is computed once per data set in the tab's state,
// and everything the view adds on top -- the fit transform, the tween between
// an old layout and a new one, hit testing, search dimming -- is a pure
// function of that result, so the same math drives the painter and the tap
// handler and both are testable without a canvas.
import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../../api/client.dart';
import '../../api/models/network_map.dart';
import '../../app_state.dart';
import '../../format.dart';
import '../../theme/glow.dart';
import '../../theme/quality_tiers.dart';
import '../../theme/section_theme.dart';
import '../../theme/theme_spec.dart';
import '../../theme/tokens.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_checkbox.dart';
import '../../widgets/tc_icon.dart';
import '../../widgets/tc_text_field.dart';

/// Same tiers as the Qt map's _COL_QUALITY and SignalMeter, so the map
/// agrees with the header's link chip: 4=excellent .. 1=poor, 0=unknown.
/// [colors] defaults to the stock palette so the tear-off stays a
/// `Color Function(int)`.
Color mapQualityColor(int quality, {TCSectionColors? colors}) =>
    tcQualityColor(quality, colors ?? TCSectionColors.stock);

/// The legend's word for a quality tier, so the details panel and the legend
/// can never disagree about what a color means.
String mapQualityLabel(int quality) => switch (quality) {
      4 => 'EXCELLENT',
      3 => 'GOOD',
      2 => 'FAIR',
      1 => 'POOR',
      _ => 'UNKNOWN',
    };

/// Nodes kept by the "peers only" filter: this device plus nodes known to
/// run TrenchChat, not plain Reticulum/LXMF infrastructure (interfaces,
/// relays, nodes that never announced as TrenchChat users).
bool isPeerNode(MapNode node) =>
    node.kind == MapNodeKind.self ||
    (node.isTrenchChat && node.kind != MapNodeKind.interface_);

/// A peer square is filled for a TrenchChat client and outlined for any other
/// Reticulum node, so the two read apart at a glance without touching the
/// quality color the square is painted in.
PaintingStyle mapPeerStyle(MapNode node) =>
    node.isTrenchChat ? PaintingStyle.fill : PaintingStyle.stroke;

/// A transport diamond is already filled by quality, so a TrenchChat client
/// that also relays gets a small accent dot at its corner instead.
bool showsTrenchChatDot(MapNode node) =>
    node.isTrenchChat && node.kind == MapNodeKind.transport;

/// A node whose path was learned this recently gets a ring in its quality
/// color -- a static one, never a pulse: a repeating animation would keep the
/// tab painting forever and hang every `pumpAndSettle` in the suite.
const double mapRecentHeardSecs = 60;

bool mapHeardRecently(MapNode node, {double? nowUnix}) {
  final heard = node.lastHeard;
  if (heard == null || heard <= 0) return false;
  final now = nowUnix ?? DateTime.now().millisecondsSinceEpoch / 1000;
  return now - heard <= mapRecentHeardSecs && now - heard >= -mapRecentHeardSecs;
}

/// Case-insensitive match of the search box against everything a node can be
/// identified by. An empty query matches everything.
bool mapNodeMatchesQuery(MapNode node, String query) {
  final q = query.trim().toLowerCase();
  if (q.isEmpty) return true;
  return node.label.toLowerCase().contains(q) ||
      node.id.toLowerCase().contains(q) ||
      (node.identityHex ?? '').toLowerCase().contains(q);
}

/// What everything that does not match the search fades to.
const double mapDimOpacity = 0.25;

/// How far off a node's center a tap still counts, in content coordinates
/// before the fit scale is taken into account.
const double mapHitRadius = 16.0;

const String mapInterfaceIdPrefix = '__iface__';

const double _nodeHalf = 5.0;
const double _tcMarkerRadius = 2.0;
const double _labelMaxWidth = 140.0;
const double _labelHeight = 14.0;
const double _labelGap = 6.0;
const double _ringGap = 84.0;
const double _innerRadius = 64.0;
const double _arcGap = 18.0;
const double _contentMargin = 40.0;

/// A node label's estimated box (layout coordinates) and how the text is
/// anchored inside it when the measured width differs from the estimate.
class MapLabel {
  const MapLabel({required this.rect, required this.align});

  final Rect rect;
  final TextAlign align;
}

/// Result of [layoutMapNodes]: node positions and label boxes in content
/// coordinates, the content size, and where self / the ring center landed.
class MapLayout {
  const MapLayout({
    required this.positions,
    required this.labels,
    required this.size,
    required this.center,
  });

  final Map<String, Offset> positions;
  final Map<String, MapLabel> labels;
  final Size size;
  final Offset center;
}

/// How a [MapLayout] is placed inside a canvas: one uniform scale, never
/// magnifying, plus the offset that centers it. Shared by the painter and the
/// tap handler so a tap lands on the node it looks like it landed on.
class MapFit {
  const MapFit({required this.scale, required this.offset});

  final double scale;
  final Offset offset;

  Offset toContent(Offset local) => (local - offset) / scale;
  Offset toCanvas(Offset content) => content * scale + offset;
}

/// How far past natural (1:1 content) size the viewer may magnify.
const double _viewerMaxZoom = 4.0;

/// Zoom ceiling for the map's InteractiveViewer. A graph too large for the
/// canvas is first shrunk by [mapFitFor], so a fixed ceiling would leave its
/// nodes tiny at max zoom; dividing by the fit keeps max zoom at the same
/// multiple of the graph's natural size whatever the graph's extent.
double mapMaxScale(Size canvas, Size content) =>
    _viewerMaxZoom / mapFitFor(canvas, content).scale;

MapFit mapFitFor(Size canvas, Size content) {
  final scale = content.width <= 0 || content.height <= 0
      ? 1.0
      : math.min(1.0,
          math.min(canvas.width / content.width, canvas.height / content.height));
  return MapFit(
    scale: scale,
    offset: Offset((canvas.width - content.width * scale) / 2,
        (canvas.height - content.height * scale) / 2),
  );
}

/// The layout partway between two data sets. Nodes present in both move; a
/// node only [to] knows starts where it ends (the painter fades it in), and a
/// node only [from] knows is not here at all -- the painter reads its old
/// position straight off [from] to fade it out.
MapLayout lerpMapLayout(MapLayout? from, MapLayout to, double t) {
  if (from == null || t >= 1.0) return to;
  final positions = <String, Offset>{};
  to.positions.forEach((id, p) {
    final was = from.positions[id];
    positions[id] = was == null ? p : Offset.lerp(was, p, t)!;
  });
  final labels = <String, MapLabel>{};
  to.labels.forEach((id, l) {
    final was = from.labels[id];
    labels[id] = was == null
        ? l
        : MapLabel(rect: Rect.lerp(was.rect, l.rect, t)!, align: l.align);
  });
  return MapLayout(
    positions: positions,
    labels: labels,
    size: Size.lerp(from.size, to.size, t)!,
    center: Offset.lerp(from.center, to.center, t)!,
  );
}

/// The node nearest [point] within [radius], or null for empty space.
/// Ties break by id so the result never depends on map iteration order.
String? hitTestMapNode(MapLayout layout, Offset point,
    {double radius = mapHitRadius}) {
  String? best;
  var bestDistance = double.infinity;
  layout.positions.forEach((id, pos) {
    final d = (pos - point).distance;
    if (d > radius) return;
    if (d < bestDistance || (d == bestDistance && (best == null || id.compareTo(best!) < 0))) {
      best = id;
      bestDistance = d;
    }
  });
  return best;
}

double _estimateLabelWidth(String label) =>
    math.min(label.length * 6.2 + 4, _labelMaxWidth);

/// Radial positions for every node. Self sits at the center; interfaces on
/// the innermost ring; everything else ringed by hop count, with the distinct
/// ring keys compacted to consecutive indices so one distant node can't
/// squeeze the inner rings. Each node is placed inside the angular sector of
/// the node it routes through (sector width proportional to subtree size),
/// and a ring's radius grows until neighboring labels fit along its arc.
/// Deterministic: all ties break by node id.
MapLayout layoutMapNodes(NetworkMapData data) {
  final byId = {for (final n in data.nodes) n.id: n};
  String? selfId;
  for (final n in data.nodes) {
    if (n.kind == MapNodeKind.self) {
      selfId = n.id;
      break;
    }
  }

  final ringKeyById = <String, int>{};
  for (final n in data.nodes) {
    if (n.id == selfId) continue;
    ringKeyById[n.id] = n.kind == MapNodeKind.interface_ ? 0 : math.max(n.hops, 1);
  }
  final distinctKeys = ringKeyById.values.toSet().toList()..sort();
  final ringIndexByKey = {
    for (var i = 0; i < distinctKeys.length; i++) distinctKeys[i]: i + 1,
  };
  final ringOf = <String, int>{?selfId: 0};
  ringKeyById.forEach((id, key) => ringOf[id] = ringIndexByKey[key]!);

  // Parent = the edge neighbor closest inward; nodes with no inward edge hang
  // off self, so every subtree stays inside its parent's angular sector and
  // relay->peer edges come out as short radial spokes.
  final neighbors = <String, Set<String>>{};
  for (final e in data.edges) {
    if (!byId.containsKey(e.src) || !byId.containsKey(e.dst)) continue;
    neighbors.putIfAbsent(e.src, () => {}).add(e.dst);
    neighbors.putIfAbsent(e.dst, () => {}).add(e.src);
  }
  const root = '__root__';
  final parentOf = <String, String>{};
  for (final id in ringKeyById.keys) {
    final myRing = ringOf[id]!;
    var parent = selfId ?? root;
    var parentRing = 0;
    final sorted = (neighbors[id] ?? const <String>{}).toList()..sort();
    for (final nb in sorted) {
      final nbRing = ringOf[nb];
      if (nbRing == null || nbRing >= myRing) continue;
      if (nbRing > parentRing) {
        parent = nb;
        parentRing = nbRing;
      }
    }
    parentOf[id] = parent;
  }
  final childrenOf = <String, List<String>>{};
  parentOf.forEach((id, p) => childrenOf.putIfAbsent(p, () => []).add(id));

  final weights = <String, int>{};
  int weigh(String id) => weights[id] ??=
      1 + (childrenOf[id] ?? const []).fold(0, (s, c) => s + weigh(c));

  final idsByRing = <int, List<String>>{};
  for (final id in ringKeyById.keys) {
    idsByRing.putIfAbsent(ringOf[id]!, () => []).add(id);
  }
  final ringCount = distinctKeys.length;

  final angleOf = <String, double>{};
  void assignSectors(
      String id, double start, double sweep, double Function(String) sizeOf) {
    final kids = List.of(childrenOf[id] ?? const <String>[])
      ..sort((a, b) {
        final byRing = ringOf[a]!.compareTo(ringOf[b]!);
        return byRing != 0 ? byRing : a.compareTo(b);
      });
    if (kids.isEmpty) return;
    final total = kids.fold(0.0, (s, c) => s + sizeOf(c));
    var cursor = start;
    for (final kid in kids) {
      final share =
          total <= 0 ? sweep / kids.length : sweep * sizeOf(kid) / total;
      angleOf[kid] = cursor + share / 2;
      assignSectors(kid, cursor, share, sizeOf);
      cursor += share;
    }
  }

  List<double> computeRadii() {
    final radii = List<double>.filled(ringCount + 1, 0);
    var prev = 0.0;
    for (var k = 1; k <= ringCount; k++) {
      var r = math.max(prev + _ringGap, _innerRadius);
      final ids = List.of(idsByRing[k] ?? const <String>[])
        ..sort((a, b) => angleOf[a]!.compareTo(angleOf[b]!));
      if (ids.length > 1) {
        // A crowded ring first grows to the circumference its labels need
        // end to end, so labels stay beside their nodes instead of being
        // pushed away to resolve overlaps.
        var perimeter = 0.0;
        for (final id in ids) {
          perimeter += _estimateLabelWidth(byId[id]!.label) + _arcGap;
        }
        r = math.max(r, perimeter / (2 * math.pi));
        // Grow for tight neighbor pairs, but follow the 90th-percentile
        // need rather than the single worst pair -- one tight sector
        // boundary must not inflate the whole ring; the push loop handles
        // the tail. Near-zero gaps may not blow the ring out at all.
        final spikeCap = r + 3 * _ringGap;
        final pairNeeds = <double>[];
        for (var i = 0; i < ids.length; i++) {
          final a = ids[i];
          final b = ids[(i + 1) % ids.length];
          final gap = (angleOf[b]! - angleOf[a]!) % (2 * math.pi);
          if (gap <= 1e-4) continue;
          final need =
              (_estimateLabelWidth(byId[a]!.label) + _estimateLabelWidth(byId[b]!.label)) /
                      2 +
                  _arcGap;
          pairNeeds.add(need / gap);
        }
        if (pairNeeds.isNotEmpty) {
          pairNeeds.sort();
          r = math.max(r, pairNeeds[(0.9 * (pairNeeds.length - 1)).floor()]);
        }
        r = math.min(r, spikeCap);
      }
      radii[k] = r;
      prev = r;
    }
    return radii;
  }

  // First pass sizes sectors by subtree node count (needs no radii); two
  // refinement passes re-divide them by the angular room each subtree's
  // labels need at the current radii (label width over ring radius, summed
  // per ring, maxed across rings), then re-derive the radii. This keeps a
  // few nodes on an otherwise empty ring from being crammed into slivers,
  // and leaves the label push loop only stragglers.
  assignSectors(
      selfId ?? root, -math.pi / 2, 2 * math.pi, (id) => weigh(id).toDouble());
  var radii = computeRadii();
  for (var pass = 0; pass < 2; pass++) {
    final needOf = <String, double>{};
    Map<int, double> accumulate(String id) {
      final sums = <int, double>{};
      final ring = ringOf[id];
      if (ring != null && ring > 0) {
        sums[ring] = (_estimateLabelWidth(byId[id]!.label) + _arcGap) /
            math.max(radii[ring], _innerRadius);
      }
      for (final kid in childrenOf[id] ?? const <String>[]) {
        accumulate(kid).forEach((k, v) => sums[k] = (sums[k] ?? 0) + v);
      }
      needOf[id] = sums.values.fold(0.0, math.max);
      return sums;
    }

    accumulate(selfId ?? root);
    angleOf.clear();
    assignSectors(selfId ?? root, -math.pi / 2, 2 * math.pi, (id) => needOf[id]!);
    radii = computeRadii();
  }

  final positions = <String, Offset>{?selfId: Offset.zero};
  angleOf.forEach((id, theta) {
    positions[id] =
        Offset(math.cos(theta), math.sin(theta)) * radii[ringOf[id]!];
  });

  // Labels anchor on the side of the node facing away from center, so they
  // stay off the radial edge lines. Overlapping labels are pushed outward.
  MapLabel place(String id, Offset pos) {
    final w = _estimateLabelWidth(byId[id]!.label);
    const h = _labelHeight;
    final theta = angleOf[id];
    final dx = theta == null ? 0.0 : math.cos(theta);
    final dy = theta == null ? 1.0 : math.sin(theta);
    if (dx > 0.5) {
      return MapLabel(
          rect: Rect.fromLTWH(pos.dx + _nodeHalf + _labelGap, pos.dy - h / 2, w, h),
          align: TextAlign.left);
    }
    if (dx < -0.5) {
      return MapLabel(
          rect: Rect.fromLTWH(pos.dx - _nodeHalf - _labelGap - w, pos.dy - h / 2, w, h),
          align: TextAlign.right);
    }
    if (dy >= 0) {
      return MapLabel(
          rect: Rect.fromLTWH(pos.dx - w / 2, pos.dy + _nodeHalf + _labelGap, w, h),
          align: TextAlign.center);
    }
    return MapLabel(
        rect: Rect.fromLTWH(pos.dx - w / 2, pos.dy - _nodeHalf - _labelGap - h, w, h),
        align: TextAlign.center);
  }

  final order = positions.keys.toList()
    ..sort((a, b) {
      final byRing = ringOf[a]!.compareTo(ringOf[b]!);
      if (byRing != 0) return byRing;
      final byAngle = (angleOf[a] ?? 0).compareTo(angleOf[b] ?? 0);
      return byAngle != 0 ? byAngle : a.compareTo(b);
    });
  final labels = <String, MapLabel>{};
  final placedRects = <Rect>[];
  for (final id in order) {
    final label = place(id, positions[id]!);
    final theta = angleOf[id] ?? math.pi / 2;
    // One step clears an overlapping label plus its collision margin; try
    // alternating outward/inward so stacked labels split around the ring
    // instead of marching away from their nodes.
    final step = Offset(math.cos(theta), math.sin(theta)) * (_labelHeight + 4);
    var chosen = label;
    for (var attempt = 1;
        attempt <= 6 && placedRects.any((r) => r.overlaps(chosen.rect.inflate(2)));
        attempt++) {
      final shift =
          step * (((attempt + 1) ~/ 2) * (attempt.isOdd ? 1.0 : -1.0));
      chosen = MapLabel(rect: label.rect.shift(shift), align: label.align);
    }
    placedRects.add(chosen.rect);
    labels[id] = chosen;
  }

  Rect? bounds;
  void include(Rect r) => bounds = bounds?.expandToInclude(r) ?? r;
  for (final p in positions.values) {
    include(Rect.fromCircle(center: p, radius: _nodeHalf + 8));
  }
  for (final l in labels.values) {
    include(l.rect);
  }
  final content =
      (bounds ?? const Rect.fromLTWH(-100, -100, 200, 200)).inflate(_contentMargin);
  final shift = -content.topLeft;
  return MapLayout(
    positions: positions.map((id, p) => MapEntry(id, p + shift)),
    labels: labels.map(
        (id, l) => MapEntry(id, MapLabel(rect: l.rect.shift(shift), align: l.align))),
    size: content.size,
    center: shift,
  );
}

/// Below this width the details panel sits along the bottom instead of the
/// right-hand side -- a 280px column would leave nothing of the map.
const double mapDetailsSideBreakpoint = 700;

const double _detailsPanelWidth = 280;
const Duration _mapTransition = Duration(milliseconds: 400);

/// Refresh cadence even when no event arrives: TTL and quality drift on their
/// own, and a backend that never emits the event still stays roughly current.
const Duration mapFallbackRefresh = Duration(seconds: 15);

class MapTab extends StatefulWidget {
  const MapTab({super.key, required this.state});

  final AppState state;

  @override
  State<MapTab> createState() => _MapTabState();
}

class _MapTabState extends State<MapTab> with SingleTickerProviderStateMixin {
  NetworkMapData? _data;
  String? _error;
  bool _peersOnly = false;
  String? _selectedId;

  /// The filtered data on screen and its layout, plus the pair being animated
  /// away from. Both layouts are computed here, never in paint().
  NetworkMapData? _shown;
  MapLayout? _shownLayout;
  NetworkMapData? _previous;
  MapLayout? _previousLayout;

  late final AnimationController _transition;
  final TextEditingController _search = TextEditingController();
  Timer? _poll;
  int _seenRevision = 0;
  bool _fetching = false;

  @override
  void initState() {
    super.initState();
    _transition = AnimationController(vsync: this, duration: _mapTransition, value: 1);
    _seenRevision = widget.state.networkMapRevision;
    widget.state.addListener(_onStateChanged);
    _search.addListener(() => setState(() {}));
    _poll = Timer.periodic(mapFallbackRefresh, (_) => _refresh());
    _refresh();
  }

  @override
  void dispose() {
    widget.state.removeListener(_onStateChanged);
    _poll?.cancel();
    _search.dispose();
    _transition.dispose();
    super.dispose();
  }

  void _onStateChanged() {
    final revision = widget.state.networkMapRevision;
    if (revision == _seenRevision) return;
    _seenRevision = revision;
    _refresh();
  }

  NetworkMapData _filtered(NetworkMapData data) {
    if (!_peersOnly) return data;
    final nodes = data.nodes.where(isPeerNode).toList();
    final kept = nodes.map((n) => n.id).toSet();
    return NetworkMapData(
      nodes: nodes,
      edges: data.edges
          .where((e) => kept.contains(e.src) && kept.contains(e.dst))
          .toList(),
      interfaces: data.interfaces,
      nodeCount: data.nodeCount,
      pathCount: data.pathCount,
      interfaceCount: data.interfaceCount,
      onlinePeerCount: data.onlinePeerCount,
    );
  }

  /// Re-derives what is on screen from [_data] and starts the tween from
  /// whatever was there before. Called for new data and for a filter change,
  /// so both animate the same way.
  void _rebuild() {
    final data = _data;
    if (data == null) return;
    final wasLayout = _shownLayout;
    _previous = _shown;
    _previousLayout = wasLayout;
    _shown = _filtered(data);
    _shownLayout = layoutMapNodes(_shown!);
    if (wasLayout == null) {
      _transition.value = 1;
    } else {
      _transition.forward(from: 0);
    }
  }

  Future<void> _refresh() async {
    if (_fetching) return;
    _fetching = true;
    try {
      final data = await widget.state.api.getNetworkMapData();
      if (!mounted) return;
      setState(() {
        _data = data;
        _error = null;
        _rebuild();
      });
    } catch (e) {
      if (!mounted) return;
      setState(() => _error = e is ApiException ? e.message : e.toString());
    } finally {
      _fetching = false;
    }
  }

  void _handleTap(Offset local, Size canvas) {
    final layout = lerpMapLayout(_previousLayout, _shownLayout!, _transition.value);
    final fit = mapFitFor(canvas, layout.size);
    final radius = (mapHitRadius / fit.scale).clamp(mapHitRadius, 40.0);
    final hit = hitTestMapNode(layout, fit.toContent(local), radius: radius);
    if (hit == _selectedId) return;
    setState(() => _selectedId = hit);
  }

  MapNode? get _selectedNode {
    final id = _selectedId;
    if (id == null) return null;
    for (final n in _shown?.nodes ?? const <MapNode>[]) {
      if (n.id == id) return n;
    }
    return null;
  }

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final data = _data;
    final query = _search.text.trim();
    return Container(
      color: tc.bgApp,
      padding: const EdgeInsets.all(18),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              Text(
                'NETWORK MAP',
                style: TextStyle(
                  fontSize: TCType.textCaption,
                  color: tc.textSecondary,
                  letterSpacing:
                      TCType.letterSpacingFor(TCType.textCaption, TCType.trackingWider),
                ),
              ),
              const SizedBox(width: 12),
              SizedBox(
                width: 172,
                child: TcTextField(
                  label: 'FIND',
                  controller: _search,
                  hintText: 'name or hash…',
                ),
              ),
              const SizedBox(width: 12),
              // End-aligned Wrap rather than a Spacer: identical on a wide
              // window, and the chips drop to a second line on a phone
              // instead of overflowing the row.
              Expanded(
                child: Wrap(
                  alignment: WrapAlignment.end,
                  crossAxisAlignment: WrapCrossAlignment.center,
                  runSpacing: 6,
                  children: [
                    if (data != null) ...[
                      _statChip(tc, '${data.nodeCount} NODES'),
                      const SizedBox(width: 6),
                      _statChip(tc, '${data.pathCount} PATHS'),
                      const SizedBox(width: 6),
                      _statChip(tc,
                          '${data.interfaceCount} IFACE${data.interfaceCount == 1 ? '' : 'S'}'),
                      if (data.onlinePeerCount != null) ...[
                        const SizedBox(width: 6),
                        _statChip(tc, '${data.onlinePeerCount} ONLINE'),
                      ],
                      const SizedBox(width: 8),
                    ],
                    TcGhostButton(icon: TcIcons.sync, label: 'REFRESH', onPressed: _refresh),
                  ],
                ),
              ),
            ],
          ),
          if (_error != null) ...[
            const SizedBox(height: 10),
            Text(
              _error!,
              style: TextStyle(fontSize: TCType.textCaption, color: tc.statusDanger),
            ),
          ],
          const SizedBox(height: 12),
          Expanded(
            child: Container(
              decoration: BoxDecoration(
                color: tc.bgSurface,
                border: Border.all(color: tc.borderSubtle),
              ),
              child: _shown == null
                  ? Center(
                      child: Text(
                        'LOADING…',
                        style: TextStyle(fontSize: TCType.textCaption, color: tc.textTertiary),
                      ),
                    )
                  : ClipRect(child: _mapArea(tc, query)),
            ),
          ),
          const SizedBox(height: 10),
          Row(
            children: [
              TcCheckbox(
                value: _peersOnly,
                label: 'PEERS ONLY',
                onChanged: (v) => setState(() {
                  _peersOnly = v;
                  _rebuild();
                }),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: Wrap(
                  alignment: WrapAlignment.end,
                  crossAxisAlignment: WrapCrossAlignment.center,
                  runSpacing: 6,
                  children: [
                    for (final (label, quality) in const [
                      ('EXCELLENT', 4),
                      ('GOOD', 3),
                      ('FAIR', 2),
                      ('POOR', 1),
                      ('UNKNOWN', 0),
                    ])
                      _legendEntry(tc, label, quality),
                    _kindLegendEntry(tc, 'TRENCHCHAT', filled: true),
                    _kindLegendEntry(tc, 'OTHER NODE', filled: false),
                    _markerLegendEntry(tc, 'ONLINE PEER', tc.statusOnline, round: true),
                    _markerLegendEntry(tc, 'NOMAD', tc.accentSecondary, round: true),
                    _markerLegendEntry(tc, 'PROP NODE', tc.statusWarn, round: true),
                  ],
                ),
              ),
            ],
          ),
        ],
      ),
    );
  }

  Widget _mapArea(TCSectionColors tc, String query) {
    final selected = _selectedNode;
    return LayoutBuilder(
      builder: (context, outer) {
        final side = outer.maxWidth >= mapDetailsSideBreakpoint;
        return Stack(
          children: [
            Positioned.fill(
              child: InteractiveViewer(
                constrained: true,
                minScale: 0.4,
                maxScale: mapMaxScale(outer.biggest, _shownLayout!.size),
                boundaryMargin: const EdgeInsets.all(600),
                child: LayoutBuilder(
                  builder: (context, inner) => GestureDetector(
                    behavior: HitTestBehavior.opaque,
                    onTapUp: (d) => _handleTap(d.localPosition, inner.biggest),
                    child: AnimatedBuilder(
                      animation: _transition,
                      builder: (context, _) => CustomPaint(
                        painter: _NetworkMapPainter(
                          data: _shown!,
                          layout: _shownLayout!,
                          previous: _previous,
                          previousLayout: _previousLayout,
                          t: _transition.value,
                          selectedId: _selectedId,
                          query: query,
                          colors: tc,
                          glow: tcTextGlow(context),
                        ),
                        child: const SizedBox.expand(),
                      ),
                    ),
                  ),
                ),
              ),
            ),
            if (_selectedId != null)
              Positioned(
                right: 0,
                bottom: 0,
                top: side ? 0 : null,
                left: side ? null : 0,
                child: _NodeDetailsPanel(
                  node: selected,
                  selectedId: _selectedId!,
                  data: _shown!,
                  side: side,
                  onClose: () => setState(() => _selectedId = null),
                ),
              ),
          ],
        );
      },
    );
  }

  /// One legend swatch + label. The trailing gap rides inside the entry so an
  /// end-aligned Wrap keeps the same right margin the old Row had.
  Widget _legendEntry(TCSectionColors tc, String label, int quality) => Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Container(width: 8, height: 8, color: mapQualityColor(quality, colors: tc)),
          const SizedBox(width: 4),
          Text(
            label,
            style: TextStyle(
              fontSize: TCType.textMicro,
              color: tc.textSecondary,
              letterSpacing: TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWide),
            ),
          ),
          const SizedBox(width: 10),
        ],
      );

  /// The filled/outlined half of the legend: which nodes on the map run
  /// TrenchChat, independent of the quality color they are painted in.
  Widget _kindLegendEntry(TCSectionColors tc, String label,
          {required bool filled}) =>
      Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Container(
            width: 8,
            height: 8,
            decoration: BoxDecoration(
              color: filled ? tc.textSecondary : null,
              border: Border.all(color: tc.textSecondary),
            ),
          ),
          const SizedBox(width: 4),
          Text(
            label,
            style: TextStyle(
              fontSize: TCType.textMicro,
              color: tc.textSecondary,
              letterSpacing: TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWide),
            ),
          ),
          const SizedBox(width: 10),
        ],
      );

  /// The corner dots a node marker can carry: presence, Nomad, propagation.
  Widget _markerLegendEntry(TCSectionColors tc, String label, Color color,
          {bool round = false}) =>
      Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Container(
            width: 6,
            height: 6,
            decoration: BoxDecoration(
              color: color,
              shape: round ? BoxShape.circle : BoxShape.rectangle,
            ),
          ),
          const SizedBox(width: 4),
          Text(
            label,
            style: TextStyle(
              fontSize: TCType.textMicro,
              color: tc.textSecondary,
              letterSpacing: TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWide),
            ),
          ),
          const SizedBox(width: 10),
        ],
      );

  Widget _statChip(TCSectionColors tc, String label) => Container(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
        decoration: BoxDecoration(
          color: tc.bgInset,
          border: Border.all(color: tc.borderSubtle),
        ),
        child: Text(
          label,
          style: TextStyle(
            fontSize: TCType.textMicro,
            color: tc.textSecondary,
            letterSpacing: TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWide),
          ),
        ),
      );
}

class _NetworkMapPainter extends CustomPainter {
  _NetworkMapPainter({
    required this.data,
    required this.layout,
    required this.colors,
    this.previous,
    this.previousLayout,
    this.t = 1.0,
    this.selectedId,
    this.query = '',
    this.glow,
  });

  final NetworkMapData data;
  final MapLayout layout;

  /// The data/layout being animated away from, or null on the first paint.
  final NetworkMapData? previous;
  final MapLayout? previousLayout;

  /// 0 at the start of the transition, 1 once it is settled.
  final double t;

  final String? selectedId;
  final String query;
  final TCSectionColors colors;

  /// The section's text glow, or null when it has glow off.
  final List<Shadow>? glow;

  @override
  void paint(Canvas canvas, Size size) {
    final eased = Curves.easeInOut.transform(t.clamp(0.0, 1.0));
    final shown = lerpMapLayout(previousLayout, layout, eased);
    final fit = mapFitFor(size, shown.size);
    canvas.save();
    canvas.translate(fit.offset.dx, fit.offset.dy);
    canvas.scale(fit.scale);

    final incident = <String>{};
    if (selectedId != null) {
      incident.add(selectedId!);
      for (final e in data.edges) {
        if (e.src == selectedId) incident.add(e.dst);
        if (e.dst == selectedId) incident.add(e.src);
      }
    }
    final matches = {
      for (final n in data.nodes)
        if (mapNodeMatchesQuery(n, query)) n.id,
    };

    for (final edge in data.edges) {
      final a = shown.positions[edge.src];
      final b = shown.positions[edge.dst];
      if (a == null || b == null) continue;
      final selectedEdge =
          selectedId != null && (edge.src == selectedId || edge.dst == selectedId);
      var alpha = 1.0;
      if (query.isNotEmpty &&
          !(matches.contains(edge.src) && matches.contains(edge.dst))) {
        alpha = mapDimOpacity;
      } else if (selectedId != null && !selectedEdge) {
        alpha = 0.45;
      }
      _drawEdge(canvas, edge, a, b, shown.center,
          alpha: alpha, emphasized: selectedEdge);
    }

    for (final node in data.nodes) {
      final pos = shown.positions[node.id];
      if (pos == null) continue;
      final appearing = previousLayout != null &&
          !previousLayout!.positions.containsKey(node.id);
      var alpha = 1.0;
      if (query.isNotEmpty && !matches.contains(node.id)) {
        alpha = mapDimOpacity;
      } else if (selectedId != null && !incident.contains(node.id)) {
        alpha = 0.7;
      }
      if (appearing) alpha *= eased;
      _drawNode(canvas, node, pos,
          alpha: alpha,
          scale: appearing ? 0.6 + 0.4 * eased : 1.0,
          selected: node.id == selectedId,
          highlighted: query.isNotEmpty && matches.contains(node.id));
    }
    for (final node in data.nodes) {
      final label = shown.labels[node.id];
      if (label == null) continue;
      var alpha = 1.0;
      if (query.isNotEmpty && !matches.contains(node.id)) alpha = mapDimOpacity;
      if (previousLayout != null && !previousLayout!.labels.containsKey(node.id)) {
        alpha *= eased;
      }
      _drawLabel(canvas, node, label, alpha);
    }

    // Whatever the new data dropped, fading out from where it used to be.
    if (previous != null && previousLayout != null && eased < 1) {
      final gone = 1 - eased;
      final live = {for (final n in data.nodes) n.id};
      for (final node in previous!.nodes) {
        if (live.contains(node.id)) continue;
        final pos = previousLayout!.positions[node.id];
        if (pos == null) continue;
        _drawNode(canvas, node, pos, alpha: gone, scale: 1.0);
        final label = previousLayout!.labels[node.id];
        if (label != null) _drawLabel(canvas, node, label, gone);
      }
    }
    canvas.restore();
  }

  Color _fade(Color color, double alpha) =>
      color.withValues(alpha: (color.a * alpha).clamp(0.0, 1.0));

  void _drawEdge(Canvas canvas, MapEdge edge, Offset a, Offset b, Offset center,
      {required double alpha, required bool emphasized}) {
    final delta = b - a;
    final len = delta.distance;
    if (len < 2 * (_nodeHalf + 4)) return;
    final dir = delta / len;
    final start = a + dir * (_nodeHalf + 4);
    final end = b - dir * (_nodeHalf + 4);
    final color = mapQualityColor(edge.quality, colors: colors);
    final base = edge.direct ? 1.0 : 0.35;
    final paint = Paint()
      ..color = _fade(color, base * alpha)
      ..strokeWidth = (edge.direct ? 1.2 : 1) * (emphasized ? 2.0 : 1.0)
      ..style = PaintingStyle.stroke;
    if (edge.direct) {
      canvas.drawLine(start, end, paint);
      return;
    }
    // Bow indirect paths away from center so they don't ride coincident
    // with the direct radial spokes.
    final mid = (start + end) / 2;
    var perp = Offset(-dir.dy, dir.dx);
    final out = mid - center;
    if (perp.dx * out.dx + perp.dy * out.dy < 0) perp = -perp;
    final ctrl = mid + perp * math.min(18.0, len * 0.15);
    canvas.drawPath(
      Path()
        ..moveTo(start.dx, start.dy)
        ..quadraticBezierTo(ctrl.dx, ctrl.dy, end.dx, end.dy),
      paint,
    );
  }

  void _drawNode(Canvas canvas, MapNode node, Offset pos,
      {double alpha = 1.0,
      double scale = 1.0,
      bool selected = false,
      bool highlighted = false}) {
    final half = _nodeHalf * scale;
    final rect = Rect.fromCircle(center: pos, radius: half);
    final diamond = Path()
      ..moveTo(pos.dx, pos.dy - half - 2)
      ..lineTo(pos.dx + half + 2, pos.dy)
      ..lineTo(pos.dx, pos.dy + half + 2)
      ..lineTo(pos.dx - half - 2, pos.dy)
      ..close();

    if (mapHeardRecently(node)) {
      canvas.drawCircle(
        pos,
        half + 5,
        Paint()
          ..color = _fade(mapQualityColor(node.quality, colors: colors), 0.45 * alpha)
          ..style = PaintingStyle.stroke
          ..strokeWidth = 1,
      );
    }

    switch (node.kind) {
      case MapNodeKind.self:
        canvas.drawRect(
          rect.inflate(3),
          Paint()
            ..color = _fade(colors.accentPrimary, 0.25 * alpha)
            ..maskFilter = const MaskFilter.blur(BlurStyle.normal, 6),
        );
        canvas.drawRect(rect, Paint()..color = _fade(colors.accentPrimary, alpha));
      case MapNodeKind.interface_:
        canvas.drawPath(
          diamond,
          Paint()
            ..color = _fade(colors.accentSecondary, alpha)
            ..style = PaintingStyle.stroke
            ..strokeWidth = 1.5,
        );
      case MapNodeKind.transport:
        canvas.drawPath(
            diamond,
            Paint()
              ..color = _fade(mapQualityColor(node.quality, colors: colors), alpha));
        if (showsTrenchChatDot(node)) {
          canvas.drawCircle(Offset(pos.dx + half, pos.dy - half), _tcMarkerRadius,
              Paint()..color = _fade(colors.accentPrimary, alpha));
        }
      case MapNodeKind.peer:
        canvas.drawRect(
          rect,
          Paint()
            ..color = _fade(mapQualityColor(node.quality, colors: colors), alpha)
            ..style = mapPeerStyle(node)
            ..strokeWidth = 1.5,
        );
      case MapNodeKind.unknown:
        canvas.drawRect(
          rect,
          Paint()
            ..color = _fade(colors.statusOffline, alpha)
            ..style = PaintingStyle.stroke
            ..strokeWidth = 1,
        );
    }

    if (node.online == true) {
      canvas.drawCircle(Offset(pos.dx - half - 1, pos.dy - half - 1), _tcMarkerRadius,
          Paint()..color = _fade(colors.statusOnline, alpha));
    }
    if (node.nomad) {
      canvas.drawCircle(Offset(pos.dx - half - 1, pos.dy + half + 1), _tcMarkerRadius,
          Paint()..color = _fade(colors.accentSecondary, alpha));
    }
    if (node.propagation) {
      canvas.drawCircle(Offset(pos.dx + half + 1, pos.dy + half + 1), _tcMarkerRadius,
          Paint()..color = _fade(colors.statusWarn, alpha));
    }

    if (selected || highlighted) {
      canvas.drawCircle(
        pos,
        half + (selected ? 7 : 5),
        Paint()
          ..color = _fade(colors.accentPrimary, selected ? alpha : 0.7 * alpha)
          ..style = PaintingStyle.stroke
          ..strokeWidth = selected ? 2 : 1,
      );
    }
  }

  void _drawLabel(Canvas canvas, MapNode node, MapLabel label, double alpha) {
    final isSelf = node.kind == MapNodeKind.self;
    final painter = TextPainter(
      text: TextSpan(
        text: node.label,
        style: TextStyle(
          fontFamily: TCType.fontMono,
          fontSize: TCType.textMicro,
          color: _fade(isSelf ? colors.textEmphasis : colors.textSecondary, alpha),
          shadows: isSelf && alpha >= 1 ? glow : null,
        ),
      ),
      textDirection: TextDirection.ltr,
      maxLines: 1,
      ellipsis: '…',
    )..layout(maxWidth: _labelMaxWidth);

    final x = switch (label.align) {
      TextAlign.left => label.rect.left,
      TextAlign.right => label.rect.right - painter.width,
      _ => label.rect.center.dx - painter.width / 2,
    };
    final y = label.rect.center.dy - painter.height / 2;
    // Backing box so text stays readable where an edge passes underneath.
    canvas.drawRect(
      Rect.fromLTWH(x - 3, y - 1, painter.width + 6, painter.height + 2),
      Paint()..color = _fade(colors.bgSurface, alpha),
    );
    painter.paint(canvas, Offset(x, y));
  }

  @override
  bool shouldRepaint(covariant _NetworkMapPainter oldDelegate) =>
      oldDelegate.data != data ||
      oldDelegate.layout != layout ||
      oldDelegate.previousLayout != previousLayout ||
      oldDelegate.t != t ||
      oldDelegate.selectedId != selectedId ||
      oldDelegate.query != query ||
      oldDelegate.colors != colors ||
      !listEquals(oldDelegate.glow, glow);
}

String mapShortHex(String hex) =>
    hex.length <= 12 ? hex : '${hex.substring(0, 6)}…${hex.substring(hex.length - 4)}';

/// What a `via` hex points at: the label of that node when it is on the map,
/// a short hex when it is not.
String mapViaLabel(NetworkMapData data, String via) {
  for (final n in data.nodes) {
    if (n.id == via || n.identityHex == via) {
      return n.label.isEmpty ? mapShortHex(via) : n.label;
    }
  }
  return mapShortHex(via);
}

/// Everything the map knows about one node, on the right of a wide tab and
/// along the bottom of a narrow one. A [node] of null means the selection
/// survived a refresh that dropped it.
class _NodeDetailsPanel extends StatelessWidget {
  const _NodeDetailsPanel({
    required this.node,
    required this.selectedId,
    required this.data,
    required this.side,
    required this.onClose,
  });

  final MapNode? node;
  final String selectedId;
  final NetworkMapData data;
  final bool side;
  final VoidCallback onClose;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final n = node;
    return Container(
      width: side ? _detailsPanelWidth : null,
      constraints: side ? null : const BoxConstraints(maxHeight: 260),
      decoration: BoxDecoration(
        color: tc.bgSurfaceRaised,
        border: side
            ? Border(left: BorderSide(color: tc.borderDefault))
            : Border(top: BorderSide(color: tc.borderDefault)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        mainAxisSize: MainAxisSize.min,
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(12, 10, 8, 6),
            child: Row(
              children: [
                Expanded(
                  child: Text(
                    n == null
                        ? mapShortHex(selectedId)
                        : (n.label.isEmpty ? mapShortHex(n.id) : n.label),
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(
                      fontSize: TCType.textBodySm,
                      color: tc.textPrimary,
                    ),
                  ),
                ),
                _IconTap(icon: TcIcons.close, onTap: onClose),
              ],
            ),
          ),
          Flexible(
            child: SingleChildScrollView(
              padding: const EdgeInsets.fromLTRB(12, 0, 12, 12),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: n == null ? _goneRows(tc) : _rowsFor(context, tc, n),
              ),
            ),
          ),
        ],
      ),
    );
  }

  List<Widget> _goneRows(TCSectionColors tc) => [
        Text(
          'NO LONGER VISIBLE — the last refresh dropped this node.',
          style: TextStyle(fontSize: TCType.textCaption, color: tc.textTertiary),
        ),
      ];

  List<Widget> _rowsFor(BuildContext context, TCSectionColors tc, MapNode n) {
    if (n.kind == MapNodeKind.interface_) return _interfaceRows(tc, n);
    if (n.kind == MapNodeKind.self) return _selfRows(context, tc, n);

    final rows = <Widget>[
      _badges(tc, n),
      _row(tc, 'KIND', _kindLabel(n.kind)),
    ];
    if (n.identityHex != null && n.identityHex!.isNotEmpty) {
      rows.add(_copyRow(context, tc, 'IDENTITY', n.identityHex!));
    }
    rows.add(_copyRow(context, tc, 'NODE', n.id));
    rows.add(_row(tc, 'HOPS', '${n.hops}'));
    if (n.via != null && n.via!.isNotEmpty) {
      rows.add(_row(tc, 'VIA', mapViaLabel(data, n.via!)));
    }
    if (n.interfaceName != null && n.interfaceName!.isNotEmpty) {
      rows.add(_row(tc, 'INTERFACE', n.interfaceName!));
    }
    rows.add(_row(tc, 'QUALITY', mapQualityLabel(n.quality),
        valueColor: mapQualityColor(n.quality, colors: tc)));
    if (n.rttMs != null) {
      rows.add(_row(tc, 'RTT', '${n.rttMs!.toStringAsFixed(0)} ms'));
    }
    if (n.lastHeard != null) {
      rows.add(_row(tc, 'LAST HEARD', formatRelativeAgo(n.lastHeard!)));
    }
    if (n.expires != null) {
      rows.add(_row(tc, 'PATH EXPIRES', formatRelativeIn(n.expires!)));
    }
    return rows;
  }

  List<Widget> _interfaceRows(TCSectionColors tc, MapNode n) {
    final name = n.id.startsWith(mapInterfaceIdPrefix)
        ? n.id.substring(mapInterfaceIdPrefix.length)
        : n.label;
    MapInterface? iface;
    for (final i in data.interfaces) {
      if (i.name == name) {
        iface = i;
        break;
      }
    }
    return [
      _row(tc, 'KIND', 'INTERFACE'),
      _row(tc, 'NAME', name),
      _row(tc, 'TYPE', iface?.type ?? '—'),
      _row(tc, 'STATUS', (iface?.status ?? false) ? 'ONLINE' : 'OFFLINE',
          valueColor: (iface?.status ?? false) ? tc.statusOnline : tc.statusOffline),
      _row(tc, 'BITRATE', formatBitrate(iface?.bitrate)),
      _row(tc, 'RX', formatByteCount(iface?.rxb ?? 0)),
      _row(tc, 'TX', formatByteCount(iface?.txb ?? 0)),
    ];
  }

  List<Widget> _selfRows(BuildContext context, TCSectionColors tc, MapNode n) => [
        _row(tc, 'KIND', 'THIS DEVICE'),
        if (n.identityHex != null && n.identityHex!.isNotEmpty)
          _copyRow(context, tc, 'IDENTITY', n.identityHex!)
        else
          _copyRow(context, tc, 'NODE', n.id),
        _row(tc, 'INTERFACES', '${data.interfaceCount}'),
      ];

  String _kindLabel(MapNodeKind kind) => switch (kind) {
        MapNodeKind.self => 'THIS DEVICE',
        MapNodeKind.interface_ => 'INTERFACE',
        MapNodeKind.transport => 'TRANSPORT',
        MapNodeKind.peer => 'PEER',
        MapNodeKind.unknown => 'UNKNOWN',
      };

  Widget _badges(TCSectionColors tc, MapNode n) {
    final badges = <(String, Color)>[
      if (n.isTrenchChat) ('TRENCHCHAT', tc.accentPrimary),
      if (n.nomad) ('NOMAD', tc.accentSecondary),
      if (n.propagation) ('PROPAGATION', tc.statusWarn),
      if (n.online == true) ('ONLINE', tc.statusOnline),
    ];
    if (badges.isEmpty) return const SizedBox(height: 2);
    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: Wrap(
        spacing: 4,
        runSpacing: 4,
        children: [for (final (label, color) in badges) _Badge(label: label, color: color)],
      ),
    );
  }

  Widget _row(TCSectionColors tc, String label, String value, {Color? valueColor}) =>
      Padding(
        padding: const EdgeInsets.only(bottom: 6),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            SizedBox(
              width: 88,
              child: Text(
                label,
                style: TextStyle(
                  fontSize: TCType.textMicro,
                  color: tc.textTertiary,
                  letterSpacing:
                      TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWide),
                ),
              ),
            ),
            Expanded(
              child: Text(
                value,
                style: TextStyle(
                  fontFamily: TCType.fontMono,
                  fontSize: TCType.textCaption,
                  color: valueColor ?? tc.textSecondary,
                ),
              ),
            ),
          ],
        ),
      );

  Widget _copyRow(BuildContext context, TCSectionColors tc, String label, String hex) =>
      Padding(
        padding: const EdgeInsets.only(bottom: 6),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            SizedBox(
              width: 88,
              child: Text(
                label,
                style: TextStyle(
                  fontSize: TCType.textMicro,
                  color: tc.textTertiary,
                  letterSpacing:
                      TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWide),
                ),
              ),
            ),
            Expanded(
              child: Text(
                hex,
                style: TextStyle(
                  fontFamily: TCType.fontMono,
                  fontSize: TCType.textCaption,
                  color: tc.textSecondary,
                ),
              ),
            ),
            const SizedBox(width: 6),
            TcGhostButton(
              label: 'COPY',
              onPressed: () => Clipboard.setData(ClipboardData(text: hex)),
            ),
          ],
        ),
      );
}

class _Badge extends StatelessWidget {
  const _Badge({required this.label, required this.color});

  final String label;
  final Color color;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
      decoration: BoxDecoration(
        color: tc.bgInset,
        border: Border.all(color: color),
      ),
      child: Text(
        label,
        style: TextStyle(
          fontSize: TCType.textMicro,
          color: color,
          letterSpacing: TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWide),
        ),
      ),
    );
  }
}

class _IconTap extends StatelessWidget {
  const _IconTap({required this.icon, required this.onTap});

  final TcIconData icon;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return MouseRegion(
      cursor: SystemMouseCursors.click,
      child: GestureDetector(
        onTap: onTap,
        behavior: HitTestBehavior.opaque,
        child: Padding(padding: const EdgeInsets.all(4), child: TcIcon(icon, size: 12)),
      ),
    );
  }
}
