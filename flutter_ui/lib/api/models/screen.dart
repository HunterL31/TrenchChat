// Screen share as the client sees it: the monitors this node can share, what
// it shares and watches, and the shares direct participants have told it of.
// Mirrors trenchchat/core/screen/manager.py's status() and the reasons its
// actions answer with.
//
// A share travels direct sessions only. A participant this node holds no
// direct session with is never told of a share and cannot watch one, and the
// roster says so rather than showing a Watch that fails.

/// The two presets the picker offers, mirroring core/screen/encoder.py.
enum ScreenPreset { clearer, smoother }

String screenPresetName(ScreenPreset preset) => switch (preset) {
      ScreenPreset.clearer => 'clearer',
      ScreenPreset.smoother => 'smoother',
    };

ScreenPreset screenPresetFrom(String? raw) => switch (raw) {
      'smoother' => ScreenPreset.smoother,
      _ => ScreenPreset.clearer,
    };

/// One monitor, from GET /screen/sources, with a small picture of it.
class ScreenSource {
  const ScreenSource({
    required this.index,
    required this.width,
    required this.height,
    this.thumbnail,
  });

  final int index;
  final int width;
  final int height;

  /// Base64 JPEG grabbed for the picker, or null when the grab failed.
  final String? thumbnail;

  factory ScreenSource.fromJson(Map<String, dynamic> json) => ScreenSource(
        index: json['index'] as int? ?? 1,
        width: json['width'] as int? ?? 0,
        height: json['height'] as int? ?? 0,
        thumbnail: json['thumbnail'] as String?,
      );
}

/// GET /screen/sources: the monitors, or why there are none, and the choices
/// the picker starts from.
class ScreenSources {
  const ScreenSources({
    required this.available,
    required this.reason,
    required this.monitors,
    required this.selectedMonitor,
    required this.selectedPreset,
    required this.selectedFps,
  });

  final bool available;
  final String reason;
  final List<ScreenSource> monitors;
  final int selectedMonitor;
  final ScreenPreset selectedPreset;
  final int selectedFps;

  static const unavailable = ScreenSources(
    available: false,
    reason: '',
    monitors: [],
    selectedMonitor: 1,
    selectedPreset: ScreenPreset.clearer,
    selectedFps: 15,
  );

  factory ScreenSources.fromJson(Map<String, dynamic> json) {
    final selected = json['selected'] as Map<String, dynamic>? ?? const {};
    return ScreenSources(
      available: json['available'] as bool? ?? false,
      reason: json['reason'] as String? ?? '',
      monitors: [
        for (final m in json['monitors'] as List<dynamic>? ?? [])
          ScreenSource.fromJson(m as Map<String, dynamic>)
      ],
      selectedMonitor: selected['monitor'] as int? ?? 1,
      selectedPreset: screenPresetFrom(selected['preset'] as String?),
      selectedFps: selected['fps'] as int? ?? 15,
    );
  }
}

/// A share another participant told this node of over a direct session.
class HeldShare {
  const HeldShare({
    required this.peer,
    required this.channel,
    required this.displayName,
    required this.width,
    required this.height,
    required this.fps,
    required this.since,
  });

  final String peer;
  final String channel;
  final String displayName;
  final int width;
  final int height;
  final int fps;
  final double since;

  factory HeldShare.fromJson(Map<String, dynamic> json) => HeldShare(
        peer: json['peer'] as String? ?? '',
        channel: json['channel'] as String? ?? '',
        displayName: json['display_name'] as String? ?? '',
        width: json['width'] as int? ?? 0,
        height: json['height'] as int? ?? 0,
        fps: json['fps'] as int? ?? 0,
        since: (json['since'] as num? ?? 0).toDouble(),
      );
}

/// One peer watching this node's share.
class ScreenViewer {
  const ScreenViewer({
    required this.peer,
    required this.displayName,
    required this.since,
    required this.updates,
    required this.bytes,
  });

  final String peer;
  final String displayName;
  final double since;
  final int updates;
  final int bytes;

  factory ScreenViewer.fromJson(Map<String, dynamic> json) => ScreenViewer(
        peer: json['peer'] as String? ?? '',
        displayName: json['display_name'] as String? ?? '',
        since: (json['since'] as num? ?? 0).toDouble(),
        updates: json['updates'] as int? ?? 0,
        bytes: json['bytes'] as int? ?? 0,
      );
}

