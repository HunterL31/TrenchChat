"""
Unit tests for PresenceManager.

These tests do not require a Reticulum instance or network -- PresenceManager
is a pure in-memory module. Time is mocked via unittest.mock.patch so tests
run deterministically without sleeping.
"""

import time
from unittest.mock import patch

import pytest

from trenchchat.core.presence import PresenceManager, PRESENCE_TIMEOUT_SECS


SELF_HEX = "aa" * 16
PEER_A    = "bb" * 16
PEER_B    = "cc" * 16
PEER_C    = "dd" * 16


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_mgr(self_hex: str = SELF_HEX) -> PresenceManager:
    return PresenceManager(self_hex)


# ---------------------------------------------------------------------------
# record_seen / is_online
# ---------------------------------------------------------------------------

def test_record_seen_marks_peer_online():
    mgr = make_mgr()
    mgr.record_seen(PEER_A)
    assert mgr.is_online(PEER_A)


def test_unknown_peer_is_offline():
    mgr = make_mgr()
    assert not mgr.is_online(PEER_A)


def test_self_always_online():
    mgr = make_mgr()
    assert mgr.is_online(SELF_HEX)


def test_self_record_seen_is_ignored():
    """Calling record_seen with self_hex must not raise and self stays online."""
    mgr = make_mgr()
    mgr.record_seen(SELF_HEX)
    assert mgr.is_online(SELF_HEX)


def test_peer_goes_offline_after_timeout():
    mgr = make_mgr()
    now = time.time()
    with patch("trenchchat.core.presence.time") as mock_time:
        mock_time.time.return_value = now
        mgr.record_seen(PEER_A)
        assert mgr.is_online(PEER_A)

        # Advance past the timeout
        mock_time.time.return_value = now + PRESENCE_TIMEOUT_SECS + 1
        assert not mgr.is_online(PEER_A)


def test_refreshing_seen_keeps_peer_online():
    mgr = make_mgr()
    now = time.time()
    with patch("trenchchat.core.presence.time") as mock_time:
        mock_time.time.return_value = now
        mgr.record_seen(PEER_A)

        # Just before timeout, re-announce
        mock_time.time.return_value = now + PRESENCE_TIMEOUT_SECS - 5
        mgr.record_seen(PEER_A)

        # Advance past original timeout but not past the refreshed one
        mock_time.time.return_value = now + PRESENCE_TIMEOUT_SECS + 10
        assert mgr.is_online(PEER_A)


# ---------------------------------------------------------------------------
# get_online_peers
# ---------------------------------------------------------------------------

def test_get_online_peers_returns_only_online():
    mgr = make_mgr()
    now = time.time()
    with patch("trenchchat.core.presence.time") as mock_time:
        mock_time.time.return_value = now
        mgr.record_seen(PEER_A)
        mgr.record_seen(PEER_B)

        # Advance so PEER_B is stale but PEER_A is not
        mock_time.time.return_value = now + PRESENCE_TIMEOUT_SECS - 5
        mgr.record_seen(PEER_A)

        mock_time.time.return_value = now + PRESENCE_TIMEOUT_SECS + 1
        online = mgr.get_online_peers()

    assert PEER_A in online
    assert PEER_B not in online
    assert SELF_HEX not in online  # self is not in the raw set


# ---------------------------------------------------------------------------
# prune
# ---------------------------------------------------------------------------

def test_prune_removes_stale_entries():
    mgr = make_mgr()
    now = time.time()
    with patch("trenchchat.core.presence.time") as mock_time:
        mock_time.time.return_value = now
        mgr.record_seen(PEER_A)

        mock_time.time.return_value = now + PRESENCE_TIMEOUT_SECS + 1
        mgr.prune()

        assert not mgr.is_online(PEER_A)


def test_prune_fires_callback_for_expired_peers():
    mgr = make_mgr()
    events: list[tuple] = []
    mgr.add_presence_callback(lambda peer, online: events.append((peer, online)))

    now = time.time()
    with patch("trenchchat.core.presence.time") as mock_time:
        mock_time.time.return_value = now
        mgr.record_seen(PEER_A)
        events.clear()  # discard the "came online" event

        mock_time.time.return_value = now + PRESENCE_TIMEOUT_SECS + 1
        mgr.prune()

    assert (PEER_A, False) in events


def test_prune_does_not_fire_for_fresh_peers():
    mgr = make_mgr()
    events: list[tuple] = []
    mgr.add_presence_callback(lambda peer, online: events.append((peer, online)))

    now = time.time()
    with patch("trenchchat.core.presence.time") as mock_time:
        mock_time.time.return_value = now
        mgr.record_seen(PEER_A)
        events.clear()

        # Prune before timeout — PEER_A should stay
        mock_time.time.return_value = now + PRESENCE_TIMEOUT_SECS - 5
        mgr.prune()

    assert not any(peer == PEER_A and not online for peer, online in events)


