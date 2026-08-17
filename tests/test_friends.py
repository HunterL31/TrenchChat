"""
Integration tests for FriendsManager against real Storage and PresenceManager
objects (no networking).
"""

import time
from unittest.mock import patch

import pytest

from trenchchat.core.friends import FRIEND_SEEN_WRITE_INTERVAL_SECS, FriendsManager
from trenchchat.core.presence import PRESENCE_TIMEOUT_SECS, PresenceManager
from trenchchat.core.storage import Storage

SELF_HEX = "aa" * 16
PEER_A = "bb" * 16
PEER_B = "cc" * 16


@pytest.fixture
def storage(tmp_path) -> Storage:
    s = Storage(db_path=tmp_path / "friends_test.db")
    yield s
    s.close()


@pytest.fixture
def presence_mgr() -> PresenceManager:
    return PresenceManager(SELF_HEX)


@pytest.fixture
def mgr(storage, presence_mgr) -> FriendsManager:
    m = FriendsManager(storage, SELF_HEX, presence_mgr)
    presence_mgr.add_seen_callback(m.record_seen)
    presence_mgr.add_presence_callback(m.record_presence)
    return m


# ---------------------------------------------------------------------------
# add / get / update / remove round-trip
# ---------------------------------------------------------------------------

def test_add_get_update_remove_round_trip(mgr):
    assert mgr.add_friend(PEER_A, "Al", "met at defcon") is True
    assert mgr.is_friend(PEER_A) is True

    friends = mgr.get_friends()
    assert len(friends) == 1
    assert friends[0]["identity_hash"] == PEER_A
    assert friends[0]["nickname"] == "Al"
    assert friends[0]["note"] == "met at defcon"

    assert mgr.update_friend(PEER_A, nickname="Alice") is True
    assert mgr.get_friends()[0]["nickname"] == "Alice"
    assert mgr.get_friends()[0]["note"] == "met at defcon"

    assert mgr.remove_friend(PEER_A) is True
    assert mgr.is_friend(PEER_A) is False
    assert mgr.get_friends() == []


def test_update_nickname_and_note_independently(mgr):
    mgr.add_friend(PEER_A, "Al", "old note")

    mgr.update_friend(PEER_A, nickname="Alice")
    row = mgr.get_friends()[0]
    assert row["nickname"] == "Alice"
    assert row["note"] == "old note"

    mgr.update_friend(PEER_A, note="new note")
    row = mgr.get_friends()[0]
    assert row["nickname"] == "Alice"
    assert row["note"] == "new note"


def test_update_nonexistent_friend_returns_false(mgr):
    assert mgr.update_friend(PEER_A, nickname="x") is False


def test_remove_nonexistent_friend_returns_false(mgr):
    assert mgr.remove_friend(PEER_A) is False


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def test_add_friend_rejects_self(mgr):
    assert mgr.add_friend(SELF_HEX, "Me", "") is False
    assert mgr.get_friends() == []


def test_add_friend_rejects_odd_length_hash(mgr):
    assert mgr.add_friend("a" * 31, "X", "") is False


def test_add_friend_rejects_non_hex_chars(mgr):
    assert mgr.add_friend("g" * 32, "X", "") is False


# ---------------------------------------------------------------------------
# added_at preserved on re-add
# ---------------------------------------------------------------------------

def test_readd_existing_friend_preserves_added_at(mgr):
    mgr.add_friend(PEER_A, "Al", "")
    first_added_at = mgr.get_friends()[0]["added_at"]

    mgr.add_friend(PEER_A, "Alice", "updated note")
    second = mgr.get_friends()[0]
    assert second["added_at"] == first_added_at
    assert second["nickname"] == "Alice"
    assert second["note"] == "updated note"


# ---------------------------------------------------------------------------
# last_seen durability
# ---------------------------------------------------------------------------

def test_last_seen_survives_presence_prune(storage, mgr, presence_mgr):
    """The core regression this design exists for: PresenceManager.prune()
    deletes the in-memory entry, but the throttled DB write must remain."""
    mgr.add_friend(PEER_A, "Al", "")

    now = time.time()
    with patch("trenchchat.core.presence.time") as mock_presence_time, \
         patch("trenchchat.core.friends.time") as mock_friends_time:
        mock_presence_time.time.return_value = now
        mock_friends_time.time.return_value = now
        presence_mgr.record_seen(PEER_A)

        later = now + 10_000
        mock_presence_time.time.return_value = later
        mock_friends_time.time.return_value = later
        presence_mgr.prune()

    assert presence_mgr.last_seen_at(PEER_A) == 0.0

    row = storage.get_friend(PEER_A)
    assert row["last_seen_at"] == now

    friends = mgr.get_friends()
    assert friends[0]["last_seen_at"] == now
    assert friends[0]["is_online"] is False


def test_last_seen_survives_storage_close_reopen(tmp_path):
    db_path = tmp_path / "durability.db"
    storage1 = Storage(db_path=db_path)
    presence1 = PresenceManager(SELF_HEX)
    mgr1 = FriendsManager(storage1, SELF_HEX, presence1)
    presence1.add_seen_callback(mgr1.record_seen)

    mgr1.add_friend(PEER_A, "Al", "")
    presence1.record_seen(PEER_A)
    storage1.close()

    storage2 = Storage(db_path=db_path)
    row = storage2.get_friend(PEER_A)
    assert row["last_seen_at"] > 0
    storage2.close()


# ---------------------------------------------------------------------------
# throttled writes
# ---------------------------------------------------------------------------

def test_traffic_from_non_friend_writes_nothing(mgr, storage):
    mgr.record_seen(PEER_B)
    assert storage.get_friend(PEER_B) is None


