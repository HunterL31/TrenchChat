// MAP tab -- port of network_map.py's NetworkMapWidget over GET /network/map.
// The Qt widget runs a force-directed layout; this uses a deterministic radial
// one (self centered, interfaces on the inner ring, peers ringed by hop count)
// so the picture is stable across refreshes and testable. Nodes are placed in
// angular sectors under the node they route through, ring radii grow to fit
// their labels, and labels anchor on the side of the node facing away from
// center so they stay off the edge lines.
import 'dart:math' as math;

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import '../../api/client.dart';
import '../../api/models/network_map.dart';
import '../../app_state.dart';
import '../../theme/glow.dart';
import '../../theme/quality_tiers.dart';
import '../../theme/section_theme.dart';
import '../../theme/theme_spec.dart';
import '../../theme/tokens.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_checkbox.dart';
import '../../widgets/tc_icon.dart';

/// Same tiers as the Qt map's _COL_QUALITY and SignalMeter, so the map
/// agrees with the header's link chip: 4=excellent .. 1=poor, 0=unknown.
/// [colors] defaults to the stock palette so the tear-off stays a
/// `Color Function(int)`.
Color mapQualityColor(int quality, {TCSectionColors? colors}) =>
    tcQualityColor(quality, colors ?? TCSectionColors.stock);

/// Nodes kept by the "peers only" filter: this device plus nodes known to
/// run TrenchChat, not plain Reticulum/LXMF infrastructure (interfaces,
/// relays, nodes that never announced as TrenchChat users).
bool isPeerNode(MapNode node) =>
    node.kind == MapNodeKind.self ||
    (node.isTrenchChat && node.kind != MapNodeKind.interface_);

const double _nodeHalf = 5.0;
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

class MapTab extends StatefulWidget {
  const MapTab({super.key, required this.state});

  final AppState state;

  @override
  State<MapTab> createState() => _MapTabState();
}

class _MapTabState extends State<MapTab> {
  NetworkMapData? _data;
  String? _error;
  bool _peersOnly = false;

