"""
Integration tests for sync behavior across restarts, and for watermark and
continuation continuity under adverse conditions.

A restart is modeled by tearing a TestPeer down and rebuilding fresh
managers over the same on-disk identity file and storage.db via a second
peer_factory() call for the same name -- see _restart_peer() below.
Everything SyncManager, Messaging and SubscriptionManager hold in memory is
lost on restart; everything Storage persisted to SQLite survives.

Sync only serves invite-only channels (public channels are live-only), so
these tests run on one, built with helpers.mirrored_invite_channel -- the
seeded channel, subscription and member rows live in the peer's SQLite DB
and therefore survive a restart.
"""

import time

import msgpack

from tests.helpers import (
    mirrored_invite_channel, sign_as, wait_for, wait_for_message,
)
from trenchchat.core.messaging import _compute_message_id
from trenchchat.core.permissions import PRESET_PRIVATE, ROLE_MEMBER, ROLE_OWNER, SEND_MESSAGE
from trenchchat.core.protocol import (
    F_CHANNEL_HASH, F_MSG_TYPE, F_SYNC_MESSAGES, F_SYNC_TRUNCATED,
    MT_SYNC_RESPONSE,
)
from trenchchat.core.sync import (
    MAX_RESPONSE_MESSAGES, MAX_SWEEP_SCAN, MAX_SYNC_CONTINUATIONS, SYNC_WINDOW_SECS,
)
from trenchchat.core.sync_status import SyncState


# ---------------------------------------------------------------------------
# Helpers (copied from tests/test_sync.py -- established pattern)
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
# Restart helpers
# ---------------------------------------------------------------------------

def _restart_peer(peer_factory, peer):
    """Simulate a process restart: tear the peer down, then rebuild it fresh
    over the same on-disk identity file and storage.db via a second
    peer_factory() call for the same name (conftest.py's make_peer() does
    peer_dir.mkdir(..., exist_ok=True), so the directory is happily reused).

    peer.teardown() (conftest.py) fully deregisters everything the peer
    registered with RNS.Transport, so the identity is free for a fresh
    peer_factory() call under the same name.
    """
    peer.teardown()
    return peer_factory(peer.name)


# ---------------------------------------------------------------------------
# C1 -- restart mid-backfill
# ---------------------------------------------------------------------------

