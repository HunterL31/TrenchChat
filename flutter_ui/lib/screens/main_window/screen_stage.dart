// The stage: the share this node is watching, painted from its frame buffer
// above the message list, with the sharer's name, an expand toggle and a
// stop button. Pure props like the other column leaves; main_window.dart
// owns the state.
import 'dart:ui' as ui;

import 'package:flutter/material.dart';

import '../../api/screen_watch.dart';
import '../../theme/section_theme.dart';
import '../../theme/tokens.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_icon.dart';

class ScreenStage extends StatelessWidget {
  const ScreenStage({
    super.key,
    required this.buffer,
    required this.sharerName,
    required this.expanded,
    required this.onToggleExpanded,
    required this.onStop,
    this.endedReason = '',
  });

  final ScreenFrameBuffer buffer;
  final String sharerName;

  /// Expanded fills the content column; collapsed sits above the messages
  /// at a fixed height.
  final bool expanded;
  final VoidCallback onToggleExpanded;
  final VoidCallback onStop;

  /// Set once the share ended under the viewer; the last picture stays up
  /// with the reason over it until the viewer closes the stage.
  final String endedReason;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final picture = AnimatedBuilder(
      animation: buffer,
      builder: (context, _) => CustomPaint(
        painter: ScreenStagePainter(buffer, background: tc.bgInset),
        child: const SizedBox.expand(),
      ),
    );
    return Container(
      decoration: BoxDecoration(
        color: tc.bgSurfaceRaised,
        border: Border(bottom: BorderSide(color: tc.borderSubtle)),
      ),
      child: Column(
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(14, 6, 10, 6),
            child: Row(
              children: [
                TcIcon(TcIcons.screen, size: 14, color: tc.statusOnline),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    endedReason.isEmpty
                        ? 'WATCHING · $sharerName'
                        : 'SHARE ENDED · $sharerName',
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(
                      fontSize: TCType.textCaption,
                      color: tc.textEmphasis,
                      letterSpacing: TCType.letterSpacingFor(
                          TCType.textCaption, TCType.trackingWide),
                    ),
                  ),
                ),
                TcIconButton(
                  icon: TcIcons.expand,
                  tooltip: expanded ? 'Shrink' : 'Expand',
                  size: 24,
                  onPressed: onToggleExpanded,
                ),
                const SizedBox(width: 2),
                TcIconButton(
                  icon: TcIcons.close,
                  tooltip: 'Stop watching',
                  size: 24,
                  onPressed: onStop,
                ),
              ],
            ),
          ),
          SizedBox(
            height: expanded ? null : 220,
            child: expanded
                ? null
                : Stack(fit: StackFit.expand, children: [
                    picture,
                    if (endedReason.isNotEmpty) _EndedOverlay(reason: endedReason),
                  ]),
          ),
          if (expanded)
            Expanded(
              child: Stack(fit: StackFit.expand, children: [
                picture,
                if (endedReason.isNotEmpty) _EndedOverlay(reason: endedReason),
              ]),
            ),
        ],
      ),
    );
  }
}

class _EndedOverlay extends StatelessWidget {
  const _EndedOverlay({required this.reason});
  final String reason;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    return Center(
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
        decoration: BoxDecoration(
          color: tc.bgSurfaceRaised,
          border: Border.all(color: tc.borderDefault),
        ),
        child: Text(
          reason,
          style: TextStyle(fontSize: TCType.textCaption, color: tc.textSecondary),
        ),
      ),
    );
  }
}

/// Paints the frame buffer to fit, keeping the share's aspect: the full
/// frame first, then every tile that changed after it on top.
class ScreenStagePainter extends CustomPainter {
  ScreenStagePainter(this.buffer, {required this.background})
      : super(repaint: buffer);

  final ScreenFrameBuffer buffer;
  final Color background;

  @override
  void paint(Canvas canvas, Size size) {
    canvas.drawRect(Offset.zero & size, Paint()..color = background);
    if (buffer.isEmpty || buffer.width == 0 || buffer.height == 0) return;
    final scale = _scaleFor(size);
    final drawn = Size(buffer.width * scale, buffer.height * scale);
    final origin = Offset((size.width - drawn.width) / 2, (size.height - drawn.height) / 2);
    final paint = Paint()..filterQuality = FilterQuality.medium;
    canvas.save();
    canvas.translate(origin.dx, origin.dy);
    canvas.scale(scale);
    final full = buffer.full;
    if (full != null) {
      canvas.drawImage(full, Offset.zero, paint);
    }
    final edge = buffer.tileEdge.toDouble();
    final cols = buffer.cols;
    for (final entry in buffer.tiles.entries) {
      final tx = entry.key % cols;
      final ty = entry.key ~/ cols;
      canvas.drawImage(entry.value, Offset(tx * edge, ty * edge), paint);
    }
    if (buffer.cursorX >= 0 && buffer.cursorY >= 0) {
      _drawCursor(canvas, Offset(buffer.cursorX.toDouble(), buffer.cursorY.toDouble()),
          1 / scale);
    }
    canvas.restore();
  }

  double _scaleFor(Size size) {
    final byWidth = size.width / buffer.width;
    final byHeight = size.height / buffer.height;
    return byWidth < byHeight ? byWidth : byHeight;
  }

  void _drawCursor(Canvas canvas, Offset at, double unit) {
    final path = Path()
      ..moveTo(at.dx, at.dy)
      ..lineTo(at.dx, at.dy + 14 * unit)
      ..lineTo(at.dx + 4 * unit, at.dy + 11 * unit)
      ..lineTo(at.dx + 10 * unit, at.dy + 11 * unit)
      ..close();
    canvas.drawPath(path, Paint()..color = const Color(0xFFFFFFFF));
    canvas.drawPath(
      path,
      Paint()
        ..color = const Color(0xFF000000)
        ..style = PaintingStyle.stroke
        ..strokeWidth = unit,
    );
  }

  @override
  bool shouldRepaint(ScreenStagePainter oldDelegate) =>
      oldDelegate.buffer != buffer || oldDelegate.background != background;

  /// For tests: the box the picture occupies inside [size].
  Rect pictureRect(Size size) {
    if (buffer.width == 0 || buffer.height == 0) return Rect.zero;
    final scale = _scaleFor(size);
    final drawn = Size(buffer.width * scale, buffer.height * scale);
    return Offset((size.width - drawn.width) / 2, (size.height - drawn.height) / 2) & drawn;
  }
}

/// A one-pixel-per-tile snapshot of what the painter would compose, for
/// tests that want to read the picture back without a golden.
Future<ui.Image> composeForTest(ScreenFrameBuffer buffer) async {
  final recorder = ui.PictureRecorder();
  final canvas = Canvas(recorder);
  ScreenStagePainter(buffer, background: const Color(0xFF000000)).paint(
      canvas, Size(buffer.width.toDouble(), buffer.height.toDouble()));
  final picture = recorder.endRecording();
  try {
    return await picture.toImage(buffer.width, buffer.height);
  } finally {
    picture.dispose();
  }
}
