"""
Tests for per-channel sync status tracking.

Two halves: the tracker's own state derivation against a real Storage, and the
end-to-end states a peer actually passes through when SyncManager drives it.
"""

import time

import pytest

from tests.helpers import wait_for
from trenchchat.core import sync_status
from trenchchat.core.messaging import _compute_message_id
from trenchchat.core.protocol import (
    F_CHANNEL_HASH, F_MISSED_FOR, F_MISSED_MSG_ID, F_MSG_TYPE, MT_MISSED_DELIVERY,
)
from trenchchat.core.storage import Storage
from trenchchat.core.sync_status import PeerSyncState, SyncState, SyncStatusTracker

CHANNEL = "aa" * 16
PEER = "bb" * 16
OTHER_PEER = "cc" * 16


@pytest.fixture
def tracker(tmp_path):
    storage = Storage(db_path=tmp_path / "status.db")
    storage.upsert_channel(CHANNEL, "status", "", PEER, "public", time.time())
    storage.subscribe(CHANNEL)
    yield SyncStatusTracker(storage)
    storage.close()


def _insert_message(storage, ch_hash, sender_hex, content, ts=None):
    """Insert a message directly into storage and return its message_id."""
    ts = ts or time.time()
    msg_id = _compute_message_id(content, sender_hex, ts)
    storage.insert_message(
        channel_hash=ch_hash,
        sender_hash=sender_hex,
        sender_name="Test",
        content=content,
        timestamp=ts,
        message_id=msg_id,
        reply_to=None,
        last_seen_id=None,
        received_at=ts,
    )
    return msg_id


def _seed_channel_on_peer(peer, ch_hash, channel_name, creator_hash,
                          access_mode="public"):
    peer.storage.upsert_channel(ch_hash, channel_name, "", creator_hash,
                                access_mode, time.time())
    peer.storage.subscribe(ch_hash)


# ---------------------------------------------------------------------------
# State derivation
# ---------------------------------------------------------------------------

class TestStateDerivation:
    def test_unknown_before_any_activity(self, tracker):
        assert tracker.get_state(CHANNEL) == SyncState.UNKNOWN

    def test_outstanding_request_is_syncing(self, tracker):
        tracker.request_sent(CHANNEL, PEER)
        assert tracker.get_state(CHANNEL) == SyncState.SYNCING

    def test_answered_request_is_synced(self, tracker):
        tracker.request_sent(CHANNEL, PEER)
        tracker.response_received(CHANNEL, PEER, received=3, inserted=3, truncated=False)
        assert tracker.get_state(CHANNEL) == SyncState.SYNCED

    def test_one_peer_still_pending_keeps_syncing(self, tracker):
        tracker.request_sent(CHANNEL, PEER)
        tracker.request_sent(CHANNEL, OTHER_PEER)
        tracker.response_received(CHANNEL, PEER, received=1, inserted=1, truncated=False)
        assert tracker.get_state(CHANNEL) == SyncState.SYNCING

    def test_unreachable_peer_is_waiting(self, tracker):
        tracker.request_unreachable(CHANNEL, PEER)
        assert tracker.get_state(CHANNEL) == SyncState.WAITING

    def test_channel_with_no_peers_is_waiting(self, tracker):
        tracker.note_no_peers(CHANNEL)
        assert tracker.get_state(CHANNEL) == SyncState.WAITING

    def test_truncated_response_is_incomplete(self, tracker):
        tracker.request_sent(CHANNEL, PEER)
        tracker.response_received(CHANNEL, PEER, received=50, inserted=50, truncated=True)
        assert tracker.get_state(CHANNEL) == SyncState.INCOMPLETE

    def test_continuation_clears_truncation(self, tracker):
        tracker.request_sent(CHANNEL, PEER)
        tracker.response_received(CHANNEL, PEER, received=50, inserted=50, truncated=True)
        tracker.request_sent(CHANNEL, PEER)
        tracker.response_received(CHANNEL, PEER, received=5, inserted=5, truncated=False)
        assert tracker.get_state(CHANNEL) == SyncState.SYNCED

    def test_recheck_of_a_settled_channel_stays_synced(self, tracker):
        """Every peer announce re-asks; a settled channel must not flicker."""
        tracker.request_sent(CHANNEL, PEER)
        tracker.response_received(CHANNEL, PEER, received=0, inserted=0, truncated=False)
        tracker.request_sent(CHANNEL, PEER)
        assert tracker.get_state(CHANNEL) == SyncState.SYNCED
        assert tracker.get_status(CHANNEL)["pending_peers"] == 0

    def test_recheck_that_finds_history_reports_syncing_again(self, tracker):
        tracker.request_sent(CHANNEL, PEER)
        tracker.response_received(CHANNEL, PEER, received=0, inserted=0, truncated=False)
        tracker.request_sent(CHANNEL, PEER)
        tracker.response_received(CHANNEL, PEER, received=50, inserted=50, truncated=True)
        assert tracker.get_state(CHANNEL) == SyncState.INCOMPLETE

    def test_recheck_of_an_unsettled_channel_still_reports_syncing(self, tracker):
        tracker.request_sent(CHANNEL, PEER)
        tracker.response_received(CHANNEL, PEER, received=1, inserted=1, truncated=False)
        tracker.note_gap(CHANNEL)
        tracker.request_sent(CHANNEL, PEER)
        assert tracker.get_state(CHANNEL) == SyncState.SYNCING

    def test_known_gap_is_incomplete(self, tracker):
        tracker.request_sent(CHANNEL, PEER)
        tracker.response_received(CHANNEL, PEER, received=0, inserted=0, truncated=False)
        tracker.note_gap(CHANNEL)
        assert tracker.get_state(CHANNEL) == SyncState.INCOMPLETE

    def test_filled_gap_returns_to_synced(self, tracker):
        tracker.request_sent(CHANNEL, PEER)
        tracker.note_gap(CHANNEL)
        tracker.response_received(CHANNEL, PEER, received=1, inserted=1, truncated=False)
        tracker.clear_gap(CHANNEL)
        assert tracker.get_state(CHANNEL) == SyncState.SYNCED

    def test_silent_peer_is_not_synced(self, tracker, monkeypatch):
        """A peer that never answers must never be reported as up to date."""
        monkeypatch.setattr(sync_status, "PEER_RESPONSE_TIMEOUT_SECS", -1)
        tracker.request_sent(CHANNEL, PEER)
        tracker.prune()
        assert tracker.get_state(CHANNEL) == SyncState.INCOMPLETE

    def test_prune_leaves_answered_peers_alone(self, tracker, monkeypatch):
        monkeypatch.setattr(sync_status, "PEER_RESPONSE_TIMEOUT_SECS", -1)
        tracker.request_sent(CHANNEL, PEER)
        tracker.response_received(CHANNEL, PEER, received=1, inserted=1, truncated=False)
        tracker.prune()
        assert tracker.get_state(CHANNEL) == SyncState.SYNCED


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