class TestRestartMidBackfill:
    def test_inflight_continuation_dropped_but_fresh_sync_recovers_full_history(
        self, peer_factory
    ):
        """A truncated first batch has landed and its continuation request is
        outstanding when the requester restarts. _pending_requests and
        _continuations are in-memory only, so the continuation's eventual
        answer is dropped as unsolicited -- but nothing is permanently lost:
        the watermark from the first batch is already on disk, and a fresh
        sync request recovers the rest.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = mirrored_invite_channel("restart-midsync", alice, bob, carol)

        window_start = time.time()
        total = MAX_RESPONSE_MESSAGES + 10
        msg_ids = []
        for i in range(total):
            ts = window_start + i + 1
            msg_ids.append(_insert_message(carol.storage, ch_hash,
                                           alice.identity.hash_hex, f"m{i}", ts))

        # Hold Carol's continuation (second) response so the restart happens
        # deterministically before it lands -- otherwise the async 0.05s
        # TestTransport delivery races the restart.
        held = []
        responses_sent = 0
        original_send_raw = carol.sync_mgr._send_raw

        def gate(dest_hex, fields):
            nonlocal responses_sent
            if fields.get(F_MSG_TYPE) == MT_SYNC_RESPONSE:
                responses_sent += 1
                if responses_sent == 2:
                    held.append((dest_hex, fields))
                    return True
            return original_send_raw(dest_hex, fields)

        carol.sync_mgr._send_raw = gate

        bob.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, window_start)

        assert wait_for(lambda: len(held) == 1, timeout=5), \
            "Carol's continuation response was never held"
        assert len(bob.storage.get_messages(ch_hash)) == MAX_RESPONSE_MESSAGES

        bob = _restart_peer(peer_factory, bob)

        # Deliver the held response now, through the real path (Carol's own
        # _send_raw), to a Bob whose SyncManager never issued the request it
        # claims to answer.
        dest_hex, fields = held[0]
        original_send_raw(dest_hex, fields)
        time.sleep(0.5)
        assert len(bob.storage.get_messages(ch_hash)) == MAX_RESPONSE_MESSAGES, (
            "the pre-restart continuation response was applied after restart "
            "even though the fresh SyncManager's _pending_requests was empty"
        )

        # Something re-triggering sync (an announce, a hint, a manual retry)
        # recovers the rest, because the watermark from batch 1 survived on
        # disk and the continuation budget reset clean.
        resume_ts = bob.storage.get_last_sync(ch_hash)
        bob.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, resume_ts)
        assert wait_for(
            lambda: len(bob.storage.get_messages(ch_hash)) == total, timeout=10
        ), "Bob never recovered the rest of the backlog after a fresh post-restart sync"


# ---------------------------------------------------------------------------
# C2 -- restart loses Messaging._pending
# ---------------------------------------------------------------------------

class TestRestartLosesPendingQueue:
    def test_pending_queue_lost_but_recovered_via_sync_from_a_third_holder(
        self, peer_factory
    ):
        """Alice restarts with a message queued for offline Bob. The queue is
        gone, but Carol also holds a copy and can serve it to Bob via sync --
        the designed safety net.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = mirrored_invite_channel("restart-pending-queue", alice, bob, carol)

        ts = time.time()
        content = "queued for offline Bob"
        msg_id = _compute_message_id(content, alice.identity.hash_hex, ts)

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
        alice.storage.insert_message(
            channel_hash=ch_hash, sender_hash=alice.identity.hash_hex,
            sender_name="Alice", content=content, timestamp=ts, message_id=msg_id,
            reply_to=None, last_seen_id=None, received_at=ts,
            author_sig=sign_as(alice.identity.hash_hex, ch_hash, msg_id, ts, content),
        )
        # Carol was online and already has her own copy.
        carol.storage.insert_message(
            channel_hash=ch_hash, sender_hash=alice.identity.hash_hex,
            sender_name="Alice", content=content, timestamp=ts, message_id=msg_id,
            reply_to=None, last_seen_id=None, received_at=ts,
            author_sig=sign_as(alice.identity.hash_hex, ch_hash, msg_id, ts, content),
        )

        alice = _restart_peer(peer_factory, alice)
        assert alice.messaging._pending == {}

        bob.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, ts - 1)
        assert wait_for_message(bob.storage, ch_hash, msg_id, timeout=5), (
            "Bob never recovered the message via sync from Carol after "
            "Alice's restart wiped her pending-retry queue"
        )

    def test_pending_queue_lost_and_only_the_original_sender_can_still_serve_it(
        self, peer_factory
    ):
        """When no other peer holds a copy, losing the pending queue means
        nothing is delivered automatically -- but the message is not gone:
        it still lives in the sender's own storage and can be recovered by a
        direct sync pull from the sender.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = mirrored_invite_channel("restart-pending-sole-holder", alice, bob)

        ts = time.time()
        content = "only Alice has this"
        msg_id = _compute_message_id(content, alice.identity.hash_hex, ts)

        alice.messaging._pending[bob.identity.hash_hex] = [{
            "channel_hash_hex":  ch_hash,
            "content":           content,
            "timestamp":         ts,
            "msg_id":            msg_id,
            "display_name":      "Alice",
            "reply_to":          None,
            "last_seen_id":      None,
            "subscriber_hashes": [bob.identity.hash_hex],
        }]
        alice.storage.insert_message(
            channel_hash=ch_hash, sender_hash=alice.identity.hash_hex,
            sender_name="Alice", content=content, timestamp=ts, message_id=msg_id,
            reply_to=None, last_seen_id=None, received_at=ts,
            author_sig=sign_as(alice.identity.hash_hex, ch_hash, msg_id, ts, content),
        )

        alice = _restart_peer(peer_factory, alice)

        # Nothing pushes it automatically: the queue is gone and nothing else
        # triggers delivery without an explicit sync request.
        time.sleep(0.5)
        assert not bob.storage.message_exists(msg_id), (
            "the message should not arrive without an explicit sync pull"
        )

        # But it is recoverable: Alice's own storage still has it.
        bob.sync_mgr._send_sync_request(alice.identity.hash_hex, ch_hash, ts - 1)
        assert wait_for_message(bob.storage, ch_hash, msg_id, timeout=5), (
            "the message should still be recoverable via a direct sync pull "
            "from Alice's own storage, even with no other holder and no "
            "pending-retry queue"
        )


# ---------------------------------------------------------------------------
# C3 -- restart must not lose peer discovery
# ---------------------------------------------------------------------------

class TestRestartKeepsPeerDiscovery:
    def test_restart_keeps_channel_peer_discovery(self, peer_factory):
        """_get_channel_peers reads the members table, which is persisted, so
        Bob must still find Carol as a fellow member after a restart.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = mirrored_invite_channel("restart-peer-discovery", alice, bob, carol)

        assert carol.identity.hash_hex in bob.sync_mgr._get_channel_peers(ch_hash)

        bob = _restart_peer(peer_factory, bob)

        peers_after = bob.sync_mgr._get_channel_peers(ch_hash)
        assert carol.identity.hash_hex in peers_after, (
            "Carol dropped out of _get_channel_peers after Bob's restart -- "
            f"peers_after={peers_after}; member rows live in SQLite and must "
            "survive a restart"
        )

    def test_startup_sync_reaches_a_fellow_member_after_restart(self, peer_factory):
        """End-to-end consequence: request_sync_all() at startup must be able
        to recover a message that only a fellow (non-creator) member holds,
        even after a restart wiped everything held in memory.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = mirrored_invite_channel("restart-startup-sync", alice, bob, carol)

        msg_id = _insert_message(carol.storage, ch_hash, carol.identity.hash_hex,
                                 "carol only has this", time.time())

        bob = _restart_peer(peer_factory, bob)
        bob.sync_mgr.request_sync_all()

        assert wait_for_message(bob.storage, ch_hash, msg_id, timeout=5), (
            "Bob never recovered Carol's message after restart: "
            "request_sync_all() must discover Carol from the persisted "
            "members table, not from any in-memory state"
        )


# ---------------------------------------------------------------------------
# C4 -- restart resets the deep-sync cooldown
# ---------------------------------------------------------------------------

class TestRestartResetsDeepSyncCooldown:
    def test_restart_resets_cooldown_allowing_repeated_full_sweeps(self, peer_factory):
        """_deep_sync_last_served is in-memory only (sync.py documents this as
        an accepted soft-limit tradeoff). Restarting the responder forgets
        any cooldown it was enforcing against a given requester, so a
        restart-looping responder can be driven into repeated full sweeps by
        the same requester.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = mirrored_invite_channel("restart-deep-cooldown", alice, bob)

        old_ts = time.time() - SYNC_WINDOW_SECS - 3600
        # Tenure opened before the fabricated history: Storage's startup
        # tenure backfill would otherwise stamp joined_at=now on restart and
        # withhold every ancient message from Bob.
        for member in (alice, bob):
            alice.storage.open_tenure(ch_hash, member.identity.hash_hex, old_ts - 100)
        first_id = _insert_message(alice.storage, ch_hash, alice.identity.hash_hex,
                                   "first ancient", old_ts)

        bob.sync_mgr._send_sync_request(alice.identity.hash_hex, ch_hash, old_ts - 10)
        assert wait_for_message(bob.storage, ch_hash, first_id, timeout=5)

        second_id = _insert_message(alice.storage, ch_hash, alice.identity.hash_hex,
                                    "second ancient", old_ts + 1)
        bob.sync_mgr._send_sync_request(alice.identity.hash_hex, ch_hash, old_ts - 10)
        time.sleep(0.5)
        assert not bob.storage.message_exists(second_id), \
            "cooldown should still be active pre-restart"

        alice = _restart_peer(peer_factory, alice)

        bob.sync_mgr._send_sync_request(alice.identity.hash_hex, ch_hash, old_ts - 10)
        assert wait_for_message(bob.storage, ch_hash, second_id, timeout=5), (
            "restarting the responder should reset the deep-sync cooldown, "
            "but the repeated deep sweep still appears throttled"
        )


