// Mirrors GET /version, backed by trenchchat/version.py: which build is
// running, and what the installer that produced it did to this profile.

/// The step from the profile's last recorded build to this one. Names match
/// trenchchat/version.py's CHANGE_* constants.
enum VersionTransition { firstRun, unknown, same, upgrade, downgrade, sidegrade }

class AppVersionInfo {
  const AppVersionInfo({
    required this.version,
    required this.transition,
    this.previous,
    this.changedAt,
  });

  final String version;
  final VersionTransition transition;

  /// The build that last ran against this profile, absent on a first run.
  final String? previous;

  /// When the version last changed, in epoch seconds.
  final double? changedAt;

  static const unknown =
      AppVersionInfo(version: '', transition: VersionTransition.unknown);

  factory AppVersionInfo.fromJson(Map<String, dynamic> json) {
    final previous = json['previous'];
    final changedAt = json['changed_at'];
    return AppVersionInfo(
      version: json['version'] as String? ?? '',
      transition: _transitionFromName(json['transition'] as String? ?? ''),
      previous: previous is String && previous.isNotEmpty ? previous : null,
      changedAt: changedAt is num ? changedAt.toDouble() : null,
    );
  }

  bool get isKnown => version.isNotEmpty;

  static VersionTransition _transitionFromName(String name) => switch (name) {
        'first_run' => VersionTransition.firstRun,
        'same' => VersionTransition.same,
        'upgrade' => VersionTransition.upgrade,
        'downgrade' => VersionTransition.downgrade,
        'sidegrade' => VersionTransition.sidegrade,
        _ => VersionTransition.unknown,
      };
}
