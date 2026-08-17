// Port of components/data-display/StatusDot.jsx.
import 'package:flutter/material.dart';

import '../theme/effects.dart';
import '../theme/tokens.dart';

enum PresenceStatus { online, offline, away }

class StatusDot extends StatelessWidget {
  const StatusDot({super.key, required this.status, this.size = 10});

  final PresenceStatus status;
  final double size;

  Color get _color => switch (status) {
        PresenceStatus.online => TCColors.statusOnline,
        PresenceStatus.offline => TCColors.statusOffline,
        PresenceStatus.away => TCColors.statusWarn,
      };

  @override
  Widget build(BuildContext context) {
    return Container(
      width: size,
      height: size,
      decoration: BoxDecoration(
        shape: BoxShape.circle,
        color: _color,
        border: Border.all(color: TCColors.bgApp, width: 2),
        boxShadow: status == PresenceStatus.online ? TCEffects.glowGreenSm : null,
      ),
    );
  }
}
