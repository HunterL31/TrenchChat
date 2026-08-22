// Port of components/data-display/SignalMeter.jsx.
import 'package:flutter/material.dart';

import '../api/models/link_quality.dart';
import '../theme/glow.dart';
import '../theme/quality_tiers.dart';
import '../theme/section_theme.dart';

class SignalMeter extends StatelessWidget {
  const SignalMeter({super.key, this.level = LinkQualityLevel.unknown, this.size = 12});

  final LinkQualityLevel level;
  final double size;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    final score = switch (level) {
      LinkQualityLevel.excellent => 4,
      LinkQualityLevel.good => 3,
      LinkQualityLevel.fair => 2,
      LinkQualityLevel.poor => 1,
      LinkQualityLevel.unknown => 0,
    };
    final color = tcQualityColor(score, tc);
    return Row(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.end,
      children: List.generate(4, (i) {
        final lit = i < score;
        final barHeight = size * (0.4 + i * 0.2);
        return Container(
          margin: EdgeInsets.only(right: i < 3 ? 2 : 0),
          width: 3,
          height: barHeight,
          decoration: BoxDecoration(
            color: lit ? color : tc.textDisabled,
            boxShadow: (lit && level == LinkQualityLevel.excellent) ? tcBoxGlowSm(context) : null,
          ),
        );
      }),
    );
  }
}