# ---------------------------------------------------------------------------
# callbacks
# ---------------------------------------------------------------------------

def test_record_seen_fires_callback_for_new_peer():
    mgr = make_mgr()
    events: list[tuple] = []
    mgr.add_presence_callback(lambda peer, online: events.append((peer, online)))

    mgr.record_seen(PEER_A)
    assert (PEER_A, True) in events


def test_record_seen_does_not_fire_callback_for_already_online_peer():
    """A second announce from an already-online peer must not re-fire the callback."""
    mgr = make_mgr()
    events: list[tuple] = []
    mgr.add_presence_callback(lambda peer, online: events.append((peer, online)))

    mgr.record_seen(PEER_A)
    events.clear()
    mgr.record_seen(PEER_A)

    assert events == []


def test_multiple_callbacks_all_fired():
    mgr = make_mgr()
    results_a: list = []
    results_b: list = []
    mgr.add_presence_callback(lambda p, o: results_a.append((p, o)))
    mgr.add_presence_callback(lambda p, o: results_b.append((p, o)))

    mgr.record_seen(PEER_A)
    assert (PEER_A, True) in results_a
    assert (PEER_A, True) in results_b


def test_callback_exception_does_not_propagate():
    """A bad callback must not prevent other callbacks from running."""
    mgr = make_mgr()
    results: list = []
    mgr.add_presence_callback(lambda p, o: (_ for _ in ()).throw(RuntimeError("bad")))
    mgr.add_presence_callback(lambda p, o: results.append((p, o)))

    mgr.record_seen(PEER_A)
    assert (PEER_A, True) in results


# ---------------------------------------------------------------------------
# get_online_for_channel
# ---------------------------------------------------------------------------

class _FakeStorage:
    """Minimal storage stub for get_online_for_channel tests."""

    def __init__(self, channel_hash: str, members: list[dict],
                 is_open: bool = False):
        self._channel_hash = channel_hash
        self._members = members
        self._is_open = is_open

    def get_channel(self, hash: str):
        if hash != self._channel_hash:
            return None
        perm = '{"open_join": true}' if self._is_open else '{"open_join": false}'
        return {"hash": hash, "permissions": perm}

    def get_members(self, hash: str):
        return self._members


class _FakeSubscriptionMgr:
    def __init__(self, subs: set[str]):
        self._subs = subs

    def get_subscribers(self, channel_hash: str) -> set[str]:
        return self._subs


def test_get_online_for_channel_invite_only_shows_all_members():
    """For invite-only channels all members appear, with correct online flag."""
    channel_hash = "ff" * 16
    members = [
        {"identity_hash": PEER_A, "display_name": "Alice"},
        {"identity_hash": PEER_B, "display_name": "Bob"},
    ]
    storage = _FakeStorage(channel_hash, members, is_open=False)
    sub_mgr = _FakeSubscriptionMgr(set())

    mgr = make_mgr()
    mgr.record_seen(PEER_A)

    entries = mgr.get_online_for_channel(channel_hash, storage, sub_mgr)
    by_hash = {e["identity_hash"]: e for e in entries}

    assert PEER_A in by_hash
    assert PEER_B in by_hash
    assert by_hash[PEER_A]["is_online"] is True
    assert by_hash[PEER_B]["is_online"] is False


def test_get_online_for_channel_public_shows_only_online_subscribers():
    """For public channels only online subscribers are shown."""
    channel_hash = "ee" * 16
    storage = _FakeStorage(channel_hash, [], is_open=True)
    sub_mgr = _FakeSubscriptionMgr({PEER_A, PEER_B})

    mgr = make_mgr()
    mgr.record_seen(PEER_A)
    # PEER_B is not online

    entries = mgr.get_online_for_channel(channel_hash, storage, sub_mgr)
    hashes = {e["identity_hash"] for e in entries}

    assert PEER_A in hashes
    assert PEER_B not in hashes


def test_get_online_for_channel_unknown_channel_returns_empty():
    storage = _FakeStorage("00" * 16, [])
    sub_mgr = _FakeSubscriptionMgr(set())
    mgr = make_mgr()
    assert mgr.get_online_for_channel("11" * 16, storage, sub_mgr) == []


def test_get_online_for_channel_results_sorted_online_first():
    """Online members must appear before offline members."""
    channel_hash = "ab" * 16
    members = [
        {"identity_hash": PEER_A, "display_name": "Zara"},   # offline
        {"identity_hash": PEER_B, "display_name": "Alice"},  # online
    ]
    storage = _FakeStorage(channel_hash, members, is_open=False)
    sub_mgr = _FakeSubscriptionMgr(set())

    mgr = make_mgr()
    mgr.record_seen(PEER_B)

    entries = mgr.get_online_for_channel(channel_hash, storage, sub_mgr)
    assert entries[0]["identity_hash"] == PEER_B
    assert entries[1]["identity_hash"] == PEER_A
