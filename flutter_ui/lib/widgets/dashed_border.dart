// Flutter's BoxDecoration has no dashed border; this paints one directly,
// used for the rail's "+" add-server tile (`border: 1px dashed` in the mockup).
import 'package:flutter/material.dart';

class DashedBorder extends StatelessWidget {
  const DashedBorder({
    super.key,
    required this.color,
    required this.child,
    this.dashWidth = 3,
    this.dashGap = 2,
    this.strokeWidth = 1,
    this.borderRadius = BorderRadius.zero,
  });

  final Color color;
  final Widget child;
  final double dashWidth;
  final double dashGap;
  final double strokeWidth;

  /// Rounds the dashed outline, so a themed tile's dashes follow the same
  /// corners its filled neighbours do.
  final BorderRadius borderRadius;

  @override
  Widget build(BuildContext context) {
    return CustomPaint(
      foregroundPainter: _DashedBorderPainter(
        color: color,
        dashWidth: dashWidth,
        dashGap: dashGap,
        strokeWidth: strokeWidth,
        borderRadius: borderRadius,
      ),
      child: child,
    );
  }
}

class _DashedBorderPainter extends CustomPainter {
  _DashedBorderPainter({
    required this.color,
    required this.dashWidth,
    required this.dashGap,
    required this.strokeWidth,
    required this.borderRadius,
  });

  final Color color;
  final double dashWidth;
  final double dashGap;
  final double strokeWidth;
  final BorderRadius borderRadius;

  @override
  void paint(Canvas canvas, Size size) {
    final paint = Paint()
      ..color = color
      ..style = PaintingStyle.stroke
      ..strokeWidth = strokeWidth;
    final rect = Rect.fromLTWH(0, 0, size.width, size.height);
    final path = Path();
    if (borderRadius == BorderRadius.zero) {
      path.addRect(rect);
    } else {
      path.addRRect(borderRadius.toRRect(rect));
    }
    for (final metric in path.computeMetrics()) {
      double distance = 0;
      while (distance < metric.length) {
        final next = distance + dashWidth;
        canvas.drawPath(metric.extractPath(distance, next.clamp(0, metric.length)), paint);
        distance = next + dashGap;
      }
    }
  }

  @override
  bool shouldRepaint(covariant _DashedBorderPainter oldDelegate) =>
      oldDelegate.color != color || oldDelegate.borderRadius != borderRadius;
}
