"""
Integration tests for the offline sync system.

Covers:
  - Missed-delivery hint recording
  - Sync request / sync response (hint-targeted and timestamp fallback)
  - flush_pending
  - Startup sync via request_sync_all
  - Public (open-join) channels being live-only: no hints, no sync

Hints and sync only serve invite-only channels, so the sync-mechanics tests
run on one, usually built with helpers.mirrored_invite_channel. Public
channels appear only where their own live-only contract, or the pending
retry that still applies to them, is under test.
"""

import hashlib
import time

import pytest

from tests.helpers import (
    mirrored_invite_channel,
    seed_channel_on_peer,
    sign_as,
    wait_for,
    wait_for_member,
    wait_for_message,
)
from trenchchat.core.messaging import _compute_message_id
from trenchchat.core.permissions import (
    FULL_SYNC, PRESET_PRIVATE, ROLE_ADMIN, ROLE_MEMBER, ROLE_OWNER, SEND_MESSAGE,
)
from trenchchat.core.sync import MAX_REACTIONS_PER_MESSAGE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
        author_sig=sign_as(sender_hex, ch_hash, msg_id, ts, content),
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

        ch_hash = mirrored_invite_channel("hint-test", alice, bob, carol)

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

        ch_hash = mirrored_invite_channel("broadcast-hint", alice, bob, carol)

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

        ch_hash = mirrored_invite_channel("sync-test", alice, bob, carol)

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

        ch_hash = mirrored_invite_channel("fallback-sync", alice, bob, carol)

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

    def test_capped_batch_watermark_resumes_from_last_message(self, peer_factory):
        """
        A sync response capped at MAX_RESPONSE_MESSAGES must advance the sync
        watermark to the last message actually received, not wall-clock time
        -- otherwise everything past the cap is permanently skipped by the
        next sync request.
        """
        from trenchchat.core.sync import MAX_RESPONSE_MESSAGES

        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = mirrored_invite_channel("capped-sync", alice, bob, carol)

        window_start = time.time()
        total = MAX_RESPONSE_MESSAGES + 10
        msg_ids = []
        for i in range(total):
            ts = window_start + i + 1
            mid = _insert_message(carol.storage, ch_hash, alice.identity.hash_hex,
                                   f"Message {i}", ts)
            msg_ids.append(mid)

        bob.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, window_start)

        assert wait_for_message(
            bob.storage, ch_hash, msg_ids[MAX_RESPONSE_MESSAGES - 1], timeout=5
        ), "Bob did not receive the capped first batch"

        # Every message here is stamped in the future relative to now, so a
        # wall-clock watermark would sit back at ~window_start and strand
        # everything past the cap -- that is the bug this guards. Pinning the
        # value to the first batch's end instead made it a race: the truncated
        # response chains its own continuation immediately, which legitimately
        # carries the watermark on to the final message before this line runs.
        # Both outcomes are correct, and both are far past wall-clock.
        last_sync_at = bob.storage.get_subscriptions()[0]["last_sync_at"]
        first_batch_end = window_start + MAX_RESPONSE_MESSAGES
        last_message_ts = window_start + total
        assert first_batch_end <= last_sync_at <= last_message_ts, (
            "last_sync_at did not advance to a delivered message's timestamp: "
            f"expected between {first_batch_end} and {last_message_ts}, "
            f"got {last_sync_at}"
        )

        bob.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, last_sync_at)

        for mid in msg_ids[MAX_RESPONSE_MESSAGES:]:
            assert wait_for_message(bob.storage, ch_hash, mid, timeout=5), \
                f"Bob never received message {mid[:12]}… stranded past the cap"

    def test_capped_batch_continues_without_another_trigger(self, peer_factory):
        """
        A truncated response chains its own follow-up request, so a backfill
        larger than one batch completes on its own instead of waiting for an
        unrelated announce to drive the next request.
        """
        from trenchchat.core.sync import MAX_RESPONSE_MESSAGES

        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = mirrored_invite_channel("continue-sync", alice, bob, carol)

        window_start = time.time()
        total = MAX_RESPONSE_MESSAGES + 10
        msg_ids = []
        for i in range(total):
            ts = window_start + i + 1
            msg_ids.append(_insert_message(carol.storage, ch_hash,
                                           alice.identity.hash_hex, f"Message {i}", ts))

        bob.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, window_start)

        assert wait_for(
            lambda: len(bob.storage.get_messages(ch_hash)) == total, timeout=10,
        ), (
            f"only {len(bob.storage.get_messages(ch_hash))} of {total} messages "
            "arrived; the truncated batch did not continue on its own"
        )
        # Let the last leg of the chain land before the fixture closes storage.
        time.sleep(0.3)

    def test_continuation_stops_at_the_budget(self, peer_factory):
        """
        A peer that marks every response truncated can't drive requests
        forever: the chain is capped per (channel, peer).
        """
        from trenchchat.core.protocol import (
            F_CHANNEL_HASH, F_MSG_TYPE, F_SYNC_MESSAGES, F_SYNC_TRUNCATED,
            MT_SYNC_RESPONSE,
        )
        from trenchchat.core.sync import MAX_SYNC_CONTINUATIONS

        import msgpack

        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = mirrored_invite_channel("budget-sync", alice, bob, carol)

        requests = []

        # Count Bob's outbound requests without delivering them. Letting Carol
        # actually answer makes the count racy: each real (empty) response
        # claims the pending entry the next synthetic response needs, cutting
        # the chain short by a varying amount.
        def counting_send_raw(dest_hex, fields):
            requests.append(fields.get(F_MSG_TYPE))
            return True

        bob.sync_mgr._send_raw = counting_send_raw

        # Every response carries a newer message and claims more behind it, so
        # only the budget can stop the chain.
        ts = time.time()
        bob.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, ts)
        for i in range(MAX_SYNC_CONTINUATIONS + 5):
            ts += 10
            content = f"chain {i}"
            msg_id = _compute_message_id(content, alice.identity.hash_hex, ts)
            bob.sync_mgr._handle_sync_response(
                {
                    F_MSG_TYPE:       MT_SYNC_RESPONSE,
                    F_CHANNEL_HASH:   bytes.fromhex(ch_hash),
                    F_SYNC_MESSAGES:  msgpack.packb([{
                        "sender_hash":  alice.identity.hash_hex,
                        "sender_name":  "Alice",
                        "content":      content,
                        "timestamp":    ts,
                        "message_id":   msg_id,
                        "reply_to":     None,
                        "last_seen_id": None,
                        "author_sig":   sign_as(alice.identity.hash_hex, ch_hash,
                                                msg_id, ts, content),
                    }], use_bin_type=True),
                    F_SYNC_TRUNCATED: True,
                },
                ch_hash,
                carol.identity.hash_hex,
            )

        continuations = requests.count("sync_request") - 1
        assert continuations == MAX_SYNC_CONTINUATIONS, (
            f"expected the chain to stop after {MAX_SYNC_CONTINUATIONS} "
            f"continuations, got {continuations}"
        )

    def test_caught_up_peer_still_answers(self, peer_factory):
        """
        A responder with nothing to send answers anyway. Silence would be
        indistinguishable from an unreachable or unwilling peer.
        """
        from trenchchat.core.protocol import F_MSG_TYPE, MT_SYNC_RESPONSE

        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = mirrored_invite_channel("ack-sync", alice, bob, carol)

        responses = []
        original = carol.sync_mgr._send_raw

        def recording_send_raw(dest_hex, fields):
            if fields.get(F_MSG_TYPE) == MT_SYNC_RESPONSE:
                responses.append(fields)
            return original(dest_hex, fields)

        carol.sync_mgr._send_raw = recording_send_raw

        bob.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, time.time())

        assert wait_for(lambda: len(responses) == 1, timeout=5), \
            "Carol never answered a sync request she had nothing to serve"

    def test_unauthorised_request_gets_no_answer(self, peer_factory):
        """
        The empty answer is only for peers entitled to the channel: a
        non-member must not learn anything from the shape of the reply.
        """
        from trenchchat.core.protocol import F_MSG_TYPE, MT_SYNC_RESPONSE

        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = alice.channel_mgr.create_channel("closed-ack", "", "invite")
        seed_channel_on_peer(carol, ch_hash, "closed-ack", alice.identity.hash_hex,
                              access_mode="invite")

        responses = []
        original = carol.sync_mgr._send_raw

        def recording_send_raw(dest_hex, fields):
            if fields.get(F_MSG_TYPE) == MT_SYNC_RESPONSE:
                responses.append(fields)
            return original(dest_hex, fields)

        carol.sync_mgr._send_raw = recording_send_raw

        bob.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, time.time())
        time.sleep(0.5)

        assert responses == [], \
            "Carol answered a sync request from a peer with no claim to the channel"

    def test_sync_response_is_idempotent(self, peer_factory):
        """
        Receiving the same sync response twice does not create duplicate messages.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = mirrored_invite_channel("idem-sync", alice, bob, carol)

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

        ch_hash = mirrored_invite_channel("clear-hints", alice, bob, carol)

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

    def test_unresolvable_hint_does_not_shadow_the_sweep(self, peer_factory):
        """A hint naming a message the responder lacks must not cost Bob the rest.

        Hints reach every reachable subscriber, so most holders of a hint never
        have the message it names. If that empty lookup stood as the whole
        answer, Bob would be starved of all history from Carol until the hint
        aged out -- and the empty response would report the channel as synced.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = mirrored_invite_channel("hint-shadow", alice, bob, carol)

        ts = time.time()
        carol.storage.record_missed_delivery(ch_hash, bob.identity.hash_hex,
                                              "de" * 32)
        msg_id = _insert_message(carol.storage, ch_hash, alice.identity.hash_hex,
                                  "Bob must still get this", ts + 1)

        bob.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, ts)

        assert wait_for_message(bob.storage, ch_hash, msg_id, timeout=5), \
            "an unresolvable hint suppressed the timestamp sweep"

    def test_hint_and_sweep_are_served_together(self, peer_factory):
        """One response carries both the hinted message and newer swept history."""
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = mirrored_invite_channel("hint-plus-sweep", alice, bob, carol)

        ts = time.time()
        hinted_id = _insert_message(carol.storage, ch_hash, alice.identity.hash_hex,
                                     "the one Bob missed", ts + 1)
        carol.storage.record_missed_delivery(ch_hash, bob.identity.hash_hex, hinted_id)
        newer_id = _insert_message(carol.storage, ch_hash, alice.identity.hash_hex,
                                    "and the one after it", ts + 2)

        bob.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, ts)

        assert wait_for_message(bob.storage, ch_hash, hinted_id, timeout=5)
        assert wait_for_message(bob.storage, ch_hash, newer_id, timeout=5), \
            "the hint suppressed newer history Bob also lacked"

    def test_hint_resolves_on_a_busy_channel(self, peer_factory):
        """A hint must resolve however much traffic sits in front of it.

        Looking the id up by paging forward from the window start and filtering
        the page silently loses any hint past the first page, so on a busy
        channel the hint mechanism quietly stops working.
        """
        from trenchchat.core.sync import MAX_RESPONSE_MESSAGES

        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = mirrored_invite_channel("busy-hint", alice, bob, carol)

        ts = time.time()
        for i in range(1, MAX_RESPONSE_MESSAGES + 10):
            _insert_message(carol.storage, ch_hash, alice.identity.hash_hex,
                            f"traffic {i}", ts + i)
        buried_id = _insert_message(carol.storage, ch_hash, alice.identity.hash_hex,
                                     "buried behind a page of traffic",
                                     ts + MAX_RESPONSE_MESSAGES + 10)
        carol.storage.record_missed_delivery(ch_hash, bob.identity.hash_hex, buried_id)

        # Bob asks from past the whole backlog, so only the hint can deliver it.
        bob.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, ts + 100000)

        assert wait_for_message(bob.storage, ch_hash, buried_id, timeout=5), \
            "the hint never resolved past the first page of channel traffic"

    def test_throttled_deep_request_stays_silent_even_with_hints(self, peer_factory):
        """A hint must not become a back door around the deep-sync cooldown.

        Answering a throttled request with only the hinted messages hands the
        requester history out of order: they advance their watermark to the
        newest message in the response, stranding the whole un-served backlog
        behind it.
        """
        from trenchchat.core.protocol import (
            F_CHANNEL_HASH, F_MSG_TYPE, F_SYNC_WINDOW_START,
            MT_SYNC_REQUEST, MT_SYNC_RESPONSE,
        )
        from trenchchat.core.sync import MAX_RESPONSE_MESSAGES

        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = mirrored_invite_channel("hint-throttle", alice, bob, carol)

        ts = time.time()
        for i in range(1, MAX_RESPONSE_MESSAGES + 11):
            _insert_message(carol.storage, ch_hash, alice.identity.hash_hex,
                            f"backlog {i}", ts + i)
        recent_id = _insert_message(carol.storage, ch_hash, alice.identity.hash_hex,
                                     "the one Bob was told he missed", ts + 5000)
        carol.storage.record_missed_delivery(ch_hash, bob.identity.hash_hex, recent_id)

        responses = []
        original = carol.sync_mgr._send_raw

        def capture(dest_hex, fields):
            if fields.get(F_MSG_TYPE) == MT_SYNC_RESPONSE:
                responses.append(fields)
            return True

        carol.sync_mgr._send_raw = capture

        deep_request = {
            F_MSG_TYPE:          MT_SYNC_REQUEST,
            F_CHANNEL_HASH:      bytes.fromhex(ch_hash),
            F_SYNC_WINDOW_START: 0.0,
        }
        carol.sync_mgr._handle_sync_request(deep_request, ch_hash,
                                            bob.identity.hash_hex)
        assert len(responses) == 1, "the first deep request went unanswered"

        carol.sync_mgr._handle_sync_request(deep_request, ch_hash,
                                            bob.identity.hash_hex)
        assert len(responses) == 1, \
            "a hint let a throttled deep request through the cooldown"

    def test_watermark_does_not_regress_on_an_older_hinted_message(self, peer_factory):
        """Accepting a message older than the watermark must not rewind it.

        Otherwise every later sync re-requests history we already hold, and the
        deep-sync cooldown starts throttling the recovery it was meant to pace.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = mirrored_invite_channel("no-rewind", alice, bob, carol)

        ts = time.time()
        old_id = _insert_message(carol.storage, ch_hash, alice.identity.hash_hex,
                                  "older than Bob's watermark", ts - 600)
        carol.storage.record_missed_delivery(ch_hash, bob.identity.hash_hex, old_id)
        bob.storage.update_last_sync(ch_hash, ts)

        bob.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, ts)
        assert wait_for_message(bob.storage, ch_hash, old_id, timeout=5)

        assert bob.storage.get_last_sync(ch_hash) == pytest.approx(ts), \
            "watermark rewound over a hint-served message Bob already had past"


# ---------------------------------------------------------------------------
# Deep (pre-SYNC_WINDOW_SECS) sync rate limiting
# ---------------------------------------------------------------------------

class TestDeepSyncRateLimit:
    def test_deep_sync_beyond_window_is_served(self, peer_factory):
        """
        A sync request reaching further back than SYNC_WINDOW_SECS is still
        answered -- there's no hard wall anymore, just a rate limit that a
        single first-time request never trips.
        """
        from trenchchat.core.sync import SYNC_WINDOW_SECS

        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = mirrored_invite_channel("deep-sync-ch", alice, bob)

        old_ts = time.time() - SYNC_WINDOW_SECS - 3600
        msg_id = _insert_message(alice.storage, ch_hash, alice.identity.hash_hex,
                                 "Ancient message", old_ts)

        bob.sync_mgr._send_sync_request(alice.identity.hash_hex, ch_hash, old_ts - 10)

        assert wait_for_message(bob.storage, ch_hash, msg_id, timeout=5), \
            "A first deep sync request was not served"

    def test_deep_sync_throttled_on_repeat_request(self, peer_factory):
        """
        A second deep-backfill request from the same peer within the
        cooldown is silently dropped, so a flood of requests can't force
        repeated full timestamp sweeps.
        """
        from trenchchat.core.sync import SYNC_WINDOW_SECS

        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = mirrored_invite_channel("deep-sync-throttle-ch", alice, bob)

        old_ts = time.time() - SYNC_WINDOW_SECS - 3600
        first_id = _insert_message(alice.storage, ch_hash, alice.identity.hash_hex,
                                   "First ancient message", old_ts)

        bob.sync_mgr._send_sync_request(alice.identity.hash_hex, ch_hash, old_ts - 10)
        assert wait_for_message(bob.storage, ch_hash, first_id, timeout=5)

        second_id = _insert_message(alice.storage, ch_hash, alice.identity.hash_hex,
                                    "Second ancient message", old_ts + 1)

        bob.sync_mgr._send_sync_request(alice.identity.hash_hex, ch_hash, old_ts - 10)
        time.sleep(0.5)

        assert not bob.storage.message_exists(second_id), \
            "A repeated deep sync request within the cooldown was served anyway"

    def test_recent_request_is_never_throttled(self, peer_factory):
        """
        The deep-sync cooldown only guards requests reaching further back
        than SYNC_WINDOW_SECS -- repeated in-window requests are always
        answered immediately, matching pre-existing behaviour.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = mirrored_invite_channel("recent-sync-ch", alice, bob)

        window_start = time.time()
        first_id = _insert_message(alice.storage, ch_hash, alice.identity.hash_hex,
                                   "Recent message one", window_start + 1)
        bob.sync_mgr._send_sync_request(alice.identity.hash_hex, ch_hash, window_start)
        assert wait_for_message(bob.storage, ch_hash, first_id, timeout=5)

        second_id = _insert_message(alice.storage, ch_hash, alice.identity.hash_hex,
                                    "Recent message two", window_start + 2)
        bob.sync_mgr._send_sync_request(alice.identity.hash_hex, ch_hash, window_start)

        assert wait_for_message(bob.storage, ch_hash, second_id, timeout=5), \
            "A second in-window request was incorrectly throttled"


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
        seed_channel_on_peer(bob, ch_hash, "flush-manual", alice.identity.hash_hex,
                              access_mode="public")

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
            "author_sig":        sign_as(alice.identity.hash_hex, ch_hash,
                                         msg_id, ts, content),
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
        seed_channel_on_peer(bob, ch_hash, "flush-clear", alice.identity.hash_hex,
                              access_mode="public")

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

        ch_hash = mirrored_invite_channel("flush-fail-hint", alice, bob, carol)

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

        ch_hash = mirrored_invite_channel("startup-sync", alice, bob, carol)

        window_start = time.time()
        ts = window_start + 1
        msg_id = _insert_message(carol.storage, ch_hash, alice.identity.hash_hex,
                                  "Startup sync message", ts)

        bob.storage.update_last_sync(ch_hash)

        bob.sync_mgr.request_sync_all()

        assert wait_for_message(bob.storage, ch_hash, msg_id, timeout=5), \
            "Bob did not receive message via request_sync_all"