# ---------------------------------------------------------------------------
# C5 -- restart loses SyncStatusTracker state
# ---------------------------------------------------------------------------

class TestRestartLosesSyncStatus:
    def test_restart_forgets_known_gap_but_the_hint_survives_in_storage(self, peer_factory):
        """SyncStatusTracker keeps no persistent state. After a restart the
        channel reports UNKNOWN even though a missed_deliveries row naming us
        still exists in SQLite, and nothing re-derives the gap from it.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = mirrored_invite_channel("restart-status-gap", alice, bob)

        missed_id = "ab" * 32
        bob.storage.record_missed_delivery(ch_hash, bob.identity.hash_hex, missed_id)
        bob.sync_mgr._status.note_gap(ch_hash)

        assert bob.sync_mgr.status.get_state(ch_hash) == SyncState.INCOMPLETE

        bob = _restart_peer(peer_factory, bob)

        assert bob.sync_mgr.status.get_state(ch_hash) == SyncState.UNKNOWN, (
            "the channel's known gap should be forgotten after a restart -- "
            "SyncStatusTracker keeps no persistent state"
        )
        assert missed_id in bob.storage.get_missed_message_ids(ch_hash, bob.identity.hash_hex), (
            "the missed-delivery hint row should still survive in SQLite "
            "across the restart even though status forgot about it"
        )


# ---------------------------------------------------------------------------
# C6 -- _params_by_id eviction cap
# ---------------------------------------------------------------------------

class TestParamsByIdEvictionCap:
    def test_evicted_params_prevent_requeue_but_hint_still_broadcasts(self, peer_factory):
        """Past the 200-entry cap (messaging.py:117-119) the oldest send
        params are evicted, so _on_delivery_failed can no longer re-queue
        that message for retry -- but it still broadcasts a missed-delivery
        hint, since that half of the failure path doesn't depend on params.
        """
        alice = peer_factory("alice")

        ch_hash = mirrored_invite_channel("params-cap", alice)

        alice.messaging.send_message(
            channel_hash_hex=ch_hash, content="filler 0",
            subscriber_hashes=[alice.identity.hash_hex],
        )
        first_msg_id = alice.storage.get_messages(ch_hash, limit=1)[0]["message_id"]
        assert first_msg_id in alice.messaging._params_by_id

        for i in range(1, 201):
            alice.messaging.send_message(
                channel_hash_hex=ch_hash, content=f"filler {i}",
                subscriber_hashes=[alice.identity.hash_hex],
            )

        assert len(alice.messaging._params_by_id) == 200
        assert first_msg_id not in alice.messaging._params_by_id, (
            "the 201st send should have evicted the oldest message's params"
        )

        fake_dest = "de" * 16
        alice.messaging._on_delivery_failed(
            fake_dest, ch_hash, first_msg_id, [alice.identity.hash_hex, fake_dest]
        )

        assert fake_dest not in alice.messaging._pending, (
            "a delivery failure for an evicted message_id must not be able "
            "to re-queue anything, since its params are gone"
        )
        assert first_msg_id in alice.storage.get_missed_message_ids(ch_hash, fake_dest), (
            "the missed-delivery hint should still be recorded even though "
            "re-queue was impossible"
        )


# ---------------------------------------------------------------------------
# D1 -- page-boundary timestamp collision (expected FAIL)
# ---------------------------------------------------------------------------

class TestPageBoundaryTimestampCollision:
    def test_tied_timestamps_at_a_page_boundary_are_not_lost(self, peer_factory):
        """_collect_permitted_rows advances cursor = page[-1]["timestamp"],
        while Storage.get_messages_after filters on strict timestamp >
        since_ts. When several messages share the exact timestamp that lands
        on a page boundary, every one after the first sharing that timestamp
        is permanently skipped by the next request.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = mirrored_invite_channel("page-boundary-tie", alice, bob, carol)

        window_start = time.time()
        msg_ids = []

        ts = window_start
        for i in range(MAX_RESPONSE_MESSAGES - 1):
            ts += 1
            msg_ids.append(_insert_message(carol.storage, ch_hash,
                                           alice.identity.hash_hex, f"distinct {i}", ts))

        tie_ts = ts + 1
        for i in range(3):
            msg_ids.append(_insert_message(carol.storage, ch_hash,
                                           alice.identity.hash_hex, f"tied {i}", tie_ts))

        after_ts = tie_ts
        for i in range(2):
            after_ts += 1
            msg_ids.append(_insert_message(carol.storage, ch_hash,
                                           alice.identity.hash_hex, f"after {i}", after_ts))

        bob.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, window_start)

        assert wait_for(
            lambda: len(bob.storage.get_messages(ch_hash)) >= len(msg_ids) - 2, timeout=10
        ), "sync chain did not even complete as far as expected"
        time.sleep(1.0)  # let any further continuation settle

        for mid in msg_ids:
            assert bob.storage.message_exists(mid), (
                f"message {mid[:12]}… was permanently lost at a "
                "page-boundary timestamp collision (sync.py's "
                "_collect_permitted_rows advances the cursor with strict "
                "inequality against Storage.get_messages_after)"
            )


