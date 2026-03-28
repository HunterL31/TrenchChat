"""
Integration tests for the offline sync system.

Covers:
  - Missed-delivery hint recording
  - Sync request / sync response (hint-targeted and timestamp fallback)
  - flush_pending
  - Startup sync via request_sync_all
"""

import time

import pytest

from tests.helpers import (
    wait_for,
    wait_for_message,
)
from trenchchat.core.messaging import _compute_message_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_channel_on_peer(peer, ch_hash, channel_name, creator_hash,
                           access_mode="public"):
    """Give a peer knowledge of a channel and subscribe them to it."""
    peer.storage.upsert_channel(ch_hash, channel_name, "", creator_hash,
                                access_mode, time.time())
    peer.storage.subscribe(ch_hash)


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


# ---------------------------------------------------------------------------
# Missed-delivery hints
# ---------------------------------------------------------------------------

class TestMissedDeliveryHints:
    def test_hint_recorded_locally_on_missed_delivery(self, peer_factory):
        """
        When the missed-delivery callback fires (simulating a delivery failure),
        a hint is recorded in the sender's storage.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = alice.channel_mgr.create_channel("hint-test", "", "public")

        ts = time.time()
        msg_id = _insert_message(alice.storage, ch_hash, alice.identity.hash_hex,
                                  "Message Bob will miss", ts)

        alice.sync_mgr._on_missed_delivery_event(
            channel_hash_hex=ch_hash,
            missed_peer_hex=bob.identity.hash_hex,
            msg_id=msg_id,
            subscriber_hashes=[alice.identity.hash_hex, bob.identity.hash_hex,
                                carol.identity.hash_hex],
        )

        assert msg_id in alice.storage.get_missed_message_ids(
            ch_hash, bob.identity.hash_hex
        ), "Alice did not record a missed-delivery hint for Bob"

    def test_hint_broadcast_to_online_peers(self, peer_factory):
        """
        When the missed-delivery callback fires, Alice broadcasts MT_MISSED_DELIVERY
        to Carol (who is online). Carol stores the hint in her missed_deliveries table.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = alice.channel_mgr.create_channel("broadcast-hint", "", "public")
        _seed_channel_on_peer(carol, ch_hash, "broadcast-hint", alice.identity.hash_hex)

        ts = time.time()
        msg_id = _insert_message(alice.storage, ch_hash, alice.identity.hash_hex,
                                  "Carol should store hint for Bob", ts)

        alice.sync_mgr._on_missed_delivery_event(
            channel_hash_hex=ch_hash,
            missed_peer_hex=bob.identity.hash_hex,
            msg_id=msg_id,
            subscriber_hashes=[alice.identity.hash_hex, bob.identity.hash_hex,
                                carol.identity.hash_hex],
        )

        assert wait_for(
            lambda: msg_id in carol.storage.get_missed_message_ids(
                ch_hash, bob.identity.hash_hex),
            timeout=5,
        ), "Carol did not store the missed-delivery hint for Bob"


# ---------------------------------------------------------------------------
# Sync request / response
# ---------------------------------------------------------------------------

