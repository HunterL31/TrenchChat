// The color each link-quality tier is drawn in, in one place: the header's
// signal meter and the network map both read it, so the two can never
// disagree about what "good" looks like.
//
// Three tiers are named by a token. The fourth -- good -- is a yellow-green
// the design system drew and no token names, so it is carried onto the
// theme by the distance that theme moved the tier beside it (see tint.dart).
import 'package:flutter/widgets.dart';

import 'theme_spec.dart';
import 'tint.dart';
import 'tokens.dart';

/// The good tier exactly as the design system drew it.
final Color stockGoodQuality = HSLColor.fromAHSL(1, 70, 0.85, 0.55).toColor();

/// The color for a 0-4 quality score under [tc]: 4=excellent .. 1=poor,
/// 0=unknown, matching trenchchat/core/link_quality.py's LinkQuality.
Color tcQualityColor(int quality, TCSectionColors tc) => switch (quality) {
      4 => tc.statusOnline,
      3 => tcShiftLike(stockGoodQuality,
          from: TCColors.statusOnline, to: tc.statusOnline),
      2 => tc.statusWarn,
      1 => tc.statusDanger,
      _ => tc.statusOffline,
    };
