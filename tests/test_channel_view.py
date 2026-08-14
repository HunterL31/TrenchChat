"""
Tests for the chat view's message-grouping predicate.

_should_group is a pure function so it can be exercised without a
QApplication; the suite instantiates no Qt widgets anywhere.
"""

from trenchchat.gui.channel_view import GROUP_WINDOW_SECS, _should_group

SENDER_A = "aa" * 16
SENDER_B = "bb" * 16


def test_same_sender_same_name_within_window_is_grouped():
    assert _should_group(SENDER_A, "Alice", SENDER_A, "Alice", 100.0, 90.0)


def test_renamed_sender_is_not_grouped():
    """
    Regression test: grouping keyed only on sender hash + time, so a message
    sent after a display-name change folded into the previous group. Grouped
    rows render as MessageContinuation, which carries no name header, so the
    new message displayed under the sender's previous name.
    """
    assert not _should_group(SENDER_A, "Alice2", SENDER_A, "Alice", 100.0, 90.0)


def test_same_sender_outside_window_is_not_grouped():
    assert not _should_group(
        SENDER_A, "Alice", SENDER_A, "Alice",
        100.0 + GROUP_WINDOW_SECS, 100.0,
    )


def test_different_sender_is_not_grouped():
    assert not _should_group(SENDER_A, "Alice", SENDER_B, "Bob", 100.0, 90.0)


def test_first_message_is_not_grouped():
    """load_history() resets the grouping state to None before the first row."""
    assert not _should_group(SENDER_A, "Alice", None, None, 100.0, 0.0)


def test_rename_back_regroups_with_the_matching_name():
    """A→B→A: the third message groups only if the name matches the row before it."""
    assert not _should_group(SENDER_A, "Bob", SENDER_A, "Alice", 110.0, 100.0)
    assert _should_group(SENDER_A, "Bob", SENDER_A, "Bob", 120.0, 110.0)