class TestSyncOnChannelJoin:
    def test_join_triggers_sync_request_to_channel_peers(self, peer_factory):
        """
        Regression test for: SyncManager never requested sync when a
        channel_joined event fired. request_sync_all() only runs once, 3s
        after app startup, over channels already subscribed at that
        moment -- a channel joined later in the session was never covered,
        so a new member never even asked anyone for history.

        This only verifies the *request* goes out on join (the fix in
        SyncManager). It does not assert the requester ends up with the
        channel's pre-join history -- that also requires every message's
        sender to have a locally-known membership_tenure interval covering
        its timestamp, which for a brand-new joiner is only true for
        activity within their own local view. Making a joiner trust an
        existing member's *claimed* earlier history is a separate,
        security-relevant design question (see the linked bug report) --
        the signed member-list document carries no per-member join
        timestamp, so there's no verified source for how far back to
        trust someone without weakening the tenure/replay protections.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("history-on-join", "", "invite")
        alice.invite_mgr.publish_member_list(ch_hash)

        # Spy on Bob's SyncManager -- his join is what should trigger an
        # outbound sync request to Alice.
        sync_requests_seen = []
        orig_send_sync_request = bob.sync_mgr._send_sync_request

        def spy(dest_hex, channel_hash_hex, since_ts, continuation=False):
            sync_requests_seen.append((dest_hex, channel_hash_hex))
            return orig_send_sync_request(dest_hex, channel_hash_hex, since_ts,
                                          continuation)

        bob.sync_mgr._send_sync_request = spy

        def on_invite(channel_hash_hex, channel_name, token, expiry, admin_hex):
            bob.invite_mgr.send_join_request(channel_hash_hex, token, expiry, admin_hex)

        bob.invite_mgr.add_invite_callback(on_invite)
        alice.invite_mgr.send_invite(ch_hash, bob.identity.hash_hex)

        assert wait_for_member(alice.storage, ch_hash, bob.identity.hash_hex, timeout=5), \
            "Bob never joined"

        assert wait_for(
            lambda: any(dest == alice.identity.hash_hex and ch == ch_hash
                       for dest, ch in sync_requests_seen),
            timeout=5,
        ), "Bob's SyncManager never sent a sync request to Alice after joining"

    def test_new_member_receives_history_from_before_they_joined_when_full_sync_enabled(
        self, peer_factory
    ):
        """
        End-to-end: joining an invite-only channel with full_sync enabled
        backfills the channel's existing history via the join-triggered
        sync request, not just future messages.

        full_sync is off by default (see TestTenureSyncFiltering's
        test_pre_join_history_excluded_by_default) -- a plain invite-only
        channel restricts a new member's sync to messages sent since they
        joined. This test covers the case where the member role has been
        granted the full_sync permission, exercising it through the *real*
        end-to-end pipeline: invite -> join request -> auto-join ->
        SyncManager's channel_joined-triggered sync request -- not a
        manually-invoked sync call.

        This also depends on the member-list document carrying each
        member's real (signed) original join time (invite.py's joined_at
        field) -- without it, a new joiner's local tenure view only starts
        at the moment they personally observed each member, and an existing
        member's genuinely older messages get filtered by the *receiver's*
        own tenure check even once the sync request itself is working.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        perms = dict(PRESET_PRIVATE)
        perms[ROLE_MEMBER] = [SEND_MESSAGE, FULL_SYNC]
        ch_hash = alice.channel_mgr.create_channel("history-on-join", "", permissions=perms)
        alice.invite_mgr.publish_member_list(ch_hash)

        alice.messaging.send_message(
            channel_hash_hex=ch_hash,
            content="sent before Bob was even invited",
            subscriber_hashes=[alice.identity.hash_hex],
        )
        pre_join_id = alice.storage.get_messages(ch_hash)[0]["message_id"]
        # send_message has no timestamp override, so guarantee a real clock
        # tick has passed before Bob joins -- see
        # test_pre_join_history_excluded_by_default for why an implicit
        # ordering assumption alone isn't safe here (Windows' time.time()
        # resolution can return the same value across calls a few ms apart).
        time.sleep(0.02)

        def on_invite(channel_hash_hex, channel_name, token, expiry, admin_hex):
            bob.invite_mgr.send_join_request(channel_hash_hex, token, expiry, admin_hex)

        bob.invite_mgr.add_invite_callback(on_invite)
        alice.invite_mgr.send_invite(ch_hash, bob.identity.hash_hex)

        assert wait_for_member(alice.storage, ch_hash, bob.identity.hash_hex, timeout=5), \
            "Bob never joined"

        assert wait_for_message(bob.storage, ch_hash, pre_join_id, timeout=5), \
            "Bob did not receive the message Alice sent before he joined"

    def test_new_member_does_not_receive_pre_join_history_by_default(self, peer_factory):
        """
        Mirror of the full_sync-enabled case above, but for the default
        (full_sync off) channel -- confirms the join-triggered auto-sync
        (SyncManager._on_channel_joined) and the tenure filter interact
        correctly through the real end-to-end invite/join pipeline, not
        just when sync is triggered manually.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("history-on-join-default", "", "invite")
        alice.invite_mgr.publish_member_list(ch_hash)

        alice.messaging.send_message(
            channel_hash_hex=ch_hash,
            content="sent before Bob was even invited",
            subscriber_hashes=[alice.identity.hash_hex],
        )
        pre_join_id = alice.storage.get_messages(ch_hash)[0]["message_id"]
        # send_message has no timestamp override, so guarantee a real clock
        # tick has passed before Bob joins -- see
        # test_pre_join_history_excluded_by_default for why an implicit
        # ordering assumption alone isn't safe here (Windows' time.time()
        # resolution can return the same value across calls a few ms apart).
        time.sleep(0.02)

        def on_invite(channel_hash_hex, channel_name, token, expiry, admin_hex):
            bob.invite_mgr.send_join_request(channel_hash_hex, token, expiry, admin_hex)

        bob.invite_mgr.add_invite_callback(on_invite)
        alice.invite_mgr.send_invite(ch_hash, bob.identity.hash_hex)

        assert wait_for_member(alice.storage, ch_hash, bob.identity.hash_hex, timeout=5), \
            "Bob never joined"
        assert wait_for(lambda: bob.storage.is_member(ch_hash, bob.identity.hash_hex), timeout=5)

        time.sleep(1.0)  # let the join-triggered sync request round-trip
        assert not bob.storage.message_exists(pre_join_id), \
            "Bob received pre-join history via the auto-triggered sync despite full_sync being off"

    def test_forged_tenure_claim_from_untrusted_signer_still_rejected(self, peer_factory):
        """
        Security regression test: joined_at only extends trust as far as
        the signer was already trusted to vouch for -- it must not let an
        untrusted party inject messages attributed to someone who was
        never actually a legitimately-signed member.

        Bob crafts a member-list doc claiming Carol has been a member
        since the dawn of time (and signs it himself, since he's not an
        admin and has no legitimate signature to offer). Alice must
        reject the whole document, same as she would without any
        joined_at claim at all -- _validate_document's signer-trust check
        runs before joined_at is ever consulted.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = alice.channel_mgr.create_channel("no-forged-tenure", "", "invite")
        alice.invite_mgr.publish_member_list(ch_hash, add_members=[bob.identity.hash])
        assert wait_for_member(alice.storage, ch_hash, bob.identity.hash_hex, timeout=5)

        bob.storage.upsert_channel(ch_hash, "no-forged-tenure", "", alice.identity.hash_hex,
                                   "invite", time.time())
        bob.storage.subscribe(ch_hash)

        forged_doc = bob.invite_mgr._build_document(
            ch_hash,
            members=[alice.identity.hash, bob.identity.hash, carol.identity.hash],
            admins=[alice.identity.hash],
            version=99,
            published_at=time.time(),
            owners=[alice.identity.hash],
            joined_at={carol.identity.hash: 0.0},  # claims Carol joined at the Unix epoch
        )
        accepted = alice.invite_mgr._accept_document(forged_doc, ch_hash)

        assert not accepted, "Alice accepted a member list doc signed by a non-admin"
        assert not alice.storage.is_member(ch_hash, carol.identity.hash_hex), \
            "Carol was added to Alice's member list via a forged, unsigned-by-a-trusted-party doc"


