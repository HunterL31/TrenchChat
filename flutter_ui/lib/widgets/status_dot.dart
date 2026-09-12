// Port of components/data-display/StatusDot.jsx.
import 'package:flutter/material.dart';

import '../theme/glow.dart';
import '../theme/section_theme.dart';
import '../theme/theme_spec.dart';

enum PresenceStatus { online, offline, away }

class StatusDot extends StatelessWidget {
  const StatusDot({super.key, required this.status, this.size = 10, this.ringColor});

  final PresenceStatus status;
  final double size;

  /// Colour of the 2 lp ring that cuts the dot out of what it sits on. Pass
  /// the surface behind the dot; the app background is only right on bgApp.
  final Color? ringColor;

  Color _color(TCSectionColors tc) => switch (status) {
        PresenceStatus.online => tc.statusOnline,
        PresenceStatus.offline => tc.statusOffline,
        PresenceStatus.away => tc.statusWarn,
      };

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    return Container(
      width: size,
      height: size,
      decoration: BoxDecoration(
        shape: BoxShape.circle,
        color: _color(tc),
        border: Border.all(color: ringColor ?? tc.bgApp, width: 2),
        boxShadow: status == PresenceStatus.online ? tcBoxGlowSm(context) : null,
      ),
    );
  }
}
