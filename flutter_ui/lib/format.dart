// Timestamp formatting for message headers and date dividers.

const List<String> _monthAbbrev = [
  'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
];

String _pad2(int n) => n.toString().padLeft(2, '0');

/// Mirrors _format_ts: "%b %d %Y %H:%M", e.g. "Aug 10 2026 21:04".
String formatTs(double unixSeconds) {
  final dt = DateTime.fromMillisecondsSinceEpoch((unixSeconds * 1000).round());
  return '${_monthAbbrev[dt.month - 1]} ${_pad2(dt.day)} ${dt.year} ${_pad2(dt.hour)}:${_pad2(dt.minute)}';
}

/// Mirrors _format_ts_short: "%H:%M".
String formatTsShort(double unixSeconds) {
  final dt = DateTime.fromMillisecondsSinceEpoch((unixSeconds * 1000).round());
  return '${_pad2(dt.hour)}:${_pad2(dt.minute)}';
}

/// Date-divider label as shown in the mockup: "AUG 11 2026".
String formatDateDivider(double unixSeconds) {
  final dt = DateTime.fromMillisecondsSinceEpoch((unixSeconds * 1000).round());
  return '${_monthAbbrev[dt.month - 1].toUpperCase()} ${_pad2(dt.day)} ${dt.year}';
}

/// Relative "last seen" label for the friends panel: "now" under a minute,
/// then minutes/hours/days; "never" for 0 (no last-seen timestamp recorded).
String formatRelative(double unixSeconds) {
  if (unixSeconds <= 0) return 'never';
  final diffSecs = DateTime.now().millisecondsSinceEpoch / 1000 - unixSeconds;
  if (diffSecs < 60) return 'now';
  if (diffSecs < 3600) return '${(diffSecs / 60).floor()}m';
  if (diffSecs < 86400) return '${(diffSecs / 3600).floor()}h';
  return '${(diffSecs / 86400).floor()}d';
}

/// Elapsed time as an age: "12s ago", "4m ago", "2h ago", "3d ago".
/// Empty timestamps (0 or negative) read as "never".
String formatRelativeAgo(double unixSeconds) {
  if (unixSeconds <= 0) return 'never';
  final diff = DateTime.now().millisecondsSinceEpoch / 1000 - unixSeconds;
  if (diff < 0) return 'now';
  return '${_durationLabel(diff)} ago';
}

/// Remaining time as a deadline: "in 4m". Already-passed deadlines read as
/// "expired", so a stale path table is obvious rather than silently negative.
String formatRelativeIn(double unixSeconds) {
  final diff = unixSeconds - DateTime.now().millisecondsSinceEpoch / 1000;
  if (diff <= 0) return 'expired';
  return 'in ${_durationLabel(diff)}';
}

String _durationLabel(double secs) {
  if (secs < 60) return '${secs.floor()}s';
  if (secs < 3600) return '${(secs / 60).floor()}m';
  if (secs < 86400) return '${(secs / 3600).floor()}h';
  return '${(secs / 86400).floor()}d';
}

String formatByteCount(int n) {
  if (n < 1024) return '$n B';
  if (n < 1024 * 1024) return '${(n / 1024).toStringAsFixed(1)} KB';
  return '${(n / (1024 * 1024)).toStringAsFixed(1)} MB';
}

/// Interface bitrate in bits per second, in the units an operator quotes:
/// "9600 bps", "115.2 kbps", "1.0 Mbps".
String formatBitrate(int? bitsPerSecond) {
  if (bitsPerSecond == null || bitsPerSecond <= 0) return '—';
  if (bitsPerSecond < 1000) return '$bitsPerSecond bps';
  if (bitsPerSecond < 1000000) {
    return '${(bitsPerSecond / 1000).toStringAsFixed(1)} kbps';
  }
  return '${(bitsPerSecond / 1000000).toStringAsFixed(1)} Mbps';
}

bool isSameLocalDay(double aUnixSeconds, double bUnixSeconds) {
  final a = DateTime.fromMillisecondsSinceEpoch((aUnixSeconds * 1000).round());
  final b = DateTime.fromMillisecondsSinceEpoch((bUnixSeconds * 1000).round());
  return a.year == b.year && a.month == b.month && a.day == b.day;
}
