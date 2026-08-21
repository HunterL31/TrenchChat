// A visual color picker, built from CustomPaint rather than Material's
// sliders so it reads like the rest of the terminal design language: square
// corners, hairline borders, no ink ripples.
//
// The model is HSV held as four independent numbers, not as a Color. That is
// what makes a grey stay on its hue: a color has no hue left once its
// saturation reaches zero, so recovering h/s/v from the Color on every drag
// would snap the pad back to red the moment the user reached the left edge.
// The Color is derived on demand and never stored.
import 'package:flutter/material.dart';

import '../theme/section_theme.dart';
import '../theme/theme_spec.dart';
import '../theme/tokens.dart';
import 'tc_button.dart';
import 'tc_dialog.dart';

/// The saturation/value pad.
const Key tcPickerPadKey = Key('tc-picker-pad');

/// The hue strip.
const Key tcPickerHueKey = Key('tc-picker-hue');

/// The alpha strip.
const Key tcPickerAlphaKey = Key('tc-picker-alpha');

/// The hex field, which also reads out the pending color.
const Key tcPickerHexKey = Key('tc-picker-hex');

/// The swatch of the color the picker opened on; tapping it goes back.
const Key tcPickerOldKey = Key('tc-picker-old');

const Key tcPickerUseKey = Key('tc-picker-use');
const Key tcPickerCancelKey = Key('tc-picker-cancel');

const double _padWidth = 240;
const double _padHeight = 140;
const double _stripHeight = 16;
const double _checkerSize = 6;

/// Opens the picker on [initial] and resolves to the chosen color, or null
/// when it was cancelled or dismissed.
Future<Color?> showTcColorPicker(
  BuildContext context, {
  required Color initial,
  String? title,
}) {
  // The picker route is a sibling of whatever opened it, so the caller's
  // section palette has to be carried across rather than inherited.
  final spec = SectionTheme.specOf(context);
  final section = SectionTheme.sectionOf(context) ?? TCSection.dialogs;
  final colors = SectionTheme.of(context);
  final style = SectionTheme.styleOf(context);
  return showTcDialog<Color>(
    context: context,
    builder: (context) {
      final content = _TcColorPickerContent(initial: initial, title: title ?? 'Pick color');
      return spec == null
          ? SectionTheme.resolved(
              section: section,
              colors: colors,
              style: style,
              child: content,
            )
          : SectionTheme(spec: spec, section: section, child: content);
    },
  );
}

class _TcColorPickerContent extends StatefulWidget {
  const _TcColorPickerContent({required this.initial, required this.title});

  final Color initial;
  final String title;

  @override
  State<_TcColorPickerContent> createState() => _TcColorPickerContentState();
}

class _TcColorPickerContentState extends State<_TcColorPickerContent> {
  double _hue = 0;
  double _sat = 0;
  double _val = 0;
  double _alpha = 1;
  late final TextEditingController _hex =
      TextEditingController(text: encodeThemeColor(widget.initial));

  @override
  void initState() {
    super.initState();
    _adopt(widget.initial);
  }

  @override
  void dispose() {
    _hex.dispose();
    super.dispose();
  }

  Color get _pending => HSVColor.fromAHSV(_alpha, _hue, _sat, _val).toColor();

  /// Takes h/s/v from [color], keeping the components the color cannot carry:
  /// a grey has no hue, and black has neither hue nor saturation.
  void _adopt(Color color) {
    final hsv = HSVColor.fromColor(color.withValues(alpha: 1.0));
    if (hsv.saturation > 0) _hue = hsv.hue;
    if (hsv.value > 0) _sat = hsv.saturation;
    _val = hsv.value;
    _alpha = color.a;
  }

  /// Rewrites the hex field unless what is typed already means exactly the
  /// pending color, so a drag never fights the caret.
  void _syncHex() {
    if (parseThemeColor(_hex.text) == _pending) return;
    final text = encodeThemeColor(_pending);
    _hex.value = TextEditingValue(
      text: text,
      selection: TextSelection.collapsed(offset: text.length),
    );
  }

  void _onHexChanged(String raw) {
    final parsed = parseThemeColor(raw);
    if (parsed == null || parsed == _pending) return;
    setState(() => _adopt(parsed));
  }

  void _onPad(Offset local) {
    setState(() {
      _sat = (local.dx / _padWidth).clamp(0.0, 1.0);
      _val = 1.0 - (local.dy / _padHeight).clamp(0.0, 1.0);
      _syncHex();
    });
  }