# ---------------------------------------------------------------------------
# D2 -- MAX_SWEEP_SCAN exhaustion
# ---------------------------------------------------------------------------

class TestSweepScanCap:
    def test_sweep_scan_cap_eventually_reaches_permitted_history(self, peer_factory):
        """More than MAX_SWEEP_SCAN consecutive tenure-withheld rows sit in
        front of history the requester is entitled to. The requester must
        still eventually reach that history across continuations rather than
        stalling.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        perms = dict(PRESET_PRIVATE)
        perms[ROLE_MEMBER] = [SEND_MESSAGE]
        ch_hash = alice.channel_mgr.create_channel("sweep-scan-cap", "", permissions=perms)

        for peer in (bob, carol):
            peer.storage.upsert_channel(ch_hash, "sweep-scan-cap", "",
                                        alice.identity.hash_hex, perms, time.time())
            peer.storage.subscribe(ch_hash)

        join_ts = time.time() - 100000
        bob_join_ts = join_ts + 90000  # well after all withheld rows

        for peer in (bob, carol):
            peer.storage.open_tenure(ch_hash, alice.identity.hash_hex, join_ts)
            peer.storage.open_tenure(ch_hash, bob.identity.hash_hex, bob_join_ts)
            peer.storage.upsert_member(ch_hash, alice.identity.hash_hex, "Alice",
                                       role=ROLE_OWNER)
            peer.storage.upsert_member(ch_hash, bob.identity.hash_hex, "Bob",
                                       role=ROLE_MEMBER)
            peer.storage.set_channel_permissions(ch_hash, perms)

        withheld_count = MAX_SWEEP_SCAN + 1
        ts = join_ts
        for i in range(withheld_count):
            ts += 1
            _insert_message(carol.storage, ch_hash, alice.identity.hash_hex,
                            f"withheld {i}", ts)

        ts = bob_join_ts + 1
        visible_ids = []
        for i in range(5):
            ts += 1
            visible_ids.append(_insert_message(carol.storage, ch_hash,
                                               alice.identity.hash_hex, f"visible {i}", ts))

        bob.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, join_ts - 1)

        for mid in visible_ids:
            assert wait_for_message(bob.storage, ch_hash, mid, timeout=15), (
                f"Bob never received {mid[:12]}… -- with over "
                "MAX_SWEEP_SCAN withheld rows ahead of it, a single sweep "
                "returns zero messages with F_SYNC_TRUNCATED set, but "
                "_handle_sync_response only chains a continuation when "
                "newest_ts advances past requested_since (sync.py:606), and "
                "an all-withheld response never advances newest_ts -- so the "
                "chain never continues"
            )


# ---------------------------------------------------------------------------
# D3 -- continuation budget exhausted mid-backfill
# ---------------------------------------------------------------------------

class TestContinuationBudgetExhaustion:
    def test_status_reports_incomplete_when_budget_exhausts_mid_backfill(self, peer_factory):
        """A responder marking every batch truncated can drive the requester's
        continuation chain to MAX_SYNC_CONTINUATIONS with history still
        outstanding; the channel status must honestly report INCOMPLETE, and
        stays there with no automatic retry.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = mirrored_invite_channel("budget-status", alice, bob, carol)

        bob.sync_mgr._send_raw = lambda dest_hex, fields: True

        ts = time.time()
        bob.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, ts)
        for i in range(MAX_SYNC_CONTINUATIONS + 5):
            ts += 10
            bob.sync_mgr._handle_sync_response(
                {
                    F_MSG_TYPE:       MT_SYNC_RESPONSE,
                    F_CHANNEL_HASH:   bytes.fromhex(ch_hash),
                    F_SYNC_MESSAGES:  msgpack.packb([{
                        "sender_hash":  alice.identity.hash_hex,
                        "sender_name":  "Alice",
                        "content":      f"chain {i}",
                        "timestamp":    ts,
                        "message_id":   f"budget-status-{i}",
                        "reply_to":     None,
                        "last_seen_id": None,
                    }], use_bin_type=True),
                    F_SYNC_TRUNCATED: True,
                },
                ch_hash,
                carol.identity.hash_hex,
            )

        assert bob.sync_mgr.status.get_state(ch_hash) == SyncState.INCOMPLETE, (
            "the channel should report INCOMPLETE once the continuation "
            "budget is exhausted with history still outstanding"
        )

        time.sleep(0.5)
        assert bob.sync_mgr.status.get_state(ch_hash) == SyncState.INCOMPLETE, (
            "the channel is stuck INCOMPLETE with no automatic retry once "
            "the continuation budget is spent"
        )