class TestStatusReport:
    def test_status_reports_per_peer_detail(self, tracker):
        tracker.request_sent(CHANNEL, PEER)
        tracker.request_sent(CHANNEL, OTHER_PEER)
        tracker.response_received(CHANNEL, PEER, received=4, inserted=2, truncated=False)

        status = tracker.get_status(CHANNEL)
        assert status["state"] == SyncState.SYNCING.value
        assert status["pending_peers"] == 1
        assert status["received_count"] == 2

        by_hash = {p["identity_hash"]: p for p in status["peers"]}
        assert by_hash[PEER]["state"] == PeerSyncState.ANSWERED.value
        assert by_hash[PEER]["messages_received"] == 4
        assert by_hash[OTHER_PEER]["state"] == PeerSyncState.PENDING.value

    def test_status_reports_watermark_from_storage(self, tracker, tmp_path):
        storage = Storage(db_path=tmp_path / "status.db")
        assert tracker.get_status(CHANNEL)["last_synced_at"] == 0.0

        watermark = time.time()
        storage.update_last_sync(CHANNEL, watermark)
        assert tracker.get_status(CHANNEL)["last_synced_at"] == pytest.approx(watermark)
        storage.close()

    def test_unknown_channel_reports_empty_status(self, tracker):
        status = tracker.get_status("ff" * 16)
        assert status["state"] == SyncState.UNKNOWN.value
        assert status["peers"] == []
        assert status["last_synced_at"] == 0.0


class TestStatusCallbacks:
    def test_callback_fires_on_state_change(self, tracker):
        seen = []
        tracker.add_status_callback(seen.append)

        tracker.request_sent(CHANNEL, PEER)
        assert seen == [CHANNEL]

        tracker.response_received(CHANNEL, PEER, received=1, inserted=1, truncated=False)
        assert seen == [CHANNEL, CHANNEL]

    def test_callback_does_not_fire_when_nothing_changed(self, tracker):
        tracker.note_gap(CHANNEL)
        seen = []
        tracker.add_status_callback(seen.append)
        tracker.note_gap(CHANNEL)
        assert seen == []

    def test_callback_does_not_fire_on_a_routine_recheck(self, tracker):
        tracker.request_sent(CHANNEL, PEER)
        tracker.response_received(CHANNEL, PEER, received=0, inserted=0, truncated=False)
        seen = []
        tracker.add_status_callback(seen.append)

        tracker.request_sent(CHANNEL, PEER)
        tracker.response_received(CHANNEL, PEER, received=0, inserted=0, truncated=False)
        assert seen == []

    def test_one_failing_callback_does_not_block_the_others(self, tracker):
        seen = []

        def boom(_channel):
            raise RuntimeError("callback blew up")

        tracker.add_status_callback(boom)
        tracker.add_status_callback(seen.append)
        tracker.request_sent(CHANNEL, PEER)
        assert seen == [CHANNEL]