class TestSyncRequestResponse:
    def test_sync_response_delivers_missed_messages(self, peer_factory):
        """
        Bob sends a sync request to Carol. Carol has hints for Bob and responds
        with the missed messages. Bob receives them.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = alice.channel_mgr.create_channel("sync-test", "", "public")
        _seed_channel_on_peer(carol, ch_hash, "sync-test", alice.identity.hash_hex)
        _seed_channel_on_peer(bob, ch_hash, "sync-test", alice.identity.hash_hex)

        ts = time.time()
        content = "Missed by Bob"
        msg_id = _insert_message(carol.storage, ch_hash, alice.identity.hash_hex,
                                  content, ts)
        carol.storage.record_missed_delivery(ch_hash, bob.identity.hash_hex, msg_id)

        bob.sync_mgr._send_sync_request(
            carol.identity.hash_hex, ch_hash,
            time.time() - 3600,
        )

        assert wait_for_message(bob.storage, ch_hash, msg_id, timeout=5), \
            "Bob did not receive the missed message via sync response"

    def test_timestamp_fallback_sync(self, peer_factory):
        """
        No hints exist; Bob sends a sync request with an old window_start.
        Carol responds with all messages after that timestamp.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = alice.channel_mgr.create_channel("fallback-sync", "", "public")
        _seed_channel_on_peer(carol, ch_hash, "fallback-sync", alice.identity.hash_hex)
        _seed_channel_on_peer(bob, ch_hash, "fallback-sync", alice.identity.hash_hex)

        window_start = time.time()
        msg_ids = []
        for i in range(3):
            ts = window_start + i + 1
            mid = _insert_message(carol.storage, ch_hash, alice.identity.hash_hex,
                                   f"Message {i}", ts)
            msg_ids.append(mid)

        bob.sync_mgr._send_sync_request(
            carol.identity.hash_hex, ch_hash, window_start
        )

        for mid in msg_ids:
            assert wait_for_message(bob.storage, ch_hash, mid, timeout=5), \
                f"Bob did not receive message {mid[:12]}… via timestamp fallback"

    def test_sync_response_is_idempotent(self, peer_factory):
        """
        Receiving the same sync response twice does not create duplicate messages.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = alice.channel_mgr.create_channel("idem-sync", "", "public")
        _seed_channel_on_peer(carol, ch_hash, "idem-sync", alice.identity.hash_hex)
        _seed_channel_on_peer(bob, ch_hash, "idem-sync", alice.identity.hash_hex)

        window_start = time.time()
        ts = window_start + 1
        msg_id = _insert_message(carol.storage, ch_hash, alice.identity.hash_hex,
                                  "Idempotent message", ts)

        bob.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, window_start)
        assert wait_for_message(bob.storage, ch_hash, msg_id, timeout=5)

        bob.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, window_start)
        time.sleep(0.5)

        msgs = bob.storage.get_messages(ch_hash)
        assert len([m for m in msgs if m["message_id"] == msg_id]) == 1

    def test_hints_cleared_after_sync(self, peer_factory):
        """
        After Bob receives a sync response, the missed-delivery hints for Bob
        are cleared from Carol's storage.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = alice.channel_mgr.create_channel("clear-hints", "", "public")
        _seed_channel_on_peer(carol, ch_hash, "clear-hints", alice.identity.hash_hex)
        _seed_channel_on_peer(bob, ch_hash, "clear-hints", alice.identity.hash_hex)

        ts = time.time()
        msg_id = _insert_message(carol.storage, ch_hash, alice.identity.hash_hex,
                                  "Hint should clear", ts + 1)
        carol.storage.record_missed_delivery(ch_hash, bob.identity.hash_hex, msg_id)

        bob.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, ts)
        assert wait_for_message(bob.storage, ch_hash, msg_id, timeout=5)

        assert wait_for(
            lambda: bob.storage.get_missed_message_ids(ch_hash, bob.identity.hash_hex) == [],
            timeout=5,
        ), "Bob's missed-delivery hints were not cleared after sync"


# ---------------------------------------------------------------------------
# Flush pending
# ---------------------------------------------------------------------------

