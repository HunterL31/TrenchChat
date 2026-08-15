// TrenchChat icon pack: stroke-drawn vector glyphs on a 16-unit grid.
// Same language as the panel notch -- hard angles, 45-degree chamfers, butt
// caps, miter joins. Add an icon by extending the catalog, not by importing
// Material icons; the rounded Material style clashes with this design.
import 'package:flutter/widgets.dart';

import '../theme/tokens.dart';

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
      Offset(12.73, 6.73), Offset(14.83, 7.04), Offset(14.83, 8.96), Offset(12.73, 9.27),
      Offset(12.24, 10.45), Offset(13.51, 12.15), Offset(12.15, 13.51), Offset(10.45, 12.24),
      Offset(9.27, 12.73), Offset(8.96, 14.83), Offset(7.04, 14.83), Offset(6.73, 12.73),
      Offset(5.55, 12.24), Offset(3.85, 13.51), Offset(2.49, 12.15), Offset(3.76, 10.45),
      Offset(3.27, 9.27), Offset(1.17, 8.96), Offset(1.17, 7.04), Offset(3.27, 6.73),
      Offset(3.76, 5.55), Offset(2.49, 3.85), Offset(3.85, 2.49), Offset(5.55, 3.76),
      Offset(6.73, 3.27), Offset(7.04, 1.17), Offset(8.96, 1.17), Offset(9.27, 3.27),
      Offset(10.45, 3.76), Offset(12.15, 2.49), Offset(13.51, 3.85), Offset(12.24, 5.55),
      Offset(12.73, 6.73),
    ],
    [
      Offset(10.12, 8.88), Offset(8.88, 10.12), Offset(7.12, 10.12), Offset(5.88, 8.88),
      Offset(5.88, 7.12), Offset(7.12, 5.88), Offset(8.88, 5.88), Offset(10.12, 7.12),
      Offset(10.12, 8.88),
    ],
  ]);

  static const lock = TcIconData('lock', [
    [
      Offset(5.5, 7), Offset(5.5, 4.75), Offset(6.75, 3.5), Offset(9.25, 3.5),
      Offset(10.5, 4.75), Offset(10.5, 7),
    ],
    [
      Offset(3.5, 7), Offset(10.75, 7), Offset(12.5, 8.75), Offset(12.5, 13.5),
      Offset(3.5, 13.5), Offset(3.5, 7),
    ],
    [
      Offset(8, 9.5), Offset(8, 11.25),
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
      Offset(9.5, 3.5), Offset(12.5, 3.5), Offset(12.5, 12.5), Offset(9.5, 12.5),
    ],
    [
      Offset(3, 8), Offset(9.5, 8),
    ],
    [
      Offset(6.75, 5.25), Offset(9.5, 8), Offset(6.75, 10.75),
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
      Offset(4.75, 3.75), Offset(7.75, 3.75), Offset(7.75, 6.75), Offset(4.75, 6.75),
      Offset(4.75, 3.75),
    ],
    [
      Offset(3, 13), Offset(3, 11.5), Offset(4.75, 9.75), Offset(7.75, 9.75), Offset(9.5, 11.5),
      Offset(9.5, 13),
    ],
    [
      Offset(10.5, 4.5), Offset(13, 4.5), Offset(13, 7), Offset(10.5, 7), Offset(10.5, 4.5),
    ],
    [
      Offset(11, 9.4), Offset(12.5, 10.6), Offset(12.5, 13),
    ],
  ]);

  static const close = TcIconData('close', [
    [
      Offset(4.25, 4.25), Offset(11.75, 11.75),
    ],
    [
      Offset(11.75, 4.25), Offset(4.25, 11.75),
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
      Offset(4.5, 4), Offset(9.5, 8), Offset(4.5, 12),
    ],
    [
      Offset(10.75, 12), Offset(13.75, 12),
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
      Offset(4, 3.5), Offset(12, 3.5), Offset(12, 8.5), Offset(8, 13.5), Offset(4, 8.5),
      Offset(4, 3.5),
    ],
    [
      Offset(7, 6.25), Offset(9, 6.25), Offset(9, 8.25), Offset(7, 8.25), Offset(7, 6.25),
    ],
  ]);

  static const iface = TcIconData('iface', [
    [
      Offset(8, 7), Offset(8, 13.5),
    ],
    [
      Offset(5.4, 3.9), Offset(3.9, 5.5), Offset(5.4, 7.1),
    ],
    [
      Offset(10.6, 3.9), Offset(12.1, 5.5), Offset(10.6, 7.1),
    ],
    [
      Offset(5.5, 13.5), Offset(10.5, 13.5),
    ],
  ], fills: [
    [
      Offset(8, 4.4), Offset(9.1, 5.5), Offset(8, 6.6), Offset(6.9, 5.5),
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

  static const List<TcIconData> all = [
    settings, lock, plus, join, emoji, hash, users, close, search, send, sync, map, iface, menu,
  ];
}

/// Renders a [TcIconData] at [size], stroked in [color]
/// (defaults to [TCColors.textSecondary]).
class TcIcon extends StatelessWidget {
  const TcIcon(this.icon, {super.key, this.size = 16, this.color});

  final TcIconData icon;
  final double size;
  final Color? color;

  @override
  Widget build(BuildContext context) {
    return CustomPaint(
      size: Size.square(size),
      painter: _TcIconPainter(icon: icon, color: color ?? TCColors.textSecondary),
    );
  }
}

class _TcIconPainter extends CustomPainter {
  const _TcIconPainter({required this.icon, required this.color});

  static const double _grid = 16;
  static const double _strokeWidth = 1.5;

  final TcIconData icon;
  final Color color;

  Path _path(List<Offset> points, double scale) {
    final closed = points.first == points.last;
    final upper = closed ? points.length - 1 : points.length;
    final path = Path()..moveTo(points.first.dx * scale, points.first.dy * scale);
    for (int i = 1; i < upper; i++) {
      path.lineTo(points[i].dx * scale, points[i].dy * scale);
    }
    if (closed) path.close();
    return path;
  }

  @override
  void paint(Canvas canvas, Size size) {
    final scale = size.shortestSide / _grid;
    final stroke = Paint()
      ..color = color
      ..style = PaintingStyle.stroke
      ..strokeWidth = _strokeWidth * scale
      ..strokeCap = StrokeCap.butt
      ..strokeJoin = StrokeJoin.miter;
    for (final points in icon.strokes) {
      canvas.drawPath(_path(points, scale), stroke);
    }
    final fill = Paint()..color = color;
    for (final points in icon.fills) {
      canvas.drawPath(_path([...points, points.first], scale), fill);
    }
  }

  @override
  bool shouldRepaint(covariant _TcIconPainter oldDelegate) =>
      oldDelegate.icon != icon || oldDelegate.color != color;
}
