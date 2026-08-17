// 1:1 port of trenchchat-design-system tokens/effects.css.
import 'package:flutter/widgets.dart';

Color _hsla(double h, double s, double l, double a) {
  return HSLColor.fromAHSL(a, h, s, l).toColor();
}

class TCEffects {
  TCEffects._();

  // --glow-green-sm: 0 0 4px hsl(108 70% 45% / .4)
  static List<BoxShadow> get glowGreenSm => [
        BoxShadow(color: _hsla(108, 0.70, 0.45, 0.4), blurRadius: 4),
      ];

  // --glow-green-md: 0 0 12px hsl(108 70% 45% / .38), 0 0 2px hsl(108 60% 58% / .6)
  static List<BoxShadow> get glowGreenMd => [
        BoxShadow(color: _hsla(108, 0.70, 0.45, 0.38), blurRadius: 12),
        BoxShadow(color: _hsla(108, 0.60, 0.58, 0.6), blurRadius: 2),
      ];

  // --glow-green-lg: 0 0 24px hsl(108 70% 45% / .35), 0 0 5px hsl(108 60% 55% / .55)
  static List<BoxShadow> get glowGreenLg => [
        BoxShadow(color: _hsla(108, 0.70, 0.45, 0.35), blurRadius: 24),
        BoxShadow(color: _hsla(108, 0.60, 0.55, 0.55), blurRadius: 5),
      ];

  // --glow-amber-sm: 0 0 4px hsl(28 100% 56% / .55)
  static List<BoxShadow> get glowAmberSm => [
        BoxShadow(color: _hsla(28, 1.0, 0.56, 0.55), blurRadius: 4),
      ];

  // --glow-amber-md: 0 0 14px hsl(28 100% 56% / .5), 0 0 2px hsl(28 100% 70% / .8)
  static List<BoxShadow> get glowAmberMd => [
        BoxShadow(color: _hsla(28, 1.0, 0.56, 0.5), blurRadius: 14),
        BoxShadow(color: _hsla(28, 1.0, 0.70, 0.8), blurRadius: 2),
      ];

  // --glow-red-sm: 0 0 8px hsl(4 80% 56% / .5)
  static List<BoxShadow> get glowRedSm => [
        BoxShadow(color: _hsla(4, 0.80, 0.56, 0.5), blurRadius: 8),
      ];

  // --text-glow-green: 0 0 5px hsl(108 60% 55% / .5)
  static Shadow get textGlowGreen => Shadow(color: _hsla(108, 0.60, 0.55, 0.5), blurRadius: 5);

  // --text-glow-amber: 0 0 6px hsl(28 100% 65% / .7)
  static Shadow get textGlowAmber => Shadow(color: _hsla(28, 1.0, 0.65, 0.7), blurRadius: 6);

  // --shadow-panel: 0 2px 0 hsl(140 15% 2% / .8), 0 8px 24px hsl(140 15% 2% / .5)
  static List<BoxShadow> get shadowPanel => [
        BoxShadow(color: _hsla(140, 0.15, 0.02, 0.8), offset: const Offset(0, 2), blurRadius: 0),
        BoxShadow(color: _hsla(140, 0.15, 0.02, 0.5), offset: const Offset(0, 8), blurRadius: 24),
      ];

  // --shadow-modal: 0 16px 48px hsl(140 15% 2% / .7)
  static List<BoxShadow> get shadowModal => [
        BoxShadow(color: _hsla(140, 0.15, 0.02, 0.7), offset: const Offset(0, 16), blurRadius: 48),
      ];

  // --ease-terminal: cubic-bezier(.2,.9,.25,1)
  static const Curve easeTerminal = Cubic(0.2, 0.9, 0.25, 1.0);

  // --duration-fast/med/slow
  static const Duration durationFast = Duration(milliseconds: 100);
  static const Duration durationMed = Duration(milliseconds: 180);
  static const Duration durationSlow = Duration(milliseconds: 320);
}

/// --scanlines: repeating-linear-gradient(to bottom, transparent 0-2px, black/.18 3px).
/// Off by default per the design readme -- only mount this when explicitly enabled.
class Scanlines extends StatelessWidget {
  const Scanlines({super.key, this.enabled = false, required this.child});

  final bool enabled;
  final Widget child;

  @override
  Widget build(BuildContext context) {
    if (!enabled) return child;
    return Stack(
      children: [
        child,
        Positioned.fill(
          child: IgnorePointer(
            child: CustomPaint(painter: _ScanlinePainter()),
          ),
        ),
      ],
    );
  }
}

class _ScanlinePainter extends CustomPainter {
  @override
  void paint(Canvas canvas, Size size) {
    final paint = Paint()..color = const Color.fromRGBO(0, 0, 0, 0.18);
    for (double y = 2; y < size.height; y += 3) {
      canvas.drawRect(Rect.fromLTWH(0, y, size.width, 1), paint);
    }
  }

  @override
  bool shouldRepaint(covariant CustomPainter oldDelegate) => false;
}
