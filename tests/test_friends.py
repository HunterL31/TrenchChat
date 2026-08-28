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


# ---------------------------------------------------------------------------
# the friend-request handshake
#
# These use two fully wired peers, because the handshake is the part of the
# friends list that leaves the machine.
# ---------------------------------------------------------------------------

from tests.helpers import wait_for  # noqa: E402
from trenchchat.core.storage import (  # noqa: E402
    FRIEND_ACCEPTED, FRIEND_PENDING_IN, FRIEND_PENDING_OUT,
)
from trenchchat.core.friends import MAX_PENDING_FRIEND_REQUESTS  # noqa: E402


def state_of(peer, other) -> str | None:
    return peer.storage.get_friend_state(other.identity.hash_hex)


def test_request_and_accept_makes_both_sides_friends(peer_factory):
    a = peer_factory("alice")
    b = peer_factory("bob")

    assert a.friends_mgr.send_friend_request(b.identity.hash_hex, "from the ridge") is True
    assert state_of(a, b) == FRIEND_PENDING_OUT
    assert a.friends_mgr.is_friend(b.identity.hash_hex) is False

    assert wait_for(lambda: state_of(b, a) == FRIEND_PENDING_IN)
    assert b.friends_mgr.get_pending_requests()["incoming"][0]["note"] == "from the ridge"

    assert b.friends_mgr.accept_friend_request(a.identity.hash_hex) is True
    assert b.friends_mgr.is_friend(a.identity.hash_hex) is True
    assert wait_for(lambda: a.friends_mgr.is_friend(b.identity.hash_hex))


def test_crossed_requests_settle_as_a_friendship(peer_factory):
    a = peer_factory("alice")
    b = peer_factory("bob")

    a.friends_mgr.send_friend_request(b.identity.hash_hex)
    b.friends_mgr.send_friend_request(a.identity.hash_hex)

    assert wait_for(lambda: a.friends_mgr.is_friend(b.identity.hash_hex))
    assert wait_for(lambda: b.friends_mgr.is_friend(a.identity.hash_hex))


def test_a_repeat_request_from_an_existing_friend_is_answered_quietly(peer_factory):
    a = peer_factory("alice")
    b = peer_factory("bob")
    b.friends_mgr.add_friend(a.identity.hash_hex)

    prompts = []
    b.friends_mgr.add_request_callback(lambda *args: prompts.append(args))

    # A asked before losing their contacts; B already holds them.
    a.friends_mgr.send_friend_request(b.identity.hash_hex)
    assert wait_for(lambda: a.friends_mgr.is_friend(b.identity.hash_hex))
    assert prompts == []


def test_declining_clears_the_request_on_both_sides(peer_factory):
    a = peer_factory("alice")
    b = peer_factory("bob")

    a.friends_mgr.send_friend_request(b.identity.hash_hex)
    assert wait_for(lambda: state_of(b, a) == FRIEND_PENDING_IN)

    assert b.friends_mgr.decline_friend_request(a.identity.hash_hex) is True
    assert state_of(b, a) is None
    assert wait_for(lambda: state_of(a, b) is None)
    assert a.friends_mgr.is_friend(b.identity.hash_hex) is False


def test_accepting_without_a_request_returns_false(peer_factory):
    a = peer_factory("alice")
    b = peer_factory("bob")
    assert a.friends_mgr.accept_friend_request(b.identity.hash_hex) is False
    assert a.friends_mgr.is_friend(b.identity.hash_hex) is False


def test_cancelling_our_own_request_is_local(peer_factory):
    a = peer_factory("alice")
    b = peer_factory("bob")

    a.friends_mgr.send_friend_request(b.identity.hash_hex)
    assert a.friends_mgr.cancel_friend_request(b.identity.hash_hex) is True
    assert state_of(a, b) is None
    # B was never told; their side is theirs to clear.
    assert wait_for(lambda: state_of(b, a) == FRIEND_PENDING_IN)


def test_adding_a_pending_requester_directly_also_answers_them(peer_factory):
    a = peer_factory("alice")
    b = peer_factory("bob")

    a.friends_mgr.send_friend_request(b.identity.hash_hex)
    assert wait_for(lambda: state_of(b, a) == FRIEND_PENDING_IN)

    assert b.friends_mgr.add_friend(a.identity.hash_hex, "Al") is True
    assert wait_for(lambda: a.friends_mgr.is_friend(b.identity.hash_hex))


def test_requesting_someone_who_asked_us_accepts_theirs(peer_factory):
    a = peer_factory("alice")
    b = peer_factory("bob")

    a.friends_mgr.send_friend_request(b.identity.hash_hex)
    assert wait_for(lambda: state_of(b, a) == FRIEND_PENDING_IN)

    assert b.friends_mgr.send_friend_request(a.identity.hash_hex) is True
    assert b.friends_mgr.is_friend(a.identity.hash_hex) is True
    assert wait_for(lambda: a.friends_mgr.is_friend(b.identity.hash_hex))


def test_pending_requests_are_bounded(mgr, storage):
    for i in range(MAX_PENDING_FRIEND_REQUESTS + 5):
        storage.upsert_friend(f"{i:032x}", "", "", FRIEND_PENDING_IN)
        time.sleep(0.001)
    mgr._evict_oldest_pending()
    assert (storage.count_friends_in_state(FRIEND_PENDING_IN)
            < MAX_PENDING_FRIEND_REQUESTS)


def test_pending_friends_are_not_listed_as_friends(mgr, storage):
    storage.upsert_friend(PEER_A, "", "", FRIEND_PENDING_IN)
    storage.upsert_friend(PEER_B, "", "", FRIEND_PENDING_OUT)

    assert mgr.get_friends() == []
    assert mgr.is_friend(PEER_A) is False

    pending = mgr.get_pending_requests()
    assert [f["identity_hash"] for f in pending["incoming"]] == [PEER_A]
    assert [f["identity_hash"] for f in pending["outgoing"]] == [PEER_B]


def test_existing_friends_survive_the_state_migration(tmp_path):
    """A database written before the handshake existed keeps its friends."""
    db = tmp_path / "legacy.db"
    s1 = Storage(db_path=db)
    s1._conn.execute("DROP TABLE friends")
    s1._conn.execute(
        "CREATE TABLE friends (identity_hash TEXT PRIMARY KEY, nickname TEXT NOT NULL "
        "DEFAULT '', note TEXT NOT NULL DEFAULT '', added_at REAL NOT NULL, "
        "last_seen_at REAL NOT NULL DEFAULT 0)"
    )
    s1._conn.execute(
        "INSERT INTO friends (identity_hash, nickname, note, added_at) VALUES (?,?,?,?)",
        (PEER_A, "Al", "", time.time()),
    )
    s1._conn.commit()
    s1.close()

    s2 = Storage(db_path=db)
    assert s2.get_friend_state(PEER_A) == FRIEND_ACCEPTED
    assert FriendsManager(s2, SELF_HEX).is_friend(PEER_A) is True
    s2.close()
