import 'link_quality.dart';

/// Mirrors trenchchat/core/voice.py's roster link_state values.
enum VoiceLinkState { self, streaming, connecting, unreachable, signalled, unknown }

VoiceLinkState _linkStateFrom(String? raw) => switch (raw) {
      'self' => VoiceLinkState.self,
      'streaming' => VoiceLinkState.streaming,
      'connecting' => VoiceLinkState.connecting,
      'unreachable' => VoiceLinkState.unreachable,
      'signalled' => VoiceLinkState.signalled,
      _ => VoiceLinkState.unknown,
    };

class VoiceParticipant {
  const VoiceParticipant({
    required this.identityHash,
    required this.displayName,
    required this.muted,
    required this.joinedAt,
    required this.linkState,
    required this.speaking,
  });

  final String identityHash;
  final String displayName;
  final bool muted;
  final double joinedAt;
  final VoiceLinkState linkState;
  final bool speaking;

  VoiceParticipant copyWith({bool? speaking, bool? muted}) => VoiceParticipant(
        identityHash: identityHash,
        displayName: displayName,
        muted: muted ?? this.muted,
        joinedAt: joinedAt,
        linkState: linkState,
        speaking: speaking ?? this.speaking,
      );

  factory VoiceParticipant.fromJson(Map<String, dynamic> json) => VoiceParticipant(
        identityHash: json['identity_hash'] as String,
        displayName: json['display_name'] as String? ?? '',
        muted: json['muted'] as bool? ?? false,
        joinedAt: (json['joined_at'] as num? ?? 0).toDouble(),
        linkState: _linkStateFrom(json['link_state'] as String?),
        speaking: json['speaking'] as bool? ?? false,
      );
}

class VoicePeerQuality {
  const VoicePeerQuality({
    required this.received,
    required this.lost,
    required this.late,
    required this.jitterMs,
    required this.lossPct,
  });

  final int received;
  final int lost;
  final int late;
  final double jitterMs;
  final double lossPct;

  factory VoicePeerQuality.fromJson(Map<String, dynamic> json) => VoicePeerQuality(
        received: json['received'] as int? ?? 0,
        lost: json['lost'] as int? ?? 0,
        late: json['late'] as int? ?? 0,
        jitterMs: (json['jitter_ms'] as num? ?? 0).toDouble(),
        lossPct: (json['loss_pct'] as num? ?? 0).toDouble(),
      );
}

class VoiceStatus {
  const VoiceStatus({
    required this.channel,
    required this.muted,
    required this.txPackets,
    required this.rxQuality,
    required this.audioAvailable,
    required this.audioReason,
    this.inputOk = true,
    this.outputOk = true,
    this.inputError = '',
    this.outputError = '',
  });

  /// Channel hash of the live session, or null when not in voice.
  final String? channel;
  final bool muted;
  final int txPackets;
  final Map<String, VoicePeerQuality> rxQuality;
  final bool audioAvailable;
  final String audioReason;

  /// Per-direction device state: the pipeline runs whichever halves
  /// opened, so the mic and the speakers can fail independently.
  final bool inputOk;
  final bool outputOk;
  final String inputError;
  final String outputError;

  static const idle = VoiceStatus(
    channel: null,
    muted: false,
    txPackets: 0,
    rxQuality: {},
    audioAvailable: true,
    audioReason: '',
  );

  factory VoiceStatus.fromJson(Map<String, dynamic> json) {
    final stats = json['stats'] as Map<String, dynamic>? ?? const {};
    final audio = json['audio'] as Map<String, dynamic>? ?? const {};
    final rawQuality = stats['rx_quality'] as Map<String, dynamic>? ?? const {};
    return VoiceStatus(
      channel: json['channel'] as String?,
      muted: json['muted'] as bool? ?? false,
      txPackets: stats['tx_packets'] as int? ?? 0,
      rxQuality: rawQuality.map(
        (peer, q) => MapEntry(peer, VoicePeerQuality.fromJson(q as Map<String, dynamic>)),
      ),
      audioAvailable: audio['available'] as bool? ?? true,
      audioReason: audio['reason'] as String? ?? '',
      inputOk: audio['input_ok'] as bool? ?? true,
      outputOk: audio['output_ok'] as bool? ?? true,
      inputError: audio['input_error'] as String? ?? '',
      outputError: audio['output_error'] as String? ?? '',
    );
  }
}

/// GET /voice/devices, the PortAudio devices the backend can capture from
/// and play to, plus the configured selection (null = system default).
class AudioDevices {
  const AudioDevices({
    required this.available,
    required this.reason,
    required this.input,
    required this.output,
    required this.selectedInput,
    required this.selectedOutput,
  });

  final bool available;
  final String reason;
  final List<String> input;
  final List<String> output;
  final String? selectedInput;
  final String? selectedOutput;

  static const unavailable = AudioDevices(
    available: false,
    reason: '',
    input: [],
    output: [],
    selectedInput: null,
    selectedOutput: null,
  );

  factory AudioDevices.fromJson(Map<String, dynamic> json) {
    final selected = json['selected'] as Map<String, dynamic>? ?? const {};
    return AudioDevices(
      available: json['available'] as bool? ?? false,
      reason: json['reason'] as String? ?? '',
      input: [for (final d in json['input'] as List<dynamic>? ?? []) d as String],
      output: [for (final d in json['output'] as List<dynamic>? ?? []) d as String],
      // The config may hold a legacy integer index; render it as text.
      selectedInput: (selected['input'] as Object?)?.toString(),
      selectedOutput: (selected['output'] as Object?)?.toString(),
    );
  }
}

/// Maps one peer's measured stream quality onto the shared meter tiers.
/// The excellent bound is the backend's own Discord-comparable pass
/// criterion (docs/voice.md: loss <= 2%, jitter <= 30 ms).
LinkQualityLevel voicePeerLevel(VoicePeerQuality q) {
  if (q.lossPct <= 2 && q.jitterMs <= 30) return LinkQualityLevel.excellent;
  if (q.lossPct <= 5 && q.jitterMs <= 60) return LinkQualityLevel.good;
  if (q.lossPct <= 12 && q.jitterMs <= 120) return LinkQualityLevel.fair;
  return LinkQualityLevel.poor;
}

/// Worst peer wins; no measured peers yet (solo room, or everyone still
/// connecting) -> unknown, so the meter reads grey instead of lying green.
LinkQualityLevel voiceOverallLevel(VoiceStatus status) {
  if (status.rxQuality.isEmpty) return LinkQualityLevel.unknown;
  var worst = LinkQualityLevel.excellent;
  for (final q in status.rxQuality.values) {
    final level = voicePeerLevel(q);
    if (level.index > worst.index) worst = level;
  }
  return worst;
}