  @override
  void initState() {
    super.initState();
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
    );
  }

  Future<void> _refresh() async {
    try {
      final data = await widget.state.api.getNetworkMapData();
      if (!mounted) return;
      setState(() {
        _data = data;
        _error = null;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() => _error = e is ApiException ? e.message : e.toString());
    }
  }

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final data = _data;
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
              child: data == null
                  ? Center(
                      child: Text(
                        'LOADING…',
                        style: TextStyle(fontSize: TCType.textCaption, color: tc.textTertiary),
                      ),
                    )
                  : ClipRect(
                      child: InteractiveViewer(
                        constrained: true,
                        minScale: 0.4,
                        maxScale: 4,
                        boundaryMargin: const EdgeInsets.all(600),
                        child: CustomPaint(
                          painter: _NetworkMapPainter(
                            data: _filtered(data),
                            colors: tc,
                            glow: tcTextGlow(context),
                          ),
                          child: const SizedBox.expand(),
                        ),
                      ),
                    ),
            ),
          ),
          const SizedBox(height: 10),
          Row(
            children: [
              TcCheckbox(
                value: _peersOnly,
                label: 'PEERS ONLY',
                onChanged: (v) => setState(() => _peersOnly = v),
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
                  ],
                ),
              ),
            ],
          ),
        ],
      ),
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
  _NetworkMapPainter({required this.data, required this.colors, this.glow});

  final NetworkMapData data;
  final TCSectionColors colors;

  /// The section's text glow, or null when it has glow off.
  final List<Shadow>? glow;

  @override
  void paint(Canvas canvas, Size size) {
    final layout = layoutMapNodes(data);
    final fit = math.min(1.0,
        math.min(size.width / layout.size.width, size.height / layout.size.height));
    canvas.save();
    canvas.translate((size.width - layout.size.width * fit) / 2,
        (size.height - layout.size.height * fit) / 2);
    canvas.scale(fit);

    for (final edge in data.edges) {
      final a = layout.positions[edge.src];
      final b = layout.positions[edge.dst];
      if (a == null || b == null) continue;
      final delta = b - a;
      final len = delta.distance;
      if (len < 2 * (_nodeHalf + 4)) continue;
      final dir = delta / len;
      final start = a + dir * (_nodeHalf + 4);
      final end = b - dir * (_nodeHalf + 4);
      final color = mapQualityColor(edge.quality, colors: colors);
      final paint = Paint()
        ..color = edge.direct ? color : color.withValues(alpha: 0.35)
        ..strokeWidth = edge.direct ? 1.2 : 1
        ..style = PaintingStyle.stroke;
      if (edge.direct) {
        canvas.drawLine(start, end, paint);
      } else {
        // Bow indirect paths away from center so they don't ride coincident
        // with the direct radial spokes.
        final mid = (start + end) / 2;
        var perp = Offset(-dir.dy, dir.dx);
        final out = mid - layout.center;
        if (perp.dx * out.dx + perp.dy * out.dy < 0) perp = -perp;
        final ctrl = mid + perp * math.min(18.0, len * 0.15);
        canvas.drawPath(
          Path()
            ..moveTo(start.dx, start.dy)
            ..quadraticBezierTo(ctrl.dx, ctrl.dy, end.dx, end.dy),
          paint,
        );
      }
    }

    for (final node in data.nodes) {
      final pos = layout.positions[node.id];
      if (pos == null) continue;
      _drawNode(canvas, node, pos);
    }
    for (final node in data.nodes) {
      final label = layout.labels[node.id];
      if (label == null) continue;
      _drawLabel(canvas, node, label);
    }
    canvas.restore();
  }

  void _drawNode(Canvas canvas, MapNode node, Offset pos) {
    const half = _nodeHalf;
    final rect = Rect.fromCircle(center: pos, radius: half);
    final diamond = Path()
      ..moveTo(pos.dx, pos.dy - half - 2)
      ..lineTo(pos.dx + half + 2, pos.dy)
      ..lineTo(pos.dx, pos.dy + half + 2)
      ..lineTo(pos.dx - half - 2, pos.dy)
      ..close();

    switch (node.kind) {
      case MapNodeKind.self:
        canvas.drawRect(
          rect.inflate(3),
          Paint()
            ..color = colors.accentPrimary.withValues(alpha: 0.25)
            ..maskFilter = const MaskFilter.blur(BlurStyle.normal, 6),
        );
        canvas.drawRect(rect, Paint()..color = colors.accentPrimary);
      case MapNodeKind.interface_:
        canvas.drawPath(
          diamond,
          Paint()
            ..color = colors.accentSecondary
            ..style = PaintingStyle.stroke
            ..strokeWidth = 1.5,
        );
      case MapNodeKind.transport:
        canvas.drawPath(diamond, Paint()..color = mapQualityColor(node.quality, colors: colors));
      case MapNodeKind.peer:
        canvas.drawRect(
          rect,
          Paint()
            ..color = mapQualityColor(node.quality, colors: colors)
            ..style = PaintingStyle.stroke
            ..strokeWidth = 1.5,
        );
      case MapNodeKind.unknown:
        canvas.drawRect(
          rect,
          Paint()
            ..color = colors.statusOffline
            ..style = PaintingStyle.stroke
            ..strokeWidth = 1,
        );
    }
  }

  void _drawLabel(Canvas canvas, MapNode node, MapLabel label) {
    final painter = TextPainter(
      text: TextSpan(
        text: node.label,
        style: TextStyle(
          fontFamily: TCType.fontMono,
          fontSize: TCType.textMicro,
          color: node.kind == MapNodeKind.self
              ? colors.textEmphasis
              : colors.textSecondary,
          shadows: node.kind == MapNodeKind.self ? glow : null,
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
      Paint()..color = colors.bgSurface,
    );
    painter.paint(canvas, Offset(x, y));
  }

  @override
  bool shouldRepaint(covariant _NetworkMapPainter oldDelegate) =>
      oldDelegate.data != data ||
      oldDelegate.colors != colors ||
      !listEquals(oldDelegate.glow, glow);
}
