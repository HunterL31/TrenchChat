// The angular top-right notch cut: clip-path: polygon(0 0, calc(100% - N) 0,
// 100% N, 100% 100%, 0 100%). Reserved for emphasis panels only, per the
// design readme -- not every surface.
import 'package:flutter/widgets.dart';

class NotchClipper extends CustomClipper<Path> {
  const NotchClipper({this.notch = 10});

  final double notch;

  @override
  Path getClip(Size size) {
    final n = notch.clamp(0, size.width).toDouble();
    return Path()
      ..moveTo(0, 0)
      ..lineTo(size.width - n, 0)
      ..lineTo(size.width, n)
      ..lineTo(size.width, size.height)
      ..lineTo(0, size.height)
      ..close();
  }

  @override
  bool shouldReclip(covariant NotchClipper oldClipper) => oldClipper.notch != notch;
}

/// Wraps [child] with the angular notch clip and an optional border drawn
/// along the clipped outline.
class NotchedPanel extends StatelessWidget {
  const NotchedPanel({
    super.key,
    required this.child,
    this.notch = 10,
    this.color,
    this.border,
    this.boxShadow,
  });

  final Widget child;
  final double notch;
  final Color? color;
  final Color? border;
  final List<BoxShadow>? boxShadow;

  @override
  Widget build(BuildContext context) {
    Widget content = ClipPath(
      clipper: NotchClipper(notch: notch),
      child: Container(color: color, child: child),
    );
    if (border != null) {
      content = CustomPaint(
        foregroundPainter: _NotchBorderPainter(notch: notch, color: border!),
        child: content,
      );
    }
    if (boxShadow != null) {
      content = DecoratedBox(
        decoration: BoxDecoration(boxShadow: boxShadow),
        child: content,
      );
    }
    return content;
  }
}

class _NotchBorderPainter extends CustomPainter {
  _NotchBorderPainter({required this.notch, required this.color});

  final double notch;
  final Color color;

  @override
  void paint(Canvas canvas, Size size) {
    final n = notch.clamp(0, size.width).toDouble();
    final path = Path()
      ..moveTo(0, 0)
      ..lineTo(size.width - n, 0)
      ..lineTo(size.width, n)
      ..lineTo(size.width, size.height)
      ..lineTo(0, size.height)
      ..close();
    final paint = Paint()
      ..color = color
      ..style = PaintingStyle.stroke
      ..strokeWidth = 1;
    canvas.drawPath(path, paint);
  }

  @override
  bool shouldRepaint(covariant _NotchBorderPainter oldDelegate) =>
      oldDelegate.notch != notch || oldDelegate.color != color;
}
