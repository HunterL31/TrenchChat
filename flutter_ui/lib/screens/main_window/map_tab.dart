// MAP tab -- port of network_map.py's NetworkMapWidget over GET /network/map.
// The Qt widget runs a force-directed layout; this uses a deterministic
// radial one (self centered, interfaces on the inner ring, peers ringed by
// hop count) so the picture is stable across refreshes and testable.
import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../../api/client.dart';
import '../../api/models/network_map.dart';
import '../../app_state.dart';
import '../../theme/effects.dart';
import '../../theme/tokens.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_checkbox.dart';
import '../../widgets/tc_icon.dart';

/// Same tiers as the Qt map's _COL_QUALITY and SignalMeter, so the map
/// agrees with the header's link chip: 4=excellent .. 1=poor, 0=unknown.
Color mapQualityColor(int quality) => switch (quality) {
      4 => TCColors.green400,
      3 => HSLColor.fromAHSL(1, 70, 0.85, 0.55).toColor(),
      2 => TCColors.amber400,
      1 => TCColors.statusDanger,
      _ => TCColors.ink500,
    };

/// Nodes kept by the "peers only" filter: real TrenchChat identities, not
/// infrastructure (interfaces, transports, unresolved hashes).
bool isPeerNode(MapNode node) =>
    node.kind == MapNodeKind.self || node.kind == MapNodeKind.peer;

/// Radial positions for every node, in [size] coordinates. Self sits at the
/// center; interfaces on the innermost ring; everything else on a ring per
/// hop count. Deterministic: nodes are ordered by id within each ring.
Map<String, Offset> layoutMapNodes(NetworkMapData data, Size size) {
  final center = Offset(size.width / 2, size.height / 2);
  final positions = <String, Offset>{};

  final rings = <int, List<MapNode>>{};
  for (final node in data.nodes) {
    if (node.kind == MapNodeKind.self) {
      positions[node.id] = center;
      continue;
    }
    final ring = node.kind == MapNodeKind.interface_ ? 1 : (node.hops.clamp(1, 6) + 1);
    rings.putIfAbsent(ring, () => []).add(node);
  }
  if (rings.isEmpty) return positions;

  final maxRing = rings.keys.reduce(math.max);
  final maxRadius = math.min(size.width, size.height) / 2 - 40;
  for (final entry in rings.entries) {
    final nodes = entry.value..sort((a, b) => a.id.compareTo(b.id));
    final radius = maxRadius * entry.key / maxRing;
    for (int i = 0; i < nodes.length; i++) {
      // Start at -90deg so a lone node sits above the center; stagger rings
      // half a slot so adjacent rings don't line up into a single spoke.
      final angle = -math.pi / 2 +
          2 * math.pi * i / nodes.length +
          (entry.key.isEven ? math.pi / nodes.length : 0);
      positions[nodes[i].id] =
          center + Offset(math.cos(angle), math.sin(angle)) * radius;
    }
  }
  return positions;
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
    final data = _data;
    return Container(
      color: TCColors.bgApp,
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
                  color: TCColors.textSecondary,
                  letterSpacing:
                      TCType.letterSpacingFor(TCType.textCaption, TCType.trackingWider),
                ),
              ),
              const Spacer(),
              if (data != null) ...[
                _statChip('${data.nodeCount} NODES'),
                const SizedBox(width: 6),
                _statChip('${data.pathCount} PATHS'),
                const SizedBox(width: 6),
                _statChip('${data.interfaceCount} IFACES'),
                const SizedBox(width: 8),
              ],
              TcGhostButton(icon: TcIcons.sync, label: 'REFRESH', onPressed: _refresh),
            ],
          ),
          if (_error != null) ...[
            const SizedBox(height: 10),
            Text(
              _error!,
              style: TextStyle(fontSize: TCType.textCaption, color: TCColors.statusDanger),
            ),
          ],
          const SizedBox(height: 12),
          Expanded(
            child: Container(
              decoration: BoxDecoration(
                color: TCColors.bgSurface,
                border: Border.all(color: TCColors.borderSubtle),
              ),
              child: data == null
                  ? Center(
                      child: Text(
                        'LOADING…',
                        style: TextStyle(
                            fontSize: TCType.textCaption, color: TCColors.textTertiary),
                      ),
                    )
                  : ClipRect(
                      child: InteractiveViewer(
                        constrained: true,
                        minScale: 0.4,
                        maxScale: 4,
                        boundaryMargin: const EdgeInsets.all(600),
                        child: CustomPaint(
                          painter: _NetworkMapPainter(data: _filtered(data)),
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
              const Spacer(),
              for (final (label, quality) in const [
                ('EXCELLENT', 4),
                ('GOOD', 3),
                ('FAIR', 2),
                ('POOR', 1),
                ('UNKNOWN', 0),
              ]) ...[
                Container(width: 8, height: 8, color: mapQualityColor(quality)),
                const SizedBox(width: 4),
                Text(
                  label,
                  style: TextStyle(
                    fontSize: TCType.textMicro,
                    color: TCColors.textSecondary,
                    letterSpacing:
                        TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWide),
                  ),
                ),
                const SizedBox(width: 10),
              ],
            ],
          ),
        ],
      ),
    );
  }

  Widget _statChip(String label) => Container(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
        decoration: BoxDecoration(
          color: TCColors.bgInset,
          border: Border.all(color: TCColors.borderSubtle),
        ),
        child: Text(
          label,
          style: TextStyle(
            fontSize: TCType.textMicro,
            color: TCColors.textSecondary,
            letterSpacing: TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWide),
          ),
        ),
      );
}