def test_repeated_friend_traffic_within_throttle_writes_once(mgr, storage):
    mgr.add_friend(PEER_A, "Al", "")

    now = time.time()
    with patch("trenchchat.core.friends.time") as mock_time:
        mock_time.time.return_value = now
        mgr.record_seen(PEER_A)
        assert storage.get_friend(PEER_A)["last_seen_at"] == now

        # Still inside the throttle window -- no second write.
        mock_time.time.return_value = now + FRIEND_SEEN_WRITE_INTERVAL_SECS - 1
        mgr.record_seen(PEER_A)
        assert storage.get_friend(PEER_A)["last_seen_at"] == now

        # Past the window -- writes again.
        later = now + FRIEND_SEEN_WRITE_INTERVAL_SECS + 1
        mock_time.time.return_value = later
        mgr.record_seen(PEER_A)
        assert storage.get_friend(PEER_A)["last_seen_at"] == later


# ---------------------------------------------------------------------------
# get_friends(): is_online and display_name
# ---------------------------------------------------------------------------

def test_get_friends_reports_is_online(mgr, presence_mgr):
    mgr.add_friend(PEER_A, "Al", "")
    mgr.add_friend(PEER_B, "Bee", "")
    presence_mgr.record_seen(PEER_A)

    by_hash = {f["identity_hash"]: f for f in mgr.get_friends()}
    assert by_hash[PEER_A]["is_online"] is True
    assert by_hash[PEER_B]["is_online"] is False


def test_get_friends_display_name_distinct_from_nickname(storage, mgr):
    storage.upsert_member("chan01", PEER_A, "SelfAssertedName", "member")
    mgr.add_friend(PEER_A, "MyNickname", "")

    row = mgr.get_friends()[0]
    assert row["nickname"] == "MyNickname"
    assert row["display_name"] == "SelfAssertedName"
    assert row["display_name"] != row["nickname"]


def test_get_friends_handles_no_presence_manager(storage):
    mgr = FriendsManager(storage, SELF_HEX, presence_mgr=None)
    mgr.add_friend(PEER_A, "Al", "")
    row = mgr.get_friends()[0]
    assert row["is_online"] is False
    assert row["last_seen_at"] == 0


# ---------------------------------------------------------------------------
# add_friends_callback
# ---------------------------------------------------------------------------

def test_add_friends_callback_not_fired_by_record_seen(mgr, presence_mgr):
    events: list[str] = []
    mgr.add_friends_callback(events.append)

    mgr.add_friend(PEER_A, "Al", "")
    events.clear()

    presence_mgr.record_seen(PEER_A)
    assert events == []


def test_add_friends_callback_fires_on_add_update_remove(mgr):
    events: list[str] = []
    mgr.add_friends_callback(events.append)

    mgr.add_friend(PEER_A, "Al", "")
    mgr.update_friend(PEER_A, nickname="Alice")
    mgr.remove_friend(PEER_A)

    assert events == [PEER_A, PEER_A, PEER_A]


def test_last_seen_does_not_jump_backwards_when_a_friend_goes_offline(mgr, presence_mgr):
    """The throttled write lags the live sighting; presence discards its entry
    on the offline transition, so without a flush the reported value regresses
    by up to FRIEND_SEEN_WRITE_INTERVAL_SECS."""
    mgr.add_friend(PEER_A, "Al", "")

    presence_mgr.record_seen(PEER_A)          # writes through, seeds the throttle
    later = time.time() + FRIEND_SEEN_WRITE_INTERVAL_SECS / 2
    with patch("time.time", return_value=later):
        presence_mgr.record_seen(PEER_A)      # inside the window -- no DB write
        seen_while_online = mgr.get_friends()[0]["last_seen_at"]

    presence_mgr.record_offline(PEER_A)       # graceful-shutdown goodbye

    assert seen_while_online == pytest.approx(later, abs=0.001)
    after = mgr.get_friends()[0]["last_seen_at"]
    assert after == pytest.approx(later, abs=0.001), "last_seen jumped backwards"


def test_offline_flush_survives_a_restart(mgr, storage, presence_mgr):
    """The in-memory map is lost on restart, so the flush must reach the DB."""
    mgr.add_friend(PEER_A, "Al", "")
    presence_mgr.record_seen(PEER_A)
    later = time.time() + FRIEND_SEEN_WRITE_INTERVAL_SECS / 2
    with patch("time.time", return_value=later):
        presence_mgr.record_seen(PEER_A)
    presence_mgr.record_offline(PEER_A)

    assert storage.get_friend(PEER_A)["last_seen_at"] == pytest.approx(later, abs=0.001)

    restarted = FriendsManager(storage, SELF_HEX, PresenceManager(SELF_HEX))
    assert restarted.get_friends()[0]["last_seen_at"] == pytest.approx(later, abs=0.001)


def test_prune_also_flushes_the_last_sighting(mgr, storage, presence_mgr):
    """prune() discards the entry too, so it must flush on the way out."""
    mgr.add_friend(PEER_A, "Al", "")
    presence_mgr.record_seen(PEER_A)
    later = time.time() + FRIEND_SEEN_WRITE_INTERVAL_SECS / 2
    with patch("time.time", return_value=later):
        presence_mgr.record_seen(PEER_A)

    with patch("time.time", return_value=later + PRESENCE_TIMEOUT_SECS + 1):
        presence_mgr.prune()

    assert storage.get_friend(PEER_A)["last_seen_at"] == pytest.approx(later, abs=0.001)


def test_offline_transition_ignores_non_friends(mgr, storage, presence_mgr):
    presence_mgr.record_seen(PEER_B)
    presence_mgr.record_offline(PEER_B)
    assert storage.get_friend(PEER_B) is None
