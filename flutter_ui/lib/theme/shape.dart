// The shape half of a section's style: how far its corners round, what
// outline its avatars are cut to, and whether its emphasis panels keep the
// angular notch.
//
// Every helper here takes the section [BuildContext] sits in, so a widget
// asks for its shape the same way it asks for its colors. A section whose
// corner radius is zero -- the stock terminal look -- gets back the `stock`
// value the call site passes, which is what keeps an unthemed app rendering
// exactly as it did before shapes existed.
import 'package:flutter/widgets.dart';

import 'notch.dart';
import 'section_theme.dart';
import 'theme_spec.dart';
import 'tokens.dart';

/// How much of the themed radius an avatar-shaped square rounds by when its
/// section asks for rounded rather than square or circular avatars.
const double _roundedAvatarFactor = 0.3;

/// The corner radius the section [context] sits in rounds to, or [stock]
/// when it rounds nothing. [scale] tightens or loosens the radius for an
/// element that should not wear the full one.
double tcRadius(BuildContext context, {double stock = 0, double scale = 1}) {
  final radius = SectionTheme.styleOf(context).cornerRadius;
  if (radius <= 0) return stock;
  return radius * scale;
}

/// [tcRadius] as a [BorderRadius], or null when it comes out zero -- assign
/// it straight to `BoxDecoration.borderRadius`. Null rather than
/// [BorderRadius.zero] on purpose: a decoration with no radius at all takes
/// the painter's rectangle path, which is what keeps an unthemed widget
/// pixel-identical to what it drew before shapes existed.
BorderRadius? tcCorners(BuildContext context, {double stock = 0, double scale = 1}) {
  final radius = tcRadius(context, stock: stock, scale: scale);
  return radius <= 0 ? null : BorderRadius.circular(radius);
}

/// The outline an avatar or server tile of [size] is cut to: a circle, the
/// section's own rounding, or -- when the section asks for neither -- the
/// [stock] radius that square carried before shapes existed, null for none.
BorderRadius? tcAvatarCorners(BuildContext context, double size, {double stock = 0}) {
  final style = SectionTheme.styleOf(context);
  return switch (style.avatarShape) {
    TCSectionStyle.avatarCircle => BorderRadius.circular(size / 2),
    TCSectionStyle.avatarRounded =>
      BorderRadius.circular((size * _roundedAvatarFactor).clamp(0, size / 2)),
    _ => tcCorners(context, stock: stock),
  };
}

/// True when the section [context] sits in rounds its corners at all --
/// for the few places where shape changes the layout rather than just the
/// outline (a channel row that has to inset before it can round).
bool tcIsRounded(BuildContext context) => SectionTheme.styleOf(context).cornerRadius > 0;

/// An emphasis panel -- a modal, a floating menu -- cut the way its section
/// asks: the angular notch, or a plain rectangle rounded by the section's
/// corner radius.
class TcPanel extends StatelessWidget {
  const TcPanel({
    super.key,
    required this.child,
    this.notch = TCSpace.notch,
    this.color,
    this.border,
    this.boxShadow,
  });

  final Widget child;

  /// The notch depth, used only when the section keeps the notched edge.
  final double notch;

  final Color? color;
  final Color? border;
  final List<BoxShadow>? boxShadow;

  @override
  Widget build(BuildContext context) {
    final style = SectionTheme.styleOf(context);
    if (style.panelEdge == TCSectionStyle.panelNotch) {
      return NotchedPanel(
        notch: notch,
        color: color,
        border: border,
        boxShadow: boxShadow,
        child: child,
      );
    }
    final corners = tcCorners(context);
    return Container(
      clipBehavior: corners == null ? Clip.none : Clip.antiAlias,
      decoration: BoxDecoration(
        color: color,
        border: border == null ? null : Border.all(color: border!),
        borderRadius: corners,
        boxShadow: boxShadow,
      ),
      child: child,
    );
  }
}
