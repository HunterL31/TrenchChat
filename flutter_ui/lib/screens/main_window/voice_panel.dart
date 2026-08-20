// Bottom-of-column voice session panel: connection quality, channel name,
// mute toggle and leave button -- shown only while in a voice session.
// Pure props like the other column leaves; main_window.dart owns the state.
import 'package:flutter/material.dart';

import '../../api/models/link_quality.dart';
import '../../theme/section_theme.dart';
import '../../theme/tokens.dart';
import '../../widgets/signal_meter.dart';
import '../../widgets/tc_button.dart';
import '../../widgets/tc_icon.dart';

class VoicePanel extends StatelessWidget {
  const VoicePanel({
    super.key,
    required this.channelName,
    required this.quality,
    required this.muted,
    required this.audioError,
    required this.onToggleMute,
    required this.onLeave,
  });

  final String channelName;
  final LinkQualityLevel quality;
  final bool muted;

  /// The session is up but the backend has no working audio device;
  /// we're in the call listening-only (and effectively silent).
  final bool audioError;
  final VoidCallback? onToggleMute;
  final VoidCallback? onLeave;

  @override
  Widget build(BuildContext context) {
    final tc = SectionTheme.of(context);
    return Container(
      decoration: BoxDecoration(
        color: tc.bgSurfaceRaised,
        border: Border(top: BorderSide(color: tc.borderSubtle)),
      ),
      padding: const EdgeInsets.fromLTRB(14, 8, 10, 8),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              SignalMeter(level: quality, size: 12),
              const SizedBox(width: 8),
              Expanded(
                child: Text(
                  'VOICE · #$channelName',
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                    fontSize: TCType.textCaption,
                    color: TCColors.green100,
                    letterSpacing:
                        TCType.letterSpacingFor(TCType.textCaption, TCType.trackingWide),
                  ),
                ),
              ),
            ],
          ),
          if (audioError)
            Padding(
              padding: const EdgeInsets.only(top: 4),
              child: Text(
                'NO AUDIO DEVICE — LISTENING ONLY',
                style: TextStyle(
                  fontSize: TCType.textMicro,
                  color: tc.statusWarn,
                  letterSpacing:
                      TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWide),
                ),
              ),
            ),
          const SizedBox(height: 6),
          Row(
            children: [
              TcIconButton(
                icon: muted ? TcIcons.micMuted : TcIcons.mic,
                tooltip: muted ? 'Unmute' : 'Mute',
                size: 26,
                onPressed: onToggleMute,
              ),
              const SizedBox(width: 4),
              TcIconButton(
                icon: TcIcons.close,
                tooltip: 'Leave voice',
                size: 26,
                onPressed: onLeave,
              ),
              const Spacer(),
              Text(
                muted ? 'MUTED' : 'LIVE',
                style: TextStyle(
                  fontSize: TCType.textMicro,
                  color: muted ? tc.statusWarn : tc.statusOnline,
                  letterSpacing:
                      TCType.letterSpacingFor(TCType.textMicro, TCType.trackingWide),
                ),
              ),
            ],
          ),
        ],
      ),
    );
  }
}
