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

  /// True when this build is older than the one that last ran here, so the
  /// profile may already hold data the newer build wrote.
  bool get isDowngrade => transition == VersionTransition.downgrade;

  /// True when this build carries the same version as the last one but is not
  /// the same build of it.
  bool get isSidegrade => transition == VersionTransition.sidegrade;

  /// What the last install did, or null when nothing was replaced.
  String? get installNote => switch (transition) {
        VersionTransition.downgrade =>
          'Downgraded from $previous. This profile may hold data written by '
              'the newer build.',
        VersionTransition.sidegrade =>
          'Replaced a different build of the same version ($previous).',
        VersionTransition.upgrade => 'Updated from $previous.',
        _ => null,
      };

  static VersionTransition _transitionFromName(String name) => switch (name) {
        'first_run' => VersionTransition.firstRun,
        'same' => VersionTransition.same,
        'upgrade' => VersionTransition.upgrade,
        'downgrade' => VersionTransition.downgrade,
        'sidegrade' => VersionTransition.sidegrade,
        _ => VersionTransition.unknown,
      };
}