  void _onHue(Offset local) {
    setState(() {
      _hue = (local.dx / _padWidth).clamp(0.0, 1.0) * 360.0;
      _syncHex();
    });
  }

  void _onAlpha(Offset local) {
    setState(() {
      _alpha = (local.dx / _padWidth).clamp(0.0, 1.0);
      _syncHex();
    });
  }

  void _reset() {
    setState(() {
      _adopt(widget.initial);
      _syncHex();
    });
  }

  Widget _swatch(TCSectionColors tc, Color color, {Key? key, VoidCallback? onTap, String? tip}) {
    Widget box = Container(
      width: 34,
      height: 26,
      decoration: BoxDecoration(border: Border.all(color: tc.borderStrong)),
      child: CustomPaint(
        painter: _CheckerPainter(),
        child: Container(color: color),
      ),
    );
    if (onTap == null) return box;
    if (tip != null) box = Tooltip(message: tip, child: box);
    return MouseRegion(
      cursor: SystemMouseCursors.click,
      child: GestureDetector(key: key, onTap: onTap, child: box),
    );
  }

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final pending = _pending;
    return TcDialogShell(
      title: widget.title,
      width: 300,
      actions: [
        TcGhostButton(
          key: tcPickerCancelKey,
          label: 'CANCEL',
          onPressed: () => Navigator.pop(context),
        ),
        TcPrimaryButton(
          key: tcPickerUseKey,
          label: 'USE',
          onPressed: () => Navigator.pop(context, pending),
        ),
      ],
      children: [
        _DragArea(
          key: tcPickerPadKey,
          width: _padWidth,
          height: _padHeight,
          border: tc.borderDefault,
          onPosition: _onPad,
          painter: _SvPadPainter(hue: _hue, sat: _sat, val: _val),
        ),
        const SizedBox(height: 10),
        _DragArea(
          key: tcPickerHueKey,
          width: _padWidth,
          height: _stripHeight,
          border: tc.borderDefault,
          onPosition: _onHue,
          painter: _HuePainter(hue: _hue),
        ),
        const SizedBox(height: 8),
        _DragArea(
          key: tcPickerAlphaKey,
          width: _padWidth,
          height: _stripHeight,
          border: tc.borderDefault,
          onPosition: _onAlpha,
          painter: _AlphaPainter(
            color: HSVColor.fromAHSV(1.0, _hue, _sat, _val).toColor(),
            alpha: _alpha,
          ),
        ),
        const SizedBox(height: 12),
        Row(
          children: [
            _swatch(tc, widget.initial,
                key: tcPickerOldKey, onTap: _reset, tip: 'Back to the original'),
            const SizedBox(width: 4),
            _swatch(tc, pending),
            const SizedBox(width: 8),
            Expanded(
              child: Container(
                decoration: BoxDecoration(
                  color: tc.bgInset,
                  border: Border.all(color: tc.borderDefault),
                ),
                padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 5),
                child: TextField(
                  key: tcPickerHexKey,
                  controller: _hex,
                  onChanged: _onHexChanged,
                  style: TextStyle(fontSize: TCType.textBodySm, color: tc.textPrimary),
                  decoration: const InputDecoration(
                    isDense: true,
                    border: InputBorder.none,
                    contentPadding: EdgeInsets.zero,
                  ),
                ),
              ),
            ),
          ],
        ),
      ],
    );
  }
}

/// A painted strip or pad that reports every touch and drag position in its
/// own coordinates.
class _DragArea extends StatelessWidget {
  const _DragArea({
    super.key,
    required this.width,
    required this.height,
    required this.border,
    required this.painter,
    required this.onPosition,
  });

  final double width;
  final double height;
  final Color border;
  final CustomPainter painter;
  final ValueChanged<Offset> onPosition;

  @override
  Widget build(BuildContext context) {
    return MouseRegion(
      cursor: SystemMouseCursors.click,
      child: GestureDetector(
        behavior: HitTestBehavior.opaque,
        onTapDown: (d) => onPosition(d.localPosition),
        onPanDown: (d) => onPosition(d.localPosition),
        onPanUpdate: (d) => onPosition(d.localPosition),
        child: Container(
          width: width,
          height: height,
          // A foreground border keeps the painted area exactly [width] x
          // [height], so a touch position needs no inset to mean what it
          // points at.
          foregroundDecoration: BoxDecoration(border: Border.all(color: border)),
          child: CustomPaint(painter: painter, size: Size(width, height)),
        ),
      ),
    );
  }
}