# ---------------------------------------------------------------------------
# D4 -- stray on_peer_appeared resets the continuation budget
# ---------------------------------------------------------------------------

class TestContinuationBudgetResetByStrayRequest:
    def test_stray_peer_appeared_mid_chain_resets_the_budget(self, peer_factory):
        """A stray on_peer_appeared mid-chain sends a plain, non-continuation
        request, which resets _continuations for that (channel, peer) pair
        (sync.py's _send_sync_request, the `if not continuation:` branch),
        re-arming the full budget -- a soundness hole in the bound.
        """
        carol = peer_factory("carol")  # channel creator/responder
        bob = peer_factory("bob")

        ch_hash = mirrored_invite_channel("continuation-reset", carol, bob)

        requests = []

        def counting_send_raw(dest_hex, fields):
            requests.append(fields.get(F_MSG_TYPE))
            return True

        bob.sync_mgr._send_raw = counting_send_raw

        def _synthetic_response(content, ts):
            msg_id = _compute_message_id(content, carol.identity.hash_hex, ts)
            return {
                F_MSG_TYPE:       MT_SYNC_RESPONSE,
                F_CHANNEL_HASH:   bytes.fromhex(ch_hash),
                F_SYNC_MESSAGES:  msgpack.packb([{
                    "sender_hash":  carol.identity.hash_hex,
                    "sender_name":  "Carol",
                    "content":      content,
                    "timestamp":    ts,
                    "message_id":   msg_id,
                    "reply_to":     None,
                    "last_seen_id": None,
                    "author_sig":   sign_as(carol.identity.hash_hex, ch_hash,
                                            msg_id, ts, content),
                }], use_bin_type=True),
                F_SYNC_TRUNCATED: True,
            }

        ts = time.time()
        bob.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, ts)
        for i in range(MAX_SYNC_CONTINUATIONS):
            ts += 10
            bob.sync_mgr._handle_sync_response(
                _synthetic_response(f"chain-{i}", ts), ch_hash, carol.identity.hash_hex
            )

        assert requests.count("sync_request") - 1 == MAX_SYNC_CONTINUATIONS

        # Budget already spent: one more truncated response chains nothing.
        ts += 10
        bob.sync_mgr._handle_sync_response(
            _synthetic_response("chain-over-budget", ts), ch_hash, carol.identity.hash_hex
        )
        assert requests.count("sync_request") - 1 == MAX_SYNC_CONTINUATIONS, \
            "budget should already be exhausted before the stray announce"

        # A stray peer-appeared event (e.g. Carol re-announcing) fires a
        # fresh, non-continuation request.
        bob.sync_mgr.on_peer_appeared(carol.identity.hash_hex)
        assert wait_for(
            lambda: requests.count("sync_request") - 1 == MAX_SYNC_CONTINUATIONS + 1,
            timeout=5,
        ), "on_peer_appeared should have sent a fresh sync request"

        # The reset budget lets another continuation go out.
        ts += 10
        bob.sync_mgr._handle_sync_response(
            _synthetic_response("chain-after-reset", ts), ch_hash, carol.identity.hash_hex
        )
        assert requests.count("sync_request") - 1 == MAX_SYNC_CONTINUATIONS + 2, (
            "on_peer_appeared's non-continuation request should have reset "
            "_continuations for (channel, peer), letting the budget be spent "
            "all over again"
        )