/// This node's own share.
class ScreenSharing {
  const ScreenSharing({
    required this.channel,
    required this.source,
    required this.preset,
    required this.fps,
    required this.width,
    required this.height,
    required this.since,
    required this.viewers,
  });

  final String channel;
  final String source;
  final ScreenPreset preset;
  final int fps;
  final int width;
  final int height;
  final double since;
  final List<ScreenViewer> viewers;

  factory ScreenSharing.fromJson(Map<String, dynamic> json) => ScreenSharing(
        channel: json['channel'] as String? ?? '',
        source: json['source'] as String? ?? '',
        preset: screenPresetFrom(json['preset'] as String?),
        fps: json['fps'] as int? ?? 0,
        width: json['width'] as int? ?? 0,
        height: json['height'] as int? ?? 0,
        since: (json['since'] as num? ?? 0).toDouble(),
        viewers: [
          for (final v in json['viewers'] as List<dynamic>? ?? [])
            ScreenViewer.fromJson(v as Map<String, dynamic>)
        ],
      );
}

/// The share this node is watching.
class ScreenWatching {
  const ScreenWatching({
    required this.peer,
    required this.channel,
    required this.displayName,
    required this.width,
    required this.height,
    required this.updates,
    required this.bytes,
  });

  final String peer;
  final String channel;
  final String displayName;
  final int width;
  final int height;
  final int updates;
  final int bytes;

  factory ScreenWatching.fromJson(Map<String, dynamic> json) => ScreenWatching(
        peer: json['peer'] as String? ?? '',
        channel: json['channel'] as String? ?? '',
        displayName: json['display_name'] as String? ?? '',
        width: json['width'] as int? ?? 0,
        height: json['height'] as int? ?? 0,
        updates: json['updates'] as int? ?? 0,
        bytes: json['bytes'] as int? ?? 0,
      );
}

/// GET /screen/status.
class ScreenStatus {
  const ScreenStatus({
    required this.available,
    required this.reason,
    required this.sharing,
    required this.watching,
    required this.shares,
  });

  /// Whether this node could share at all: direct connections on and a
  /// screen it can capture. [reason] says why not.
  final bool available;
  final String reason;
  final ScreenSharing? sharing;
  final ScreenWatching? watching;
  final List<HeldShare> shares;

  static const idle = ScreenStatus(
    available: false,
    reason: '',
    sharing: null,
    watching: null,
    shares: [],
  );

  factory ScreenStatus.fromJson(Map<String, dynamic> json) {
    final available = json['available'] as Map<String, dynamic>? ?? const {};
    final sharing = json['sharing'] as Map<String, dynamic>?;
    final watching = json['watching'] as Map<String, dynamic>?;
    return ScreenStatus(
      available: available['ok'] as bool? ?? false,
      reason: available['reason'] as String? ?? '',
      sharing: sharing == null ? null : ScreenSharing.fromJson(sharing),
      watching: watching == null ? null : ScreenWatching.fromJson(watching),
      shares: [
        for (final s in json['shares'] as List<dynamic>? ?? [])
          HeldShare.fromJson(s as Map<String, dynamic>)
      ],
    );
  }
}

/// A refusal reason as a sentence, for the panel and the snackbar.
String screenReasonText(String? reason) => switch (reason) {
      'not_in_voice' => 'Join the voice session first.',
      'no_screen_permission' || 'no_permission' =>
        'You do not have permission to share your screen here.',
      'no_direct' => 'Screen share needs direct connections, which are off.',
      'capture_unavailable' => 'This machine cannot capture its screen.',
      'already_sharing' => 'You are already sharing.',
      'no_share' => 'That participant is not sharing.',
      'no_session' => 'Screen share needs a direct connection to that participant.',
      'full' => 'That share already has as many viewers as it allows.',
      'forbidden' => 'The sharer refused.',
      'session_lost' => 'The direct connection was lost.',
      'stopped' => 'The share ended.',
      'voice_left' => 'The voice session ended.',
      null || '' => '',
      _ => 'Screen share failed: $reason',
    };
