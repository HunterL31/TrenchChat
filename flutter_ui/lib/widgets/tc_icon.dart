// TrenchChat icon pack: stroke-drawn vector glyphs on a 16-unit grid.
// Same language as the panel notch -- hard angles, 45-degree chamfers, butt
// caps, miter joins. Add an icon by extending the catalog, not by importing
// Material icons; the rounded Material style clashes with this design.
import 'dart:math' as math;

import 'package:flutter/widgets.dart';

import '../theme/section_theme.dart';

/// One glyph: polylines and filled polygons in a 16x16 coordinate space.
/// A polyline whose first and last points are equal is drawn closed.
class TcIconData {
  const TcIconData(this.name, this.strokes, {this.fills = const []});

  final String name;
  final List<List<Offset>> strokes;
  final List<List<Offset>> fills;
}

/// The icon catalog.
class TcIcons {
  TcIcons._();

  static const settings = TcIconData('settings', [
    [
      Offset(11.45, 7.07), Offset(12.99, 7.3), Offset(12.99, 8.7), Offset(11.45, 8.93),
      Offset(11.1, 9.79), Offset(12.02, 11.03), Offset(11.03, 12.02), Offset(9.79, 11.1),
      Offset(8.93, 11.45), Offset(8.7, 12.99), Offset(7.3, 12.99), Offset(7.07, 11.45),
      Offset(6.21, 11.1), Offset(4.97, 12.02), Offset(3.98, 11.03), Offset(4.9, 9.79),
      Offset(4.55, 8.93), Offset(3.01, 8.7), Offset(3.01, 7.3), Offset(4.55, 7.07),
      Offset(4.9, 6.21), Offset(3.98, 4.97), Offset(4.97, 3.98), Offset(6.21, 4.9),
      Offset(7.07, 4.55), Offset(7.3, 3.01), Offset(8.7, 3.01), Offset(8.93, 4.55),
      Offset(9.79, 4.9), Offset(11.03, 3.98), Offset(12.02, 4.97), Offset(11.1, 6.21),
      Offset(11.45, 7.07),
    ],
    [
      Offset(9.55, 8.64), Offset(8.64, 9.55), Offset(7.36, 9.55), Offset(6.45, 8.64),
      Offset(6.45, 7.36), Offset(7.36, 6.45), Offset(8.64, 6.45), Offset(9.55, 7.36),
      Offset(9.55, 8.64),
    ],
  ]);

  static const lock = TcIconData('lock', [
    [
      Offset(5.5, 6.5), Offset(5.5, 4.25), Offset(6.75, 3), Offset(9.25, 3),
      Offset(10.5, 4.25), Offset(10.5, 6.5),
    ],
    [
      Offset(3.5, 6.5), Offset(10.75, 6.5), Offset(12.5, 8.25), Offset(12.5, 13),
      Offset(3.5, 13), Offset(3.5, 6.5),
    ],
    [
      Offset(8, 9), Offset(8, 10.75),
    ],
  ]);

  static const plus = TcIconData('plus', [
    [
      Offset(8, 3.25), Offset(8, 12.75),
    ],
    [
      Offset(3.25, 8), Offset(12.75, 8),
    ],
  ]);

  static const join = TcIconData('join', [
    [
      Offset(9.75, 3.5), Offset(12.75, 3.5), Offset(12.75, 12.5), Offset(9.75, 12.5),
    ],
    [
      Offset(3.25, 8), Offset(9.75, 8),
    ],
    [
      Offset(7, 5.25), Offset(9.75, 8), Offset(7, 10.75),
    ],
  ]);

  static const emoji = TcIconData('emoji', [
    [
      Offset(6, 3), Offset(10, 3), Offset(13, 6), Offset(13, 10), Offset(10, 13), Offset(6, 13),
      Offset(3, 10), Offset(3, 6), Offset(6, 3),
    ],
    [
      Offset(6, 6.5), Offset(6, 8),
    ],
    [
      Offset(10, 6.5), Offset(10, 8),
    ],
    [
      Offset(5.75, 10), Offset(7, 11.25), Offset(9, 11.25), Offset(10.25, 10),
    ],
  ]);

  static const hash = TcIconData('hash', [
    [
      Offset(6.5, 3), Offset(5.5, 13),
    ],
    [
      Offset(10.5, 3), Offset(9.5, 13),
    ],
    [
      Offset(3.25, 6.25), Offset(13.25, 6.25),
    ],
    [
      Offset(2.75, 9.75), Offset(12.75, 9.75),
    ],
  ]);