# ---------------------------------------------------------------------------
# D5 -- malformed F_SYNC_MESSAGES leaves the peer PENDING forever (expected FAIL)
# ---------------------------------------------------------------------------

class TestMalformedSyncResponseLeavesPeerPending:
    def test_absent_sync_messages_field_leaves_peer_pending(self, peer_factory):
        """A response with no F_SYNC_MESSAGES field consumes the pending-
        request claim and returns early, before response_received() is ever
        called -- the peer stays PENDING and the channel stays SYNCING.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = mirrored_invite_channel("malformed-absent", alice, bob, carol)

        bob.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, time.time())
        assert bob.sync_mgr.status.get_state(ch_hash) == SyncState.SYNCING

        bob.sync_mgr._handle_sync_response(
            {F_MSG_TYPE: MT_SYNC_RESPONSE, F_CHANNEL_HASH: bytes.fromhex(ch_hash)},
            ch_hash, carol.identity.hash_hex,
        )

        assert bob.sync_mgr.status.get_state(ch_hash) != SyncState.SYNCING, (
            "a response missing F_SYNC_MESSAGES consumed the pending-request "
            "claim (sync.py's _claim_pending_request) and returned early "
            "(sync.py:493-495) before response_received() was ever called, "
            "leaving the peer stuck PENDING with nothing actually outstanding"
        )

    def test_unparseable_sync_messages_leaves_peer_pending(self, peer_factory):
        """Same failure mode, triggered by payload bytes that don't unpack."""
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = mirrored_invite_channel("malformed-badpack", alice, bob, carol)

        bob.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, time.time())
        assert bob.sync_mgr.status.get_state(ch_hash) == SyncState.SYNCING

        bob.sync_mgr._handle_sync_response(
            {
                F_MSG_TYPE:      MT_SYNC_RESPONSE,
                F_CHANNEL_HASH:  bytes.fromhex(ch_hash),
                F_SYNC_MESSAGES: b"\xff\xfe\x00not msgpack",
            },
            ch_hash, carol.identity.hash_hex,
        )

        assert bob.sync_mgr.status.get_state(ch_hash) != SyncState.SYNCING, (
            "a response whose F_SYNC_MESSAGES doesn't unpack (sync.py:496-500) "
            "consumed the claim and returned early, before response_received() "
            "was ever called, leaving the peer stuck PENDING"
        )

    def test_non_list_sync_messages_leaves_peer_pending(self, peer_factory):
        """Same failure mode, triggered by a payload that unpacks to a dict
        instead of a list.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = mirrored_invite_channel("malformed-notalist", alice, bob, carol)

        bob.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, time.time())
        assert bob.sync_mgr.status.get_state(ch_hash) == SyncState.SYNCING

        bob.sync_mgr._handle_sync_response(
            {
                F_MSG_TYPE:      MT_SYNC_RESPONSE,
                F_CHANNEL_HASH:  bytes.fromhex(ch_hash),
                F_SYNC_MESSAGES: msgpack.packb({"not": "a list"}, use_bin_type=True),
            },
            ch_hash, carol.identity.hash_hex,
        )

        assert bob.sync_mgr.status.get_state(ch_hash) != SyncState.SYNCING, (
            "a response whose F_SYNC_MESSAGES unpacks to something other "
            "than a list (sync.py:501-503) consumed the claim and returned "
            "early, before response_received() was ever called, leaving the "
            "peer stuck PENDING"
        )


# ---------------------------------------------------------------------------
# D6 -- watermark advances past a message that failed to insert (expected FAIL)
# ---------------------------------------------------------------------------

class TestWatermarkAdvancesPastFailedInsert:
    def test_insert_failure_does_not_advance_watermark_past_the_failed_message(
        self, peer_factory
    ):
        """_handle_sync_response computes newest_ts = max(newest_ts, msg_ts)
        before calling insert_message, and the insert is wrapped in a
        try/except that only logs. A later message in the same batch that
        DOES insert successfully still pulls the watermark past the one that
        failed, permanently hiding it from every future sync (strict
        timestamp > filtering in Storage.get_messages_after).
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = mirrored_invite_channel("watermark-insert-fail", alice, bob, carol)

        ts = time.time()
        ok_id_1 = _compute_message_id("ok 1", alice.identity.hash_hex, ts + 1)
        boom_id = _compute_message_id("boom", alice.identity.hash_hex, ts + 2)
        ok_id_2 = _compute_message_id("ok 2", alice.identity.hash_hex, ts + 3)

        original_insert = bob.storage.insert_message

        def failing_insert(**kwargs):
            if kwargs.get("message_id") == boom_id:
                raise RuntimeError("simulated storage failure")
            return original_insert(**kwargs)

        bob.storage.insert_message = failing_insert

        bob.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, ts)

        def _row(content, offset, mid):
            return {"sender_hash": alice.identity.hash_hex, "sender_name": "Alice",
                    "content": content, "timestamp": ts + offset,
                    "message_id": mid, "reply_to": None, "last_seen_id": None,
                    "author_sig": sign_as(alice.identity.hash_hex, ch_hash, mid,
                                          ts + offset, content)}

        payload = msgpack.packb([
            _row("ok 1", 1, ok_id_1),
            _row("boom", 2, boom_id),
            _row("ok 2", 3, ok_id_2),
        ], use_bin_type=True)

        bob.sync_mgr._handle_sync_response(
            {
                F_MSG_TYPE:       MT_SYNC_RESPONSE,
                F_CHANNEL_HASH:   bytes.fromhex(ch_hash),
                F_SYNC_MESSAGES:  payload,
                F_SYNC_TRUNCATED: False,
            },
            ch_hash, carol.identity.hash_hex,
        )

        assert bob.storage.message_exists(ok_id_1)
        assert not bob.storage.message_exists(boom_id)
        assert bob.storage.message_exists(ok_id_2)

        watermark = bob.storage.get_last_sync(ch_hash)
        assert watermark <= ts + 2, (
            f"watermark advanced to {watermark} (past the failed insert at "
            f"ts={ts + 2}) even though message {boom_id[:12]}… was never "
            "stored -- a later successful insert (ts+3) still pulled the "
            "watermark past the one that failed"
        )
