// Mirrors trenchchat/gui/channel_view.py's grouping/lateness constants so
// both UIs collapse consecutive messages and flag late delivery the same way.

/// Seconds within which consecutive messages from the same sender are grouped.
const double groupWindowSecs = 300;

/// Messages received more than this many seconds after their timestamp are "late".
const double lateThresholdSecs = 30.0;
