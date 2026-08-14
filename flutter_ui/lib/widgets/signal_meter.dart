// Port of components/data-display/SignalMeter.jsx.
import 'package:flutter/material.dart';

import '../api/models/link_quality.dart';
import '../theme/effects.dart';
import '../theme/tokens.dart';

class _Level {
  const _Level(this.n, this.color);
  final int n;
  final Color color;
}

class SignalMeter extends StatelessWidget {
  const SignalMeter({super.key, this.level = LinkQualityLevel.unknown, this.size = 12});

  final LinkQualityLevel level;
  final double size;

  @override
  Widget build(BuildContext context) {
    final levels = <LinkQualityLevel, _Level>{
      LinkQualityLevel.excellent: _Level(4, TCColors.green400),
      LinkQualityLevel.good: _Level(3, HSLColor.fromAHSL(1, 70, 0.85, 0.55).toColor()),
      LinkQualityLevel.fair: _Level(2, TCColors.amber400),
      LinkQualityLevel.poor: _Level(1, TCColors.statusDanger),
      LinkQualityLevel.unknown: _Level(0, TCColors.ink500),
    };
    final l = levels[level]!;
    return Row(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.end,
      children: List.generate(4, (i) {
        final lit = i < l.n;
        final barHeight = size * (0.4 + i * 0.2);
        return Container(
          margin: EdgeInsets.only(right: i < 3 ? 2 : 0),
          width: 3,
          height: barHeight,
          decoration: BoxDecoration(
            color: lit ? l.color : TCColors.ink700,
            boxShadow: (lit && level == LinkQualityLevel.excellent) ? TCEffects.glowGreenSm : null,
          ),
        );
      }),
    );
  }
}