# ---------------------------------------------------------------------------
# End-to-end, driven by SyncManager
# ---------------------------------------------------------------------------

class TestSyncManagerIntegration:
    def test_caught_up_channel_reaches_synced(self, peer_factory):
        """
        A peer with nothing to send still answers, so the requester can tell
        "caught up" apart from "never answered".
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = alice.channel_mgr.create_channel("status-empty", "", "public")
        _seed_channel_on_peer(carol, ch_hash, "status-empty", alice.identity.hash_hex)
        _seed_channel_on_peer(bob, ch_hash, "status-empty", alice.identity.hash_hex)

        bob.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, time.time())

        assert wait_for(
            lambda: bob.sync_mgr.status.get_state(ch_hash) == SyncState.SYNCED,
            timeout=5,
        ), f"Bob never reached SYNCED, got {bob.sync_mgr.status.get_state(ch_hash)}"

    def test_status_tracks_messages_received(self, peer_factory):
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = alice.channel_mgr.create_channel("status-count", "", "public")
        _seed_channel_on_peer(carol, ch_hash, "status-count", alice.identity.hash_hex)
        _seed_channel_on_peer(bob, ch_hash, "status-count", alice.identity.hash_hex)

        window_start = time.time()
        for i in range(3):
            _insert_message(carol.storage, ch_hash, alice.identity.hash_hex,
                            f"Message {i}", window_start + i + 1)

        bob.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, window_start)

        assert wait_for(
            lambda: bob.sync_mgr.status.get_status(ch_hash)["received_count"] == 3,
            timeout=5,
        ), "sync status did not report the three synced messages"

        status = bob.sync_mgr.status.get_status(ch_hash)
        assert status["state"] == SyncState.SYNCED.value
        assert status["peers"][0]["identity_hash"] == carol.identity.hash_hex

    def test_unreachable_peer_reports_waiting(self, peer_factory):
        """A request that never went out must not leave the channel 'syncing'."""
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("status-waiting", "", "public")
        _seed_channel_on_peer(bob, ch_hash, "status-waiting", alice.identity.hash_hex)

        unknown_peer = "de" * 16
        sent = bob.sync_mgr._send_sync_request(unknown_peer, ch_hash, time.time())

        assert sent is False
        assert bob.sync_mgr.status.get_state(ch_hash) == SyncState.WAITING

    def test_unsent_request_cannot_be_answered(self, peer_factory):
        """
        A request that failed to send leaves nothing outstanding, so a response
        claiming to answer it is unsolicited.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("status-unsent", "", "public")
        _seed_channel_on_peer(bob, ch_hash, "status-unsent", alice.identity.hash_hex)

        unknown_peer = "de" * 16
        bob.sync_mgr._send_sync_request(unknown_peer, ch_hash, time.time())

        assert bob.sync_mgr._claim_pending_request(ch_hash, unknown_peer) is None

    def test_missed_delivery_hint_for_self_marks_incomplete(self, peer_factory):
        """
        A hint naming us is direct evidence of a gap in our own history.

        The originating sender skips the peer that missed the message -- they
        were unreachable, that being the point -- so this arrives from a third
        peer relaying what it was told.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = alice.channel_mgr.create_channel("status-gap", "", "public")
        _seed_channel_on_peer(carol, ch_hash, "status-gap", alice.identity.hash_hex)
        _seed_channel_on_peer(bob, ch_hash, "status-gap", alice.identity.hash_hex)

        msg_id = _insert_message(alice.storage, ch_hash, alice.identity.hash_hex,
                                 "Bob will miss this")
        carol.sync_mgr._send_raw(bob.identity.hash_hex, {
            F_MSG_TYPE:      MT_MISSED_DELIVERY,
            F_CHANNEL_HASH:  bytes.fromhex(ch_hash),
            F_MISSED_FOR:    bob.identity.hash_hex,
            F_MISSED_MSG_ID: msg_id,
        })

        assert wait_for(
            lambda: bob.sync_mgr.status.get_state(ch_hash) == SyncState.INCOMPLETE,
            timeout=5,
        ), "a missed-delivery hint naming Bob did not mark his channel incomplete"