/// Dark inner outline, light outer outline -- the pair stays visible on both
/// a white corner and a black one.
void _paintThumb(Canvas canvas, Rect rect) {
  final stroke = Paint()
    ..style = PaintingStyle.stroke
    ..strokeWidth = 1;
  canvas.drawRect(rect.deflate(1), stroke..color = const Color(0xFF000000));
  canvas.drawRect(rect, stroke..color = const Color(0xFFFFFFFF));
}

void _paintChecker(Canvas canvas, Size size) {
  canvas.save();
  canvas.clipRect(Offset.zero & size);
  canvas.drawRect(Offset.zero & size, Paint()..color = const Color(0xFF3A3A3A));
  final light = Paint()..color = const Color(0xFF606060);
  for (double y = 0; y < size.height; y += _checkerSize) {
    for (double x = 0; x < size.width; x += _checkerSize) {
      final odd = ((x / _checkerSize).floor() + (y / _checkerSize).floor()).isEven;
      if (odd) canvas.drawRect(Rect.fromLTWH(x, y, _checkerSize, _checkerSize), light);
    }
  }
  canvas.restore();
}

class _CheckerPainter extends CustomPainter {
  @override
  void paint(Canvas canvas, Size size) => _paintChecker(canvas, size);

  @override
  bool shouldRepaint(covariant _CheckerPainter oldDelegate) => false;
}

class _SvPadPainter extends CustomPainter {
  _SvPadPainter({required this.hue, required this.sat, required this.val});

  final double hue;
  final double sat;
  final double val;

  @override
  void paint(Canvas canvas, Size size) {
    final rect = Offset.zero & size;
    canvas.drawRect(
      rect,
      Paint()
        ..shader = LinearGradient(
          colors: [Colors.white, HSVColor.fromAHSV(1, hue, 1, 1).toColor()],
        ).createShader(rect),
    );
    canvas.drawRect(
      rect,
      Paint()
        ..shader = const LinearGradient(
          begin: Alignment.topCenter,
          end: Alignment.bottomCenter,
          colors: [Colors.transparent, Colors.black],
        ).createShader(rect),
    );
    _paintThumb(
      canvas,
      Rect.fromCenter(
        center: Offset(sat * size.width, (1 - val) * size.height),
        width: 10,
        height: 10,
      ),
    );
  }

  @override
  bool shouldRepaint(covariant _SvPadPainter old) =>
      old.hue != hue || old.sat != sat || old.val != val;
}

class _HuePainter extends CustomPainter {
  _HuePainter({required this.hue});

  final double hue;

  static const List<Color> _stops = [
    Color(0xFFFF0000),
    Color(0xFFFFFF00),
    Color(0xFF00FF00),
    Color(0xFF00FFFF),
    Color(0xFF0000FF),
    Color(0xFFFF00FF),
    Color(0xFFFF0000),
  ];

  @override
  void paint(Canvas canvas, Size size) {
    final rect = Offset.zero & size;
    canvas.drawRect(
      rect,
      Paint()..shader = const LinearGradient(colors: _stops).createShader(rect),
    );
    _paintThumb(
      canvas,
      Rect.fromCenter(
        center: Offset(hue / 360 * size.width, size.height / 2),
        width: 6,
        height: size.height,
      ),
    );
  }

  @override
  bool shouldRepaint(covariant _HuePainter old) => old.hue != hue;
}

class _AlphaPainter extends CustomPainter {
  _AlphaPainter({required this.color, required this.alpha});

  final Color color;
  final double alpha;

  @override
  void paint(Canvas canvas, Size size) {
    final rect = Offset.zero & size;
    _paintChecker(canvas, size);
    canvas.drawRect(
      rect,
      Paint()
        ..shader = LinearGradient(
          colors: [color.withValues(alpha: 0), color],
        ).createShader(rect),
    );
    _paintThumb(
      canvas,
      Rect.fromCenter(
        center: Offset(alpha * size.width, size.height / 2),
        width: 6,
        height: size.height,
      ),
    );
  }

  @override
  bool shouldRepaint(covariant _AlphaPainter old) => old.color != color || old.alpha != alpha;
}