  static const users = TcIconData('users', [
    [
      Offset(4.75, 3.38), Offset(7.75, 3.38), Offset(7.75, 6.38), Offset(4.75, 6.38),
      Offset(4.75, 3.38),
    ],
    [
      Offset(3, 12.62), Offset(3, 11.12), Offset(4.75, 9.38), Offset(7.75, 9.38),
      Offset(9.5, 11.12), Offset(9.5, 12.62),
    ],
    [
      Offset(10.5, 4.12), Offset(13, 4.12), Offset(13, 6.62), Offset(10.5, 6.62),
      Offset(10.5, 4.12),
    ],
    [
      Offset(11, 9.03), Offset(12.5, 10.22), Offset(12.5, 12.62),
    ],
  ]);

  static const close = TcIconData('close', [
    [
      Offset(3.24, 3.24), Offset(12.76, 12.76),
    ],
    [
      Offset(12.76, 3.24), Offset(3.24, 12.76),
    ],
  ]);

  static const search = TcIconData('search', [
    [
      Offset(5, 3), Offset(8.5, 3), Offset(10.5, 5), Offset(10.5, 8.5), Offset(8.5, 10.5),
      Offset(5, 10.5), Offset(3, 8.5), Offset(3, 5), Offset(5, 3),
    ],
    [
      Offset(10.4, 10.4), Offset(13.25, 13.25),
    ],
  ]);

  static const send = TcIconData('send', [
    [
      Offset(3.38, 4), Offset(8.38, 8), Offset(3.38, 12),
    ],
    [
      Offset(9.62, 12), Offset(12.62, 12),
    ],
  ]);

  static const sync = TcIconData('sync', [
    [
      Offset(3.5, 9.5), Offset(3.5, 5.5), Offset(12.5, 5.5),
    ],
    [
      Offset(10.5, 3.5), Offset(12.5, 5.5), Offset(10.5, 7.5),
    ],
    [
      Offset(12.5, 6.5), Offset(12.5, 10.5), Offset(3.5, 10.5),
    ],
    [
      Offset(5.5, 8.5), Offset(3.5, 10.5), Offset(5.5, 12.5),
    ],
  ]);

  static const map = TcIconData('map', [
    [
      Offset(4, 3), Offset(12, 3), Offset(12, 8), Offset(8, 13),
      Offset(4, 8), Offset(4, 3),
    ],
    [
      Offset(7, 5.75), Offset(9, 5.75), Offset(9, 7.75), Offset(7, 7.75),
      Offset(7, 5.75),
    ],
  ]);

  static const iface = TcIconData('iface', [
    [
      Offset(8, 5.9), Offset(8, 12.8),
    ],
    [
      Offset(5.4, 3.2), Offset(3.9, 4.8), Offset(5.4, 6.4),
    ],
    [
      Offset(10.6, 3.2), Offset(12.1, 4.8), Offset(10.6, 6.4),
    ],
    [
      Offset(5.5, 12.8), Offset(10.5, 12.8),
    ],
  ], fills: [
    [
      Offset(8, 3.7), Offset(9.1, 4.8), Offset(8, 5.9), Offset(6.9, 4.8),
    ],
  ]);
  static const mic = TcIconData('mic', [
    [
      Offset(6.25, 3.5), Offset(7, 2.75), Offset(9, 2.75), Offset(9.75, 3.5),
      Offset(9.75, 8), Offset(9, 8.75), Offset(7, 8.75), Offset(6.25, 8),
      Offset(6.25, 3.5),
    ],
    [
      Offset(4.25, 7), Offset(4.25, 9), Offset(6.25, 11), Offset(9.75, 11),
      Offset(11.75, 9), Offset(11.75, 7),
    ],
    [
      Offset(8, 11), Offset(8, 13.25),
    ],
    [
      Offset(5.5, 13.25), Offset(10.5, 13.25),
    ],
  ]);

  static const micMuted = TcIconData('micMuted', [
    [
      Offset(6.25, 3.5), Offset(7, 2.75), Offset(9, 2.75), Offset(9.75, 3.5),
      Offset(9.75, 8), Offset(9, 8.75), Offset(7, 8.75), Offset(6.25, 8),
      Offset(6.25, 3.5),
    ],
    [
      Offset(4.25, 7), Offset(4.25, 9), Offset(6.25, 11), Offset(9.75, 11),
      Offset(11.75, 9), Offset(11.75, 7),
    ],
    [
      Offset(8, 11), Offset(8, 13.25),
    ],
    [
      Offset(5.5, 13.25), Offset(10.5, 13.25),
    ],
    [
      Offset(4.25, 4.25), Offset(11.75, 11.75),
    ],
  ]);

