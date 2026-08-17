// Timestamp formatting mirroring trenchchat/gui/channel_view.py's
// _format_ts / _format_ts_short so headers read identically to the Qt client.

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

bool isSameLocalDay(double aUnixSeconds, double bUnixSeconds) {
  final a = DateTime.fromMillisecondsSinceEpoch((aUnixSeconds * 1000).round());
  final b = DateTime.fromMillisecondsSinceEpoch((bUnixSeconds * 1000).round());
  return a.year == b.year && a.month == b.month && a.day == b.day;
}