# ---------------------------------------------------------------------------
# Membership tenure — sync filtering
# ---------------------------------------------------------------------------

def _confirm_membership(peer, ch_hash, timeout: float = 5.0):
    """Confirm a membership an admin created for us without an invite.

    A member list document for a channel the recipient cannot anchor trust for
    is held rather than applied; the user confirms it. Tests that add a member
    directly with publish_member_list go through this instead of joining
    automatically.
    """
    assert wait_for(
        lambda: peer.storage.get_pending_member_doc(ch_hash) is not None,
        timeout=timeout,
    ), "no pending membership arrived to confirm"
    assert peer.invite_mgr.accept_pending_membership(ch_hash), \
        "pending membership was rejected on confirmation"


def _setup_invite_channel(peer_factory):
    """Create alice (owner) and bob (member) on a shared invite-only channel."""
    alice = peer_factory("alice")
    bob = peer_factory("bob")
    perms = dict(PRESET_PRIVATE)
    perms[ROLE_MEMBER] = [SEND_MESSAGE]
    ch_hash = alice.channel_mgr.create_channel("tenure-ch", "", permissions=perms)
    # Mirror the channel on Bob *before* publishing. The member list document
    # is delivered asynchronously, and a peer only accepts one for a channel it
    # can anchor trust for -- a stored channel record naming the creator, or an
    # invite it accepted. Seeding the record first is what the real invite flow
    # achieves, and it also removes a race between the document arriving and
    # the test's own mirroring.
    bob.storage.upsert_channel(ch_hash, "tenure-ch", "", alice.identity.hash_hex,
                               perms, time.time())
    bob.storage.subscribe(ch_hash)
    alice.invite_mgr.publish_member_list(ch_hash, add_members=[bob.identity.hash])
    assert wait_for_member(alice.storage, ch_hash, bob.identity.hash_hex)
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
            author_sig=sign_as(bob.identity.hash_hex, ch_hash, valid_msg_id,
                               valid_ts, valid_content),
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

    def test_owner_message_survives_sync_to_a_newly_added_member(self, peer_factory):
        """
        Regression test: create_channel() used to never open a tenure record
        for the channel owner. was_member_at() treats "no tenure data" as
        "wasn't a member", so once *any* tenure data existed for a channel
        (as soon as one real member was added), the owner's own messages
        were silently dropped from every sync response -- regardless of
        when the requesting member actually joined. A new member would
        never see anything the owner sent, not even messages sent after
        they joined, since the same untenured-sender filter applies to all
        of the owner's history alike.

        Uses a message sent *after* Bob joins -- history from before he
        joined is a separate, deliberate boundary (see
        test_pre_join_history_excluded_by_default /
        test_full_sync_enabled_allows_pre_join_history below), not what
        this test is checking.

        Goes through the real create_channel() -> publish_member_list()
        path end to end rather than manually seeding tenure rows (the
        pattern the other tests in this class use), since manually seeding
        both sides' tenure is exactly what would paper over this gap.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("owner-tenure-ch", "", "invite")

        alice.invite_mgr.publish_member_list(ch_hash, add_members=[bob.identity.hash])
        assert wait_for_member(alice.storage, ch_hash, bob.identity.hash_hex)
        # Wait for Bob's own side to actually process the real broadcast
        # document (not manually seeded) -- his own tenure records, for
        # both himself and Alice, need to come from the real accept flow,
        # since that's exactly the path the joined_at fix lives in.
        _confirm_membership(bob, ch_hash)
        assert wait_for(lambda: bob.storage.is_member(ch_hash, bob.identity.hash_hex), timeout=5)

        after_msg_id = _insert_message(alice.storage, ch_hash, alice.identity.hash_hex,
                                       "sent after Bob joined")

        bob.sync_mgr._send_sync_request(alice.identity.hash_hex, ch_hash, 0.0)

        assert wait_for_message(bob.storage, ch_hash, after_msg_id, timeout=5), \
            "Bob never received the owner's message via sync -- owner's tenure " \
            "was likely never opened, so was_member_at() rejected it"

    def test_pre_join_history_excluded_by_default(self, peer_factory):
        """
        By default (full_sync off), a new member's sync/backfill is bounded
        by their own join time -- a message sent before they joined an
        invite-only channel must not reach them via sync, even though the
        sender was a legitimate, tenured member the whole time.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("bounded-sync-ch", "", "invite")
        # A real sleep between each ordering boundary, not just call order --
        # time.time() on Windows can return the identical value across calls
        # a few ms apart, and was_member_at()'s joined_at <= timestamp check
        # then treats "joined in the same clock tick" as "was already a
        # member," admitting a message that was, in program order, inserted
        # before the join. A fixed backdate offset isn't safe either: too
        # large and it can predate the channel's own creation (failing the
        # *sender* tenure check instead), so an actual elapsed tick on both
        # sides of the message is what actually removes the ambiguity.
        time.sleep(0.02)
        before_msg_id = _insert_message(alice.storage, ch_hash, alice.identity.hash_hex,
                                        "sent before Bob joined")
        time.sleep(0.02)

        alice.invite_mgr.publish_member_list(ch_hash, add_members=[bob.identity.hash])
        assert wait_for_member(alice.storage, ch_hash, bob.identity.hash_hex)
        _confirm_membership(bob, ch_hash)
        assert wait_for(lambda: bob.storage.is_member(ch_hash, bob.identity.hash_hex), timeout=5)

        bob.sync_mgr._send_sync_request(alice.identity.hash_hex, ch_hash, 0.0)
        time.sleep(0.5)

        assert not bob.storage.message_exists(before_msg_id), \
            "Bob received pre-join history via sync despite full_sync being off by default"

    def test_full_sync_enabled_allows_pre_join_history(self, peer_factory):
        """
        An admin who grants the member role full_sync lets new members
        backfill the channel's entire history via sync, including messages
        from before they joined -- the opt-in this permission exists to
        provide.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        perms = dict(PRESET_PRIVATE)
        perms[ROLE_MEMBER] = [SEND_MESSAGE, FULL_SYNC]
        ch_hash = alice.channel_mgr.create_channel("full-sync-ch", "", permissions=perms)
        # See test_pre_join_history_excluded_by_default for why these sleeps
        # are needed instead of relying on call ordering alone.
        time.sleep(0.02)
        before_msg_id = _insert_message(alice.storage, ch_hash, alice.identity.hash_hex,
                                        "sent before Bob joined")
        time.sleep(0.02)

        alice.invite_mgr.publish_member_list(ch_hash, add_members=[bob.identity.hash])
        assert wait_for_member(alice.storage, ch_hash, bob.identity.hash_hex)
        _confirm_membership(bob, ch_hash)
        assert wait_for(lambda: bob.storage.is_member(ch_hash, bob.identity.hash_hex), timeout=5)

        bob.sync_mgr._send_sync_request(alice.identity.hash_hex, ch_hash, 0.0)

        assert wait_for_message(bob.storage, ch_hash, before_msg_id, timeout=5), \
            "Bob did not receive pre-join history via sync despite full_sync being enabled"

    def test_withheld_history_does_not_advance_the_watermark(self, peer_factory,
                                                             monkeypatch):
        """
        Withholding is a permission decision, and permissions change. If a
        watermark moved past history that was withheld, granting full_sync
        later could never recover it -- the requester would ask only for
        messages newer than history it never received, and report itself up
        to date.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("withheld-watermark", "", "invite")
        # See test_pre_join_history_excluded_by_default for why these sleeps
        # are needed instead of relying on call ordering alone.
        time.sleep(0.02)
        before_msg_id = _insert_message(alice.storage, ch_hash, alice.identity.hash_hex,
                                        "sent before Bob joined")
        time.sleep(0.02)

        alice.invite_mgr.publish_member_list(ch_hash, add_members=[bob.identity.hash])
        assert wait_for_member(alice.storage, ch_hash, bob.identity.hash_hex)
        _confirm_membership(bob, ch_hash)
        assert wait_for(lambda: bob.storage.is_member(ch_hash, bob.identity.hash_hex), timeout=5)

        bob.sync_mgr._send_sync_request(alice.identity.hash_hex, ch_hash, 0.0)
        time.sleep(0.5)

        assert not bob.storage.message_exists(before_msg_id), \
            "test setup: pre-join history should be withheld with full_sync off"
        assert bob.storage.get_last_sync(ch_hash) == 0.0, \
            "the watermark advanced past history Bob was never sent"

        # Grant full_sync; the backfill Bob was previously refused must now
        # reach him, which is only possible if his watermark stayed put.
        perms = dict(alice.storage.get_channel_permissions(ch_hash))
        perms[ROLE_MEMBER] = list(perms.get(ROLE_MEMBER, [])) + [FULL_SYNC]
        alice.storage.set_channel_permissions(ch_hash, perms)
        bob.storage.set_channel_permissions(ch_hash, perms)

        # Recovery still reaches back to 0, so it is a deep request like the
        # first one; in the real app the cooldown just paces it by a minute.
        monkeypatch.setattr("trenchchat.core.sync.DEEP_SYNC_COOLDOWN_SECS", 0)
        bob.sync_mgr._send_sync_request(alice.identity.hash_hex, ch_hash,
                                        bob.storage.get_last_sync(ch_hash))

        assert wait_for_message(bob.storage, ch_hash, before_msg_id, timeout=5), \
            "history withheld before the grant never arrived after it"

    def test_sweep_scans_past_withheld_history(self, peer_factory):
        """
        A new member behind a full batch of history they may not see must
        still reach the messages they may: the responder sweeps past what it
        withholds instead of answering with an empty batch.
        """
        from trenchchat.core.sync import MAX_RESPONSE_MESSAGES

        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("sweep-past", "", "invite")
        time.sleep(0.02)
        for i in range(MAX_RESPONSE_MESSAGES + 5):
            _insert_message(alice.storage, ch_hash, alice.identity.hash_hex,
                            f"before Bob joined {i}")
        time.sleep(0.02)

        alice.invite_mgr.publish_member_list(ch_hash, add_members=[bob.identity.hash])
        assert wait_for_member(alice.storage, ch_hash, bob.identity.hash_hex)
        _confirm_membership(bob, ch_hash)
        assert wait_for(lambda: bob.storage.is_member(ch_hash, bob.identity.hash_hex), timeout=5)

        time.sleep(0.02)
        after_msg_id = _insert_message(alice.storage, ch_hash, alice.identity.hash_hex,
                                       "sent after Bob joined")

        bob.sync_mgr._send_sync_request(alice.identity.hash_hex, ch_hash, 0.0)

        assert wait_for_message(bob.storage, ch_hash, after_msg_id, timeout=5), \
            "Bob never reached the message he was entitled to behind a batch " \
            "of withheld pre-join history"

    def test_full_sync_granted_to_admin_but_not_member(self, peer_factory):
        """
        The scenario full_sync being a per-role permission (rather than a
        channel-wide flag) exists for: an admin can be trusted to backfill
        the entire channel history while ordinary members stay bounded to
        their own join time, on the very same channel.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")     # plain member -- no full_sync
        carol = peer_factory("carol")  # admin -- granted full_sync

        perms = dict(PRESET_PRIVATE)
        perms[ROLE_ADMIN] = [SEND_MESSAGE, FULL_SYNC]
        ch_hash = alice.channel_mgr.create_channel("admin-only-full-sync-ch", "",
                                                    permissions=perms)
        # See test_pre_join_history_excluded_by_default for why these sleeps
        # are needed instead of relying on call ordering alone.
        time.sleep(0.02)
        before_msg_id = _insert_message(alice.storage, ch_hash, alice.identity.hash_hex,
                                        "sent before Bob and Carol joined")
        time.sleep(0.02)

        alice.invite_mgr.publish_member_list(
            ch_hash, add_members=[bob.identity.hash, carol.identity.hash],
            add_admins=[carol.identity.hash],
        )
        assert wait_for_member(alice.storage, ch_hash, bob.identity.hash_hex)
        assert wait_for_member(alice.storage, ch_hash, carol.identity.hash_hex)
        _confirm_membership(bob, ch_hash)
        assert wait_for(lambda: bob.storage.is_member(ch_hash, bob.identity.hash_hex), timeout=5)
        _confirm_membership(carol, ch_hash)
        assert wait_for(lambda: carol.storage.is_member(ch_hash, carol.identity.hash_hex), timeout=5)

        bob.sync_mgr._send_sync_request(alice.identity.hash_hex, ch_hash, 0.0)
        carol.sync_mgr._send_sync_request(alice.identity.hash_hex, ch_hash, 0.0)
        time.sleep(0.5)

        assert not bob.storage.message_exists(before_msg_id), \
            "Bob (plain member, no full_sync) received pre-join history"
        assert wait_for_message(carol.storage, ch_hash, before_msg_id, timeout=5), \
            "Carol (admin, granted full_sync) did not receive pre-join history"

    def test_new_member_receives_departed_members_pre_kick_history(self, peer_factory):
        """
        Regression test for a real gap: a brand-new joiner has no prior
        local state, so update_tenure's added/removed diff -- which only
        fires relative to what a peer already knew -- could never teach
        them about anyone who left before they joined. Every message from
        a departed member was silently and permanently unsyncable to any
        future joiner, regardless of full_sync. The departed-member entries
        a signed member-list document now carries close that gap.

        full_sync is granted to the member role so this isolates the fix:
        without it, Carol would be blocked by the *requester*-tenure
        boundary regardless (she joined after Bob left), which is a
        separate, correct restriction already covered by
        test_pre_join_history_excluded_by_default.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        perms = dict(PRESET_PRIVATE)
        perms[ROLE_MEMBER] = [SEND_MESSAGE, FULL_SYNC]
        ch_hash = alice.channel_mgr.create_channel("departed-ch", "", permissions=perms)

        alice.invite_mgr.publish_member_list(ch_hash, add_members=[bob.identity.hash])
        assert wait_for_member(alice.storage, ch_hash, bob.identity.hash_hex)
        _confirm_membership(bob, ch_hash)
        assert wait_for(lambda: bob.storage.is_member(ch_hash, bob.identity.hash_hex), timeout=5)

        pre_kick_msg_id = _insert_message(alice.storage, ch_hash, bob.identity.hash_hex,
                                          "Bob's message before he left")
        time.sleep(0.02)

        alice.invite_mgr.publish_member_list(ch_hash, remove_members=[bob.identity.hash])
        assert wait_for(
            lambda: not alice.storage.is_member(ch_hash, bob.identity.hash_hex), timeout=5
        )
        time.sleep(0.02)

        alice.invite_mgr.publish_member_list(ch_hash, add_members=[carol.identity.hash])
        assert wait_for_member(alice.storage, ch_hash, carol.identity.hash_hex)
        _confirm_membership(carol, ch_hash)
        assert wait_for(lambda: carol.storage.is_member(ch_hash, carol.identity.hash_hex), timeout=5)

        carol.sync_mgr._send_sync_request(alice.identity.hash_hex, ch_hash, 0.0)

        assert wait_for_message(carol.storage, ch_hash, pre_kick_msg_id, timeout=5), \
            "Carol did not receive Bob's pre-kick message -- she has no way to " \
            "validate his tenure without the departed-member entries the signed " \
            "document is now supposed to carry"

    def test_departed_member_tenure_does_not_cover_post_kick_message(self, peer_factory):
        """
        Security boundary: departed-member tenure only covers the actual
        interval -- a message purportedly from Bob timestamped after his
        kick must still be rejected by a brand-new joiner, the same as it
        already is for a peer that witnessed the kick directly
        (test_sync_response_rejects_gap_message).
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        perms = dict(PRESET_PRIVATE)
        perms[ROLE_MEMBER] = [SEND_MESSAGE, FULL_SYNC]
        ch_hash = alice.channel_mgr.create_channel("departed-gap-ch", "", permissions=perms)

        alice.invite_mgr.publish_member_list(ch_hash, add_members=[bob.identity.hash])
        assert wait_for_member(alice.storage, ch_hash, bob.identity.hash_hex)
        _confirm_membership(bob, ch_hash)
        assert wait_for(lambda: bob.storage.is_member(ch_hash, bob.identity.hash_hex), timeout=5)

        time.sleep(0.02)
        alice.invite_mgr.publish_member_list(ch_hash, remove_members=[bob.identity.hash])
        assert wait_for(
            lambda: not alice.storage.is_member(ch_hash, bob.identity.hash_hex), timeout=5
        )
        time.sleep(0.02)

        # A message purportedly from Bob, timestamped after his kick
        gap_msg_id = _insert_message(alice.storage, ch_hash, bob.identity.hash_hex,
                                     "Gap message after kick")
        time.sleep(0.02)

        alice.invite_mgr.publish_member_list(ch_hash, add_members=[carol.identity.hash])
        assert wait_for_member(alice.storage, ch_hash, carol.identity.hash_hex)
        _confirm_membership(carol, ch_hash)
        assert wait_for(lambda: carol.storage.is_member(ch_hash, carol.identity.hash_hex), timeout=5)

        carol.sync_mgr._send_sync_request(alice.identity.hash_hex, ch_hash, 0.0)
        time.sleep(0.5)

        assert not carol.storage.message_exists(gap_msg_id), \
            "Carol accepted a message from Bob timestamped after his departed-tenure " \
            "interval closed -- departed-member trust must not extend past left_at"

    def test_no_tenure_data_allows_sync_without_filtering(self, peer_factory):
        """
        When no tenure data exists for a channel (e.g. one bootstrapped
        before tenure tracking), sync proceeds without filtering — no false
        rejections.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = mirrored_invite_channel("no-tenure-sync", alice, bob)

        ts = time.time()
        msg_id = _insert_message(alice.storage, ch_hash, alice.identity.hash_hex,
                                  "No tenure check needed", ts + 1)

        # No tenure rows — has_any_tenure returns False, filter is bypassed
        assert not bob.storage.has_any_tenure(ch_hash)

        bob.sync_mgr._send_sync_request(alice.identity.hash_hex, ch_hash, ts)

        assert wait_for_message(bob.storage, ch_hash, msg_id, timeout=5), \
            "Message was incorrectly rejected when no tenure data exists"


