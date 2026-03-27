"""
Unit tests for UserDirectory.

These tests do not require a Reticulum instance or network -- UserDirectory
is a pure in-memory module.  Time is mocked via unittest.mock.patch so tests
run deterministically without sleeping.
"""

import time
from unittest.mock import patch

import pytest

from trenchchat.core.user_directory import UserDirectory, DIRECTORY_TTL_SECS


SELF_HEX = "aa" * 16
PEER_A    = "bb" * 16
PEER_B    = "cc" * 16
PEER_C    = "dd" * 16


def make_dir(self_hex: str = SELF_HEX) -> UserDirectory:
    return UserDirectory(self_hex)


# ---------------------------------------------------------------------------
# record_user
# ---------------------------------------------------------------------------

def test_record_user_adds_entry():
    d = make_dir()
    d.record_user(PEER_A, "Alice")
    entries = d.get_all()
    assert any(e["identity_hash"] == PEER_A for e in entries)


def test_record_user_stores_display_name():
    d = make_dir()
    d.record_user(PEER_A, "Alice")
    entries = d.get_all()
    alice = next(e for e in entries if e["identity_hash"] == PEER_A)
    assert alice["display_name"] == "Alice"


def test_record_user_updates_display_name():
    d = make_dir()
    d.record_user(PEER_A, "Alice")
    d.record_user(PEER_A, "Alice Updated")
    entries = d.get_all()
    alice = next(e for e in entries if e["identity_hash"] == PEER_A)
    assert alice["display_name"] == "Alice Updated"


def test_self_is_excluded():
    d = make_dir()
    d.record_user(SELF_HEX, "Me")
    assert d.get_all() == []


def test_multiple_peers_recorded():
    d = make_dir()
    d.record_user(PEER_A, "Alice")
    d.record_user(PEER_B, "Bob")
    hashes = {e["identity_hash"] for e in d.get_all()}
    assert PEER_A in hashes
    assert PEER_B in hashes


# ---------------------------------------------------------------------------
# prune
# ---------------------------------------------------------------------------

def test_prune_removes_expired():
    d = make_dir()
    now = time.time()
    with patch("trenchchat.core.user_directory.time") as mock_time:
        mock_time.time.return_value = now
        d.record_user(PEER_A, "Alice")

        mock_time.time.return_value = now + DIRECTORY_TTL_SECS + 1
        d.prune()

    assert d.get_all() == []


def test_prune_keeps_fresh():
    d = make_dir()
    now = time.time()
    with patch("trenchchat.core.user_directory.time") as mock_time:
        mock_time.time.return_value = now
        d.record_user(PEER_A, "Alice")

        mock_time.time.return_value = now + DIRECTORY_TTL_SECS - 5
        d.prune()

    entries = d.get_all()
    assert any(e["identity_hash"] == PEER_A for e in entries)


def test_prune_removes_only_stale():
    d = make_dir()
    now = time.time()
    with patch("trenchchat.core.user_directory.time") as mock_time:
        mock_time.time.return_value = now
        d.record_user(PEER_A, "Alice")

        # Refresh PEER_B just before pruning
        mock_time.time.return_value = now + DIRECTORY_TTL_SECS - 5
        d.record_user(PEER_B, "Bob")

        mock_time.time.return_value = now + DIRECTORY_TTL_SECS + 1
        d.prune()

    hashes = {e["identity_hash"] for e in d.get_all()}
    assert PEER_A not in hashes
    assert PEER_B in hashes


def test_refreshing_seen_resets_ttl():
    d = make_dir()
    now = time.time()
    with patch("trenchchat.core.user_directory.time") as mock_time:
        mock_time.time.return_value = now
        d.record_user(PEER_A, "Alice")

        # Re-record just before expiry, extending the clock
        mock_time.time.return_value = now + DIRECTORY_TTL_SECS - 5
        d.record_user(PEER_A, "Alice")

        # Advance past original TTL but not past the refreshed one
        mock_time.time.return_value = now + DIRECTORY_TTL_SECS + 10
        d.prune()

    entries = d.get_all()
    assert any(e["identity_hash"] == PEER_A for e in entries)


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

def test_search_by_display_name():
    d = make_dir()
    d.record_user(PEER_A, "Alice")
    d.record_user(PEER_B, "Bob")
    results = d.search("Ali")
    hashes = {e["identity_hash"] for e in results}
    assert PEER_A in hashes
    assert PEER_B not in hashes


def test_search_by_identity_hash():
    d = make_dir()
    d.record_user(PEER_A, "Alice")
    d.record_user(PEER_B, "Bob")
    # PEER_A = "bb" * 16; search for a prefix of its hex
    results = d.search(PEER_A[:8])
    hashes = {e["identity_hash"] for e in results}
    assert PEER_A in hashes
    assert PEER_B not in hashes


def test_search_is_case_insensitive():
    d = make_dir()
    d.record_user(PEER_A, "Alice")
    assert d.search("ALICE")
    assert d.search("alice")
    assert d.search("AlIcE")


def test_empty_query_returns_all():
    d = make_dir()
    d.record_user(PEER_A, "Alice")
    d.record_user(PEER_B, "Bob")
    assert len(d.search("")) == 2


def test_search_excludes_expired():
    d = make_dir()
    now = time.time()
    with patch("trenchchat.core.user_directory.time") as mock_time:
        mock_time.time.return_value = now
        d.record_user(PEER_A, "Alice")

        mock_time.time.return_value = now + DIRECTORY_TTL_SECS + 1
        results = d.search("Alice")

    assert results == []


def test_search_no_match_returns_empty():
    d = make_dir()
    d.record_user(PEER_A, "Alice")
    assert d.search("zzz_no_match") == []


# ---------------------------------------------------------------------------
# get_all
# ---------------------------------------------------------------------------

def test_get_all_returns_all_fresh():
    d = make_dir()
    d.record_user(PEER_A, "Alice")
    d.record_user(PEER_B, "Bob")
    d.record_user(PEER_C, "Charlie")
    assert len(d.get_all()) == 3


def test_get_all_sorted_by_display_name():
    d = make_dir()
    d.record_user(PEER_A, "Zara")
    d.record_user(PEER_B, "Alice")
    d.record_user(PEER_C, "Bob")
    entries = d.get_all()
    names = [e["display_name"] for e in entries]
    assert names == sorted(names, key=str.lower)


def test_get_all_empty_when_no_entries():
    d = make_dir()
    assert d.get_all() == []


# ---------------------------------------------------------------------------
# custom TTL
# ---------------------------------------------------------------------------

def test_custom_ttl_respected():
    d = UserDirectory(SELF_HEX, ttl_secs=10)
    now = time.time()
    with patch("trenchchat.core.user_directory.time") as mock_time:
        mock_time.time.return_value = now
        d.record_user(PEER_A, "Alice")

        mock_time.time.return_value = now + 11
        results = d.get_all()

    assert results == []