  static const headset = TcIconData('headset', [
    [
      Offset(3.5, 9.5), Offset(3.5, 6), Offset(6, 3.5), Offset(10, 3.5),
      Offset(12.5, 6), Offset(12.5, 9.5),
    ],
    [
      Offset(3, 9.5), Offset(5.25, 9.5), Offset(5.25, 12.75), Offset(3, 12.75),
      Offset(3, 9.5),
    ],
    [
      Offset(10.75, 9.5), Offset(13, 9.5), Offset(13, 12.75), Offset(10.75, 12.75),
      Offset(10.75, 9.5),
    ],
  ]);

  static const menu = TcIconData('menu', [
    [
      Offset(3, 4.5), Offset(13, 4.5),
    ],
    [
      Offset(3, 8), Offset(13, 8),
    ],
    [
      Offset(3, 11.5), Offset(13, 11.5),
    ],
  ]);

  static const globe = TcIconData('globe', [
    [
      Offset(8, 3.25), Offset(11.36, 4.64), Offset(12.75, 8), Offset(11.36, 11.36),
      Offset(8, 12.75), Offset(4.64, 11.36), Offset(3.25, 8), Offset(4.64, 4.64),
      Offset(8, 3.25),
    ],
    [
      Offset(3.25, 8), Offset(12.75, 8),
    ],
    [
      Offset(8, 3.25), Offset(9.9, 5.37), Offset(10.55, 8), Offset(9.9, 10.63),
      Offset(8, 12.75),
    ],
    [
      Offset(8, 3.25), Offset(6.1, 5.37), Offset(5.45, 8), Offset(6.1, 10.63),
      Offset(8, 12.75),
    ],
  ]);

  static const List<TcIconData> all = [
    settings, lock, plus, join, emoji, hash, users, close, search, send, sync, map, iface,
    mic, micMuted, headset, menu, globe,
  ];
}

/// Renders a [TcIconData] at [size], stroked in [color] (defaults to the
/// enclosing section's `textSecondary`).
class TcIcon extends StatelessWidget {
  const TcIcon(this.icon, {super.key, this.size = 16, this.color});

  final TcIconData icon;
  final double size;
  final Color? color;

  @override
  Widget build(BuildContext context) {
    return CustomPaint(
      size: Size.square(size),
      painter: _TcIconPainter(icon: icon, color: color ?? SectionTheme.of(context).textSecondary),
    );
  }
}

class _TcIconPainter extends CustomPainter {
  const _TcIconPainter({required this.icon, required this.color});

  static const double _grid = 16;
  static const double _strokeWidth = 1.5;

  final TcIconData icon;
  final Color color;

  /// Centre of an [width]-wide run that covers whole device pixels: a
  /// half-integer for an odd width, an integer for an even one. Only the
  /// axis a run is aligned on is snapped, so diagonals keep their angle.
  static double _snap(double value, double width) =>
      width.toInt().isOdd ? (value - 0.5).roundToDouble() + 0.5 : value.roundToDouble();

  Path _path(List<Offset> points, double scale, double? strokeWidth) {
    final closed = points.first == points.last;
    final upper = closed ? points.length - 1 : points.length;
    final placed = <Offset>[];
    for (int i = 0; i < upper; i++) {
      final p = points[i];
      double x = p.dx * scale;
      double y = p.dy * scale;
      if (strokeWidth != null) {
        final before = i > 0 ? points[i - 1] : (closed ? points[upper - 1] : null);
        final after = i < upper - 1 ? points[i + 1] : (closed ? points[0] : null);
        if (before?.dx == p.dx || after?.dx == p.dx) x = _snap(x, strokeWidth);
        if (before?.dy == p.dy || after?.dy == p.dy) y = _snap(y, strokeWidth);
      }
      placed.add(Offset(x, y));
    }
    final path = Path()..moveTo(placed.first.dx, placed.first.dy);
    for (int i = 1; i < placed.length; i++) {
      path.lineTo(placed[i].dx, placed[i].dy);
    }
    if (closed) path.close();
    return path;
  }

  @override
  void paint(Canvas canvas, Size size) {
    final scale = size.shortestSide / _grid;
    final width = math.max(1.0, (_strokeWidth * scale).roundToDouble());
    final stroke = Paint()
      ..color = color
      ..style = PaintingStyle.stroke
      ..strokeWidth = width
      ..strokeCap = StrokeCap.butt
      ..strokeJoin = StrokeJoin.miter;
    for (final points in icon.strokes) {
      canvas.drawPath(_path(points, scale, width), stroke);
    }
    final fill = Paint()..color = color;
    for (final points in icon.fills) {
      canvas.drawPath(_path([...points, points.first], scale, null), fill);
    }
  }

  @override
  bool shouldRepaint(covariant _TcIconPainter oldDelegate) =>
      oldDelegate.icon != icon || oldDelegate.color != color;
}