class TestFlushPending:
    def test_flush_pending_manual(self, peer_factory):
        """
        Manually inject a message into the pending queue and verify that
        flush_pending delivers it.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("flush-manual", "", "public")
        _seed_channel_on_peer(bob, ch_hash, "flush-manual", alice.identity.hash_hex)

        ts = time.time()
        content = "Manually queued"
        msg_id = _compute_message_id(content, alice.identity.hash_hex, ts)
        msg_params = {
            "channel_hash_hex":  ch_hash,
            "content":           content,
            "timestamp":         ts,
            "msg_id":            msg_id,
            "display_name":      alice.identity.display_name,
            "reply_to":          None,
            "last_seen_id":      None,
            "subscriber_hashes": [bob.identity.hash_hex],
        }
        alice.messaging._pending[bob.identity.hash_hex] = [msg_params]

        alice.storage.insert_message(
            channel_hash=ch_hash,
            sender_hash=alice.identity.hash_hex,
            sender_name=alice.identity.display_name,
            content=content,
            timestamp=ts,
            message_id=msg_id,
            reply_to=None,
            last_seen_id=None,
            received_at=ts,
        )

        alice.messaging.flush_pending(bob.identity.hash_hex)

        assert wait_for_message(bob.storage, ch_hash, msg_id, timeout=5), \
            "Bob did not receive the manually flushed pending message"

        assert bob.identity.hash_hex not in alice.messaging._pending

    def test_pending_queue_cleared_after_flush(self, peer_factory):
        """
        After flush_pending succeeds, the peer's entry is removed from _pending.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("flush-clear", "", "public")
        _seed_channel_on_peer(bob, ch_hash, "flush-clear", alice.identity.hash_hex)

        ts = time.time()
        msg_id = _compute_message_id("Clear me", alice.identity.hash_hex, ts)
        alice.messaging._pending[bob.identity.hash_hex] = [{
            "channel_hash_hex":  ch_hash,
            "content":           "Clear me",
            "timestamp":         ts,
            "msg_id":            msg_id,
            "display_name":      "Alice",
            "reply_to":          None,
            "last_seen_id":      None,
            "subscriber_hashes": [bob.identity.hash_hex],
        }]

        alice.messaging.flush_pending(bob.identity.hash_hex)

        assert wait_for(
            lambda: bob.identity.hash_hex not in alice.messaging._pending,
            timeout=5,
        ), "Alice's pending queue was not cleared after flush_pending"

    def test_flush_pending_failed_callback_broadcasts_hint(self, peer_factory):
        """
        Regression: flush_pending must register a failed callback so that if
        the LXMF send fails after the path was resolved, a missed-delivery hint
        is broadcast to other subscribers and the message can be recovered via sync.

        We simulate the failure by intercepting the LXMessage after it is built
        and directly invoking its failed callback, then verify that the hint was
        recorded in Carol's storage (a third peer who was online).
        """
        alice = peer_factory("alice")
        bob   = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = alice.channel_mgr.create_channel("flush-fail-hint", "", "public")
        _seed_channel_on_peer(bob,   ch_hash, "flush-fail-hint", alice.identity.hash_hex)
        _seed_channel_on_peer(carol, ch_hash, "flush-fail-hint", alice.identity.hash_hex)

        ts = time.time()
        content = "Will fail on flush"
        msg_id = _compute_message_id(content, alice.identity.hash_hex, ts)

        # Seed the message in Alice's storage so Carol can serve it later if needed
        alice.storage.insert_message(
            channel_hash=ch_hash,
            sender_hash=alice.identity.hash_hex,
            sender_name="Alice",
            content=content,
            timestamp=ts,
            message_id=msg_id,
            reply_to=None,
            last_seen_id=None,
            received_at=ts,
        )

        # Queue the message as pending for Bob, including subscriber_hashes
        alice.messaging._pending[bob.identity.hash_hex] = [{
            "channel_hash_hex":  ch_hash,
            "content":           content,
            "timestamp":         ts,
            "msg_id":            msg_id,
            "display_name":      "Alice",
            "reply_to":          None,
            "last_seen_id":      None,
            "subscriber_hashes": [bob.identity.hash_hex, carol.identity.hash_hex],
        }]

        # Intercept router.send to capture the LXMessage and trigger its failed callback
        captured = []
        original_send = alice.router.send
        def _intercepting_send(lxm):
            captured.append(lxm)
        alice.router.send = _intercepting_send

        alice.messaging.flush_pending(bob.identity.hash_hex)

        # Restore send so other operations work normally
        alice.router.send = original_send

        assert captured, "flush_pending did not call router.send"
        lxm = captured[0]

        # Trigger the failed callback as LXMF would on delivery failure
        assert hasattr(lxm, "failed_callback") and lxm.failed_callback is not None, \
            "flush_pending did not register a failed callback on the LXMessage"
        lxm.failed_callback(lxm)

        # The missed-delivery hint should now be recorded in Carol's storage
        # (broadcast via _on_missed_delivery_event → _send_raw to Carol)
        assert wait_for(
            lambda: msg_id in carol.storage.get_missed_message_ids(
                ch_hash, bob.identity.hash_hex
            ),
            timeout=5,
        ), "Missed-delivery hint was not broadcast to Carol after flush_pending failure"


