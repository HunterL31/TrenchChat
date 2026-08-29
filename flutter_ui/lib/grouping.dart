// Grouping/lateness constants: consecutive messages collapse and late
// delivery is flagged by these windows.

/// Seconds within which consecutive messages from the same sender are grouped.
const double groupWindowSecs = 300;

/// Messages received more than this many seconds after their timestamp are "late".
const double lateThresholdSecs = 30.0;
