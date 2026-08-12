"""
Tests for the passphrase policy.

validate_passphrase is a pure function so it can be exercised without a
QApplication; the suite instantiates no Qt widgets anywhere.
"""

from trenchchat.gui.pin_dialog import (
    _MAX_PASSPHRASE_LEN,
    _MIN_PASSPHRASE_LEN,
    validate_passphrase,
)


def test_rejects_shorter_than_minimum():
    assert validate_passphrase("a" * (_MIN_PASSPHRASE_LEN - 1)) is not None


def test_rejects_a_short_numeric_pin():
    """
    Regression test for the actual defect: a 4-8 digit numeric PIN is 13-27
    bits, recoverable offline in minutes. Setting one must no longer be
    possible.
    """
    assert validate_passphrase("1234") is not None
    assert validate_passphrase("12345678") is not None


def test_accepts_exactly_the_minimum():
    assert validate_passphrase("correct-hors") is None
    assert len("correct-hors") == _MIN_PASSPHRASE_LEN


def test_accepts_a_long_passphrase_with_spaces_and_punctuation():
    assert validate_passphrase("correct horse battery staple!") is None


def test_rejects_a_single_repeated_character():
    assert validate_passphrase("a" * _MIN_PASSPHRASE_LEN) is not None


def test_accepts_non_ascii():
    assert validate_passphrase("正しい馬バッテリーステープル") is None


def test_rejects_over_maximum_length():
    assert validate_passphrase("a1" * _MAX_PASSPHRASE_LEN) is not None


def test_accepts_exactly_the_maximum():
    text = ("abcdefghij" * ((_MAX_PASSPHRASE_LEN // 10) + 1))[:_MAX_PASSPHRASE_LEN]
    assert validate_passphrase(text) is None