# ---------------------------------------------------------------------------
# Startup sync
# ---------------------------------------------------------------------------

class TestStartupSync:
    def test_request_sync_all_on_startup(self, peer_factory):
        """
        SyncManager.request_sync_all() sends sync requests for all subscribed
        channels to known peers. Messages seeded in Carol's storage arrive at Bob.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = alice.channel_mgr.create_channel("startup-sync", "", "public")
        _seed_channel_on_peer(carol, ch_hash, "startup-sync", alice.identity.hash_hex)
        _seed_channel_on_peer(bob, ch_hash, "startup-sync", alice.identity.hash_hex)

        window_start = time.time()
        ts = window_start + 1
        msg_id = _insert_message(carol.storage, ch_hash, alice.identity.hash_hex,
                                  "Startup sync message", ts)

        # Manually add Carol as a known subscriber so sync_mgr can find her
        bob.subscription_mgr._subscribers[ch_hash] = {carol.identity.hash_hex}
        bob.storage.update_last_sync(ch_hash)

        bob.sync_mgr.request_sync_all()

        assert wait_for_message(bob.storage, ch_hash, msg_id, timeout=5), \
            "Bob did not receive message via request_sync_all"


# ---------------------------------------------------------------------------
# Membership tenure — sync filtering
# ---------------------------------------------------------------------------

from trenchchat.core.permissions import PRESET_PRIVATE, ROLE_MEMBER, ROLE_OWNER, SEND_MESSAGE
from tests.helpers import wait_for_member


def _setup_invite_channel(peer_factory):
    """Create alice (owner) and bob (member) on a shared invite-only channel."""
    alice = peer_factory("alice")
    bob = peer_factory("bob")
    perms = dict(PRESET_PRIVATE)
    perms[ROLE_MEMBER] = [SEND_MESSAGE]
    ch_hash = alice.channel_mgr.create_channel("tenure-ch", "", permissions=perms)
    alice.invite_mgr.publish_member_list(ch_hash, add_members=[bob.identity.hash])
    assert wait_for_member(alice.storage, ch_hash, bob.identity.hash_hex)
    bob.storage.upsert_channel(ch_hash, "tenure-ch", "", alice.identity.hash_hex,
                               perms, time.time())
    bob.storage.subscribe(ch_hash)
    bob.storage.upsert_member(ch_hash, bob.identity.hash_hex, "Bob", role=ROLE_MEMBER)
    bob.storage.upsert_member(ch_hash, alice.identity.hash_hex, "Alice", role=ROLE_OWNER)
    bob.storage.set_channel_permissions(ch_hash, perms)
    return alice, bob, ch_hash, perms


class TestTenureSyncFiltering:
    def test_sync_response_rejects_gap_message(self, peer_factory):
        """
        Bob is kicked and sends a message locally during the gap.
        When that message appears in a sync response to Carol, Carol drops it.
        """
        alice, bob, ch_hash, perms = _setup_invite_channel(peer_factory)
        carol = peer_factory("carol")
        carol.storage.upsert_channel(ch_hash, "tenure-ch", "", alice.identity.hash_hex,
                                     perms, time.time())
        carol.storage.subscribe(ch_hash)

        join_ts = time.time() - 300

        # Seed tenure on Carol's side
        carol.storage.open_tenure(ch_hash, alice.identity.hash_hex, join_ts)
        carol.storage.open_tenure(ch_hash, bob.identity.hash_hex, join_ts)
        carol.storage.upsert_member(ch_hash, bob.identity.hash_hex, "Bob", role=ROLE_MEMBER)
        carol.storage.upsert_member(ch_hash, alice.identity.hash_hex, "Alice",
                                    role=ROLE_OWNER)
        carol.storage.set_channel_permissions(ch_hash, perms)

        # Kick Bob on Carol's side to close tenure
        kick_ts = join_ts + 100
        carol.storage.close_tenure(ch_hash, bob.identity.hash_hex, kick_ts)
        carol.storage.remove_member(ch_hash, bob.identity.hash_hex)

        # Seed a gap message from Bob (after kick)
        gap_ts = kick_ts + 50
        gap_content = "Gap message"
        gap_msg_id = _compute_message_id(gap_content, bob.identity.hash_hex, gap_ts)
        carol.storage.insert_message(
            channel_hash=ch_hash,
            sender_hash=bob.identity.hash_hex,
            sender_name="Bob",
            content=gap_content,
            timestamp=gap_ts,
            message_id=gap_msg_id,
            reply_to=None,
            last_seen_id=None,
            received_at=gap_ts,
        )

        # Alice requests sync from Carol — Carol must NOT serve the gap message
        alice.storage.open_tenure(ch_hash, alice.identity.hash_hex, join_ts)
        alice.storage.open_tenure(ch_hash, bob.identity.hash_hex, join_ts)
        alice.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, join_ts)

        time.sleep(0.5)
        assert not alice.storage.message_exists(gap_msg_id), \
            "Carol served a gap message from a kicked member in a sync response"

    def test_sync_response_accepts_pre_kick_message(self, peer_factory):
        """
        A message sent before Bob was kicked (valid tenure) must be served
        and accepted through sync.
        """
        alice, bob, ch_hash, perms = _setup_invite_channel(peer_factory)
        carol = peer_factory("carol")
        carol.storage.upsert_channel(ch_hash, "tenure-ch", "", alice.identity.hash_hex,
                                     perms, time.time())
        carol.storage.subscribe(ch_hash)

        join_ts = time.time() - 300

        carol.storage.open_tenure(ch_hash, alice.identity.hash_hex, join_ts)
        carol.storage.open_tenure(ch_hash, bob.identity.hash_hex, join_ts)
        carol.storage.upsert_member(ch_hash, bob.identity.hash_hex, "Bob", role=ROLE_MEMBER)
        carol.storage.upsert_member(ch_hash, alice.identity.hash_hex, "Alice",
                                    role=ROLE_OWNER)
        carol.storage.set_channel_permissions(ch_hash, perms)

        # Legitimate message before kick
        valid_ts = join_ts + 50
        valid_content = "Before kick"
        valid_msg_id = _compute_message_id(valid_content, bob.identity.hash_hex, valid_ts)
        carol.storage.insert_message(
            channel_hash=ch_hash,
            sender_hash=bob.identity.hash_hex,
            sender_name="Bob",
            content=valid_content,
            timestamp=valid_ts,
            message_id=valid_msg_id,
            reply_to=None,
            last_seen_id=None,
            received_at=valid_ts,
        )

        # Kick Bob on Carol's side
        kick_ts = join_ts + 100
        carol.storage.close_tenure(ch_hash, bob.identity.hash_hex, kick_ts)
        carol.storage.remove_member(ch_hash, bob.identity.hash_hex)

        # Alice requests sync — Carol should serve the pre-kick message
        alice.storage.open_tenure(ch_hash, alice.identity.hash_hex, join_ts)
        alice.storage.open_tenure(ch_hash, bob.identity.hash_hex, join_ts)
        alice.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, join_ts)

        assert wait_for_message(alice.storage, ch_hash, valid_msg_id, timeout=5), \
            "Carol did not serve Bob's pre-kick message in a sync response"

    def test_no_tenure_data_allows_sync_without_filtering(self, peer_factory):
        """
        When no tenure data exists for a channel (e.g. open-join channel or
        legacy data), sync proceeds without filtering — no false rejections.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("no-tenure-sync", "", "public")
        _seed_channel_on_peer(bob, ch_hash, "no-tenure-sync", alice.identity.hash_hex)

        ts = time.time()
        msg_id = _insert_message(alice.storage, ch_hash, alice.identity.hash_hex,
                                  "No tenure check needed", ts + 1)

        # No tenure rows — has_any_tenure returns False, filter is bypassed
        assert not bob.storage.has_any_tenure(ch_hash)

        bob.sync_mgr._send_sync_request(alice.identity.hash_hex, ch_hash, ts)

        assert wait_for_message(bob.storage, ch_hash, msg_id, timeout=5), \
            "Message was incorrectly rejected when no tenure data exists"