class _NetworkMapPainter extends CustomPainter {
  _NetworkMapPainter({required this.data});

  final NetworkMapData data;

  @override
  void paint(Canvas canvas, Size size) {
    final positions = layoutMapNodes(data, size);

    for (final edge in data.edges) {
      final a = positions[edge.src];
      final b = positions[edge.dst];
      if (a == null || b == null) continue;
      final color = mapQualityColor(edge.quality);
      final paint = Paint()
        ..color = edge.direct ? color : color.withValues(alpha: 0.35)
        ..strokeWidth = edge.direct ? 1.2 : 1;
      canvas.drawLine(a, b, paint);
    }

    for (final node in data.nodes) {
      final pos = positions[node.id];
      if (pos == null) continue;
      _drawNode(canvas, node, pos);
    }
  }

  void _drawNode(Canvas canvas, MapNode node, Offset pos) {
    const half = 5.0;
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
            ..color = TCColors.accentPrimary.withValues(alpha: 0.25)
            ..maskFilter = const MaskFilter.blur(BlurStyle.normal, 6),
        );
        canvas.drawRect(rect, Paint()..color = TCColors.accentPrimary);
      case MapNodeKind.interface_:
        canvas.drawPath(
          diamond,
          Paint()
            ..color = TCColors.accentSecondary
            ..style = PaintingStyle.stroke
            ..strokeWidth = 1.5,
        );
      case MapNodeKind.transport:
        canvas.drawPath(diamond, Paint()..color = mapQualityColor(node.quality));
      case MapNodeKind.peer:
        canvas.drawRect(
          rect,
          Paint()
            ..color = mapQualityColor(node.quality)
            ..style = PaintingStyle.stroke
            ..strokeWidth = 1.5,
        );
      case MapNodeKind.unknown:
        canvas.drawRect(
          rect,
          Paint()
            ..color = TCColors.ink500
            ..style = PaintingStyle.stroke
            ..strokeWidth = 1,
        );
    }

    final painter = TextPainter(
      text: TextSpan(
        text: node.label,
        style: TextStyle(
          fontFamily: TCType.fontMono,
          fontSize: TCType.textMicro,
          color: node.kind == MapNodeKind.self ? TCColors.green100 : TCColors.textSecondary,
          shadows: node.kind == MapNodeKind.self ? [TCEffects.textGlowGreen] : null,
        ),
      ),
      textDirection: TextDirection.ltr,
      maxLines: 1,
      ellipsis: '…',
    )..layout(maxWidth: 140);
    painter.paint(canvas, pos + Offset(-painter.width / 2, half + 6));
  }

  @override
  bool shouldRepaint(covariant _NetworkMapPainter oldDelegate) => oldDelegate.data != data;
}