# ---------------------------------------------------------------------------
# Image data in sync
# ---------------------------------------------------------------------------

_FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 100


class TestImageSync:
    def test_row_to_dict_includes_image_data(self):
        """_row_to_dict serialises image_data as bytes."""
        from trenchchat.core.sync import SyncManager

        class _FakeRow(dict):
            """Minimal sqlite3.Row substitute for testing."""
            def keys(self):
                return list(self.keys()) if False else list(super().keys())

        # Use a real sqlite3.Row via a temp database
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE t (
                sender_hash TEXT, sender_name TEXT, content TEXT,
                timestamp REAL, message_id TEXT, reply_to TEXT,
                last_seen_id TEXT, image_data BLOB
            )
        """)
        conn.execute(
            "INSERT INTO t VALUES (?,?,?,?,?,?,?,?)",
            ("aabb", "Alice", "hi", 1000.0, "mid1", None, None, _FAKE_JPEG),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM t").fetchone()

        result = SyncManager._row_to_dict(row)
        assert "image_data" in result
        assert result["image_data"] == _FAKE_JPEG

    def test_row_to_dict_excludes_null_image_data(self):
        """_row_to_dict omits image_data key when the column is NULL."""
        from trenchchat.core.sync import SyncManager
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE t (
                sender_hash TEXT, sender_name TEXT, content TEXT,
                timestamp REAL, message_id TEXT, reply_to TEXT,
                last_seen_id TEXT, image_data BLOB
            )
        """)
        conn.execute(
            "INSERT INTO t VALUES (?,?,?,?,?,?,?,?)",
            ("aabb", "Alice", "no image", 1000.0, "mid2", None, None, None),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM t").fetchone()

        result = SyncManager._row_to_dict(row)
        assert "image_data" not in result

    def test_sync_round_trip_preserves_image(self, peer_factory):
        """An image attached to a message survives a sync request/response cycle."""
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = mirrored_invite_channel("img-sync", alice, bob)

        ts = time.time()
        img_id = _compute_message_id("synced image", alice.identity.hash_hex, ts)
        # Insert directly with image data
        alice.storage.insert_message(
            channel_hash=ch_hash,
            sender_hash=alice.identity.hash_hex,
            sender_name="Alice",
            content="synced image",
            timestamp=ts,
            message_id=img_id,
            reply_to=None,
            last_seen_id=None,
            received_at=ts,
            image_data=_FAKE_JPEG,
            author_sig=sign_as(alice.identity.hash_hex, ch_hash, img_id,
                               ts, "synced image", image_data=_FAKE_JPEG),
        )

        bob.sync_mgr._send_sync_request(alice.identity.hash_hex, ch_hash, ts - 100)

        assert wait_for_message(bob.storage, ch_hash, img_id, timeout=5), \
            "Bob did not receive the synced image message"

        bob_msgs = bob.storage.get_messages(ch_hash)
        synced = next(m for m in bob_msgs if m["message_id"] == img_id)
        assert bytes(synced["image_data"]) == _FAKE_JPEG


class TestServerScopedTenure:
    """Tenure lives at server scope. has_any_tenure() resolving there is
    load-bearing: if it didn't, sync.py would see no tenure rows under a
    server channel's own hash and silently disable filtering for it."""

    def test_tenure_filter_is_engaged_for_a_server_channel(self, peer_factory):
        from trenchchat.core import actions
        alice = peer_factory("alice")
        s = actions.create_server(alice.server_mgr, alice.invite_mgr, "S")
        ch = actions.create_channel_in_server(
            alice.storage, alice.channel_mgr, alice.invite_mgr,
            s, alice.identity.hash_hex, "general")
        assert alice.storage.has_any_tenure(ch) is True, \
            "tenure filtering would fail open for every channel in this server"

    def test_member_gets_no_history_from_before_they_joined_the_server(self, peer_factory):
        from trenchchat.core import actions
        from trenchchat.core.permissions import PRESET_SERVER
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        s = actions.create_server(alice.server_mgr, alice.invite_mgr, "S")
        ch = actions.create_channel_in_server(
            alice.storage, alice.channel_mgr, alice.invite_mgr,
            s, alice.identity.hash_hex, "general")

        actions.send_message(alice.storage, alice.subscription_mgr, alice.messaging,
                             ch, alice.identity.hash_hex, "before bob joined")
        time.sleep(0.2)

        def on_invite(scope_hex, name, token, expiry, admin_hex):
            bob.invite_mgr.send_join_request(scope_hex, token, expiry, admin_hex)
        bob.invite_mgr.add_invite_callback(on_invite)
        alice.invite_mgr.send_invite(s, bob.identity.hash_hex)
        assert wait_for_member(alice.storage, s, bob.identity.hash_hex, timeout=5)
        assert wait_for(lambda: bob.storage.get_channel(ch) is not None, timeout=5)

        bob.sync_mgr._request_sync_for_channel(ch, 0.0)
        time.sleep(1.0)
        contents = [m["content"] for m in bob.storage.get_messages(ch)]
        assert "before bob joined" not in contents, \
            "pre-join history leaked to a member without full_sync"


class TestReactionSync:
    """Reactions ride along with the messages they belong to.

    Backfilled history used to arrive stripped of every reaction, so a peer
    that synced a channel disagreed with the rest of it indefinitely.
    """

    def _seed_message(self, peer, ch_hash: str, msg_id: str, ts: float,
                      content: str = "reacted message") -> None:
        peer.storage.insert_message(
            channel_hash=ch_hash,
            sender_hash=peer.identity.hash_hex,
            sender_name="Alice",
            content=content,
            timestamp=ts,
            message_id=msg_id,
            reply_to=None,
            last_seen_id=None,
            received_at=ts,
            author_sig=sign_as(peer.identity.hash_hex, ch_hash, msg_id, ts,
                               content),
        )

    def test_synced_message_carries_its_reactions(self, peer_factory):
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = mirrored_invite_channel("reaction-sync", alice, bob)

        ts = time.time()
        rx_id = _compute_message_id("reacted message", alice.identity.hash_hex, ts)
        self._seed_message(alice, ch_hash, rx_id, ts)
        alice.storage.insert_reaction(rx_id, "\U0001F44D",
                                      alice.identity.hash_hex, ch_hash, ts)
        alice.storage.insert_reaction(rx_id, "e3" * 32,
                                      alice.identity.hash_hex, ch_hash, ts)

        bob.sync_mgr._send_sync_request(alice.identity.hash_hex, ch_hash, ts - 100)

        assert wait_for_message(bob.storage, ch_hash, rx_id, timeout=5), \
            "Bob did not receive the synced message"
        assert wait_for(
            lambda: len(bob.storage.get_reactions(rx_id)) == 2, timeout=5
        ), "Bob received the message but not its reactions"

        keys = {r["emoji_hash"] for r in bob.storage.get_reactions(rx_id)}
        assert keys == {"\U0001F44D", "e3" * 32}
        assert all(r["reactor_hash"] == alice.identity.hash_hex
                   for r in bob.storage.get_reactions(rx_id))

    def test_message_with_no_reactions_omits_the_key(self, peer_factory):
        """The payload only grows for messages that actually carry reactions."""
        alice = peer_factory("alice")
        ch_hash = alice.channel_mgr.create_channel("no-reactions", "", "public")
        ts = time.time()
        self._seed_message(alice, ch_hash, "sync_rx_002", ts)

        row = next(m for m in alice.storage.get_messages(ch_hash)
                   if m["message_id"] == "sync_rx_002")
        assert "reactions" not in alice.sync_mgr._row_to_payload(row)

    def test_synced_reactions_are_capped_per_message(self, peer_factory):
        alice = peer_factory("alice")
        ch_hash = alice.channel_mgr.create_channel("many-reactions", "", "public")
        ts = time.time()
        self._seed_message(alice, ch_hash, "sync_rx_003", ts)
        for i in range(MAX_REACTIONS_PER_MESSAGE + 8):
            alice.storage.insert_reaction(
                "sync_rx_003", f"{i:064x}", f"{i:032x}", ch_hash, ts,
            )

        row = next(m for m in alice.storage.get_messages(ch_hash)
                   if m["message_id"] == "sync_rx_003")
        payload = alice.sync_mgr._row_to_payload(row)
        assert len(payload["reactions"]) == MAX_REACTIONS_PER_MESSAGE


# ---------------------------------------------------------------------------
# Response truncation
# ---------------------------------------------------------------------------

from trenchchat.core.sync import (  # noqa: E402
    MAX_RESPONSE_BYTES, MAX_RESPONSE_MESSAGES, _truncate_at_group_boundary,
    row_wire_size,
)
from trenchchat.core.image import (  # noqa: E402
    MAX_IMAGE_BYTES,
)


class TestTruncationKeepsTimestampGroupsWhole:
    """A batch may not be cut through a run of equal timestamps.

    The resume point is a bare float and get_messages_after filters on a
    strict timestamp >, so whichever half of a group landed past the cut
    would be skipped by every later sweep.
    """

    def _rows(self, timestamps):
        return [{"timestamp": ts, "message_id": f"m{i}"}
                for i, ts in enumerate(timestamps)]

    def test_short_batch_is_untouched(self):
        rows = self._rows([1.0, 2.0, 3.0])
        out, dropped = _truncate_at_group_boundary(rows)
        assert out == rows and dropped is False

    def test_cut_backs_off_to_the_group_start(self):
        # The cap lands mid-group: every row sharing that timestamp is held
        # back together for the next batch.
        tied = 500.0
        timestamps = list(range(MAX_RESPONSE_MESSAGES - 2))
        timestamps = [float(t) for t in timestamps] + [tied, tied, tied]
        rows = self._rows(timestamps)
        out, dropped = _truncate_at_group_boundary(rows)

        assert dropped is True
        assert len(out) == MAX_RESPONSE_MESSAGES - 2
        assert all(r["timestamp"] != tied for r in out), \
            "a tied-timestamp group was split across the response cap"

    def test_single_group_over_the_cap_ships_whole(self):
        # Backing off would leave nothing to send and stall forever, so an
        # oversized single group goes out intact.
        rows = self._rows([7.0] * (MAX_RESPONSE_MESSAGES + 5))
        out, dropped = _truncate_at_group_boundary(rows)

        assert len(out) == MAX_RESPONSE_MESSAGES + 5
        assert dropped is False

    def test_group_boundary_exactly_at_the_cap_is_kept(self):
        timestamps = [float(t) for t in range(MAX_RESPONSE_MESSAGES)] + [999.0]
        rows = self._rows(timestamps)
        out, dropped = _truncate_at_group_boundary(rows)

        assert len(out) == MAX_RESPONSE_MESSAGES
        assert dropped is True


# ---------------------------------------------------------------------------
# A response is capped in bytes, not only in rows
# ---------------------------------------------------------------------------

class TestResponseByteBudget:
    """Every other cap counts rows, but the cost is bytes.

    MAX_RESPONSE_MESSAGES rows each carrying a full-size image is ~45 MB,
    far past what unpack_wire accepts -- so the batch was discarded as
    malformed at the other end and the window could never sync, while each
    attempt put tens of megabytes on a shared medium.
    """

    def _row(self, ts: float, image_len: int) -> dict:
        return {"timestamp": ts, "content": "x",
                "image_data": b"\x00" * image_len, "message_id": f"m{ts}"}

    def test_a_batch_of_images_is_cut_by_bytes(self):
        rows = [self._row(float(i), MAX_IMAGE_BYTES) for i in range(20)]
        kept, dropped = _truncate_at_group_boundary(rows)

        assert dropped, "an oversized batch was not truncated"
        assert len(kept) < len(rows)
        assert sum(row_wire_size(r) for r in kept) <= MAX_RESPONSE_BYTES

    def test_a_single_oversized_row_still_ships(self):
        """Otherwise one row past the budget stalls the channel forever."""
        rows = [self._row(1.0, MAX_RESPONSE_BYTES * 2)]
        kept, dropped = _truncate_at_group_boundary(rows)

        assert kept == rows
        assert not dropped

    def test_small_rows_are_still_capped_by_count(self):
        rows = [self._row(float(i), 0) for i in range(MAX_RESPONSE_MESSAGES + 10)]
        kept, dropped = _truncate_at_group_boundary(rows)

        assert dropped
        assert len(kept) == MAX_RESPONSE_MESSAGES

    def test_a_tied_timestamp_group_is_never_split(self):
        """A group split across a cut is stranded by a strict timestamp >."""
        rows = [self._row(1.0, MAX_IMAGE_BYTES) for _ in range(6)]
        kept, _ = _truncate_at_group_boundary(rows)

        assert len(kept) in (0, len(rows)), "a tied-timestamp group was split"


# ---------------------------------------------------------------------------
# Announce-driven sync cooldown
# ---------------------------------------------------------------------------

class TestAnnounceSyncCooldown:
    def _shared_channel(self, peer_factory):
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        ch_hash = mirrored_invite_channel("announce-cooldown", alice, bob)
        return alice, bob, ch_hash

    def _spy_requests(self, peer):
        seen = []
        orig = peer.sync_mgr._send_sync_request

        def spy(dest_hex, channel_hash_hex, since_ts, continuation=False):
            seen.append((dest_hex, channel_hash_hex))
            return orig(dest_hex, channel_hash_hex, since_ts, continuation)

        peer.sync_mgr._send_sync_request = spy
        return seen

    def test_repeat_announce_within_cooldown_sends_one_request(self, peer_factory):
        """Announces repeat on a heartbeat; only the first within the
        cooldown may cost a sync request round trip."""
        alice, bob, ch_hash = self._shared_channel(peer_factory)
        seen = self._spy_requests(bob)

        bob.sync_mgr.on_peer_appeared(alice.identity.hash_hex)
        bob.sync_mgr.on_peer_appeared(alice.identity.hash_hex)

        assert seen.count((alice.identity.hash_hex, ch_hash)) == 1, \
            "a repeat announce within the cooldown re-triggered a sync request"

    def test_announce_after_cooldown_sends_again(self, peer_factory, monkeypatch):
        from trenchchat.core import sync as sync_module
        monkeypatch.setattr(sync_module, "ANNOUNCE_SYNC_COOLDOWN_SECS", 0.2)

        alice, bob, ch_hash = self._shared_channel(peer_factory)
        seen = self._spy_requests(bob)

        bob.sync_mgr.on_peer_appeared(alice.identity.hash_hex)
        time.sleep(0.3)
        bob.sync_mgr.on_peer_appeared(alice.identity.hash_hex)

        assert seen.count((alice.identity.hash_hex, ch_hash)) == 2, \
            "an announce after the cooldown elapsed did not send a fresh request"

    def test_request_sync_all_resets_the_cooldown(self, peer_factory):
        """Our own link returning runs request_sync_all; a cooldown armed
        before the outage must not suppress the announce-driven requests
        that follow it."""
        alice, bob, ch_hash = self._shared_channel(peer_factory)
        seen = self._spy_requests(bob)

        bob.sync_mgr.on_peer_appeared(alice.identity.hash_hex)
        bob.sync_mgr.request_sync_all()

        before = seen.count((alice.identity.hash_hex, ch_hash))
        bob.sync_mgr.on_peer_appeared(alice.identity.hash_hex)

        assert seen.count((alice.identity.hash_hex, ch_hash)) == before + 1, \
            "request_sync_all did not lift the announce cooldown"

    def test_cooldown_is_per_peer(self, peer_factory):
        """One peer's cooldown must not swallow another peer's first request."""
        alice, bob, ch_hash = self._shared_channel(peer_factory)
        carol = peer_factory("carol")
        bob.storage.upsert_member(ch_hash, carol.identity.hash_hex, "",
                                  role=ROLE_MEMBER)
        seen = self._spy_requests(bob)

        bob.sync_mgr.on_peer_appeared(alice.identity.hash_hex)
        bob.sync_mgr.on_peer_appeared(carol.identity.hash_hex)

        assert seen.count((alice.identity.hash_hex, ch_hash)) == 1
        assert seen.count((carol.identity.hash_hex, ch_hash)) == 1


# ---------------------------------------------------------------------------
# Public channels are live-only
# ---------------------------------------------------------------------------

class TestPublicChannelsAreLiveOnly:
    """An open-join channel keeps no shared history, like an IRC channel.

    No sync request is ever sent for one, a request for one is refused, and
    missed-delivery hints are neither recorded, broadcast, nor accepted.
    Both sides enforce it, so a modified client can't pull or push history
    through a peer that plays by the rules. Only the sender's own pending
    retry (TestFlushPending) redelivers a missed message.
    """

    def test_no_sync_requests_go_out_for_a_public_channel(self, peer_factory):
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = hashlib.sha256(b"live-no-requests").hexdigest()[:32]
        seed_channel_on_peer(bob, ch_hash, "live-no-requests",
                              alice.identity.hash_hex, access_mode="public")
        bob.subscription_mgr._subscribers[ch_hash] = {alice.identity.hash_hex}

        seen = []
        bob.sync_mgr._send_sync_request = \
            lambda *args, **kwargs: seen.append(args) or True

        bob.sync_mgr.request_sync_all()
        bob.sync_mgr.on_peer_appeared(alice.identity.hash_hex)
        bob.sync_mgr._request_sync_for_channel(ch_hash, 0.0)

        assert seen == [], \
            "a sync request went out for a live-only public channel"

    def test_sync_request_for_a_public_channel_is_refused(self, peer_factory):
        """Core enforcement: any peer may participate in an open-join
        channel, so only the live-only gate stands between a requester and
        the transcript."""
        from trenchchat.core.protocol import (
            F_CHANNEL_HASH, F_MSG_TYPE, F_SYNC_WINDOW_START, MT_SYNC_REQUEST,
        )

        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("live-refuse", "", "public")
        _insert_message(alice.storage, ch_hash, alice.identity.hash_hex,
                        "not served to anyone", time.time())

        sent = []
        alice.sync_mgr._send_raw = lambda dest, fields: sent.append(fields) or True

        alice.sync_mgr._handle_sync_request(
            {
                F_MSG_TYPE:          MT_SYNC_REQUEST,
                F_CHANNEL_HASH:      bytes.fromhex(ch_hash),
                F_SYNC_WINDOW_START: 0.0,
            },
            ch_hash,
            bob.identity.hash_hex,
        )

        assert sent == [], \
            "a sync request for a live-only public channel was answered"

    def test_hints_are_not_recorded_or_broadcast_for_a_public_channel(
        self, peer_factory
    ):
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = alice.channel_mgr.create_channel("live-no-hints", "", "public")
        ts = time.time()
        msg_id = _insert_message(alice.storage, ch_hash,
                                 alice.identity.hash_hex, "missed live", ts)

        sent = []
        alice.sync_mgr._send_raw = lambda dest, fields: sent.append(fields) or True

        alice.sync_mgr._on_missed_delivery_event(
            channel_hash_hex=ch_hash,
            missed_peer_hex=bob.identity.hash_hex,
            msg_id=msg_id,
            subscriber_hashes=[alice.identity.hash_hex, bob.identity.hash_hex,
                               carol.identity.hash_hex],
        )

        assert sent == [], "a hint was broadcast for a live-only public channel"
        assert alice.storage.get_missed_message_ids(
            ch_hash, bob.identity.hash_hex
        ) == [], "a hint was recorded for a live-only public channel"

    def test_inbound_hint_for_a_public_channel_is_dropped(self, peer_factory):
        from trenchchat.core.protocol import (
            F_MISSED_FOR, F_MISSED_MSG_ID, F_MSG_TYPE, MT_MISSED_DELIVERY,
        )

        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = hashlib.sha256(b"live-hint-drop").hexdigest()[:32]
        seed_channel_on_peer(carol, ch_hash, "live-hint-drop",
                              alice.identity.hash_hex, access_mode="public")

        msg_id = _compute_message_id("anything", alice.identity.hash_hex, time.time())
        carol.sync_mgr._handle_missed_delivery(
            {
                F_MSG_TYPE:      MT_MISSED_DELIVERY,
                F_MISSED_FOR:    bob.identity.hash_hex,
                F_MISSED_MSG_ID: msg_id,
            },
            ch_hash,
            alice.identity.hash_hex,
        )

        assert carol.storage.get_missed_message_ids(
            ch_hash, bob.identity.hash_hex
        ) == [], "an inbound hint for a live-only public channel was recorded"

    def test_sync_response_for_a_public_channel_is_dropped(self, peer_factory):
        """Defence in depth: even a response answering a real outstanding
        request is refused once the channel is open-join."""
        from trenchchat.core.protocol import (
            F_CHANNEL_HASH, F_MSG_TYPE, F_SYNC_MESSAGES, F_SYNC_TRUNCATED,
            MT_SYNC_RESPONSE,
        )

        import msgpack

        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = hashlib.sha256(b"live-response-drop").hexdigest()[:32]
        seed_channel_on_peer(bob, ch_hash, "live-response-drop",
                              alice.identity.hash_hex, access_mode="public")
        bob.sync_mgr._record_pending_request(ch_hash, carol.identity.hash_hex, 0.0)

        ts = time.time()
        content = "history nobody asked to keep"
        msg_id = _compute_message_id(content, alice.identity.hash_hex, ts)
        bob.sync_mgr._handle_sync_response(
            {
                F_MSG_TYPE:       MT_SYNC_RESPONSE,
                F_CHANNEL_HASH:   bytes.fromhex(ch_hash),
                F_SYNC_MESSAGES:  msgpack.packb([{
                    "sender_hash":  alice.identity.hash_hex,
                    "sender_name":  "Alice",
                    "content":      content,
                    "timestamp":    ts,
                    "message_id":   msg_id,
                    "reply_to":     None,
                    "last_seen_id": None,
                    "author_sig":   sign_as(alice.identity.hash_hex, ch_hash,
                                            msg_id, ts, content),
                }], use_bin_type=True),
                F_SYNC_TRUNCATED: False,
            },
            ch_hash,
            carol.identity.hash_hex,
        )

        assert not bob.storage.message_exists(msg_id), \
            "a sync response for a live-only public channel was accepted"

    def test_public_channel_reports_live_sync_state(self, peer_factory):
        from trenchchat.core.sync_status import SyncState

        alice = peer_factory("alice")
        bob = peer_factory("bob")

        public_ch = alice.channel_mgr.create_channel("live-state", "", "public")
        assert alice.sync_mgr.status.get_state(public_ch) == SyncState.LIVE
        assert alice.sync_mgr.status.get_status(public_ch)["state"] == "live"

        invite_ch = mirrored_invite_channel("live-state-invite", alice, bob)
        assert alice.sync_mgr.status.get_state(invite_ch) != SyncState.LIVE
