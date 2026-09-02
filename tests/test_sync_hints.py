"""
Integration tests for missed-delivery hint durability.

The motivating scenario: four peers share a channel. A and B talk, then both
go offline. C comes online alone, sends a message, and goes offline. A and D
talk, and B returns and syncs from them, advancing B's watermark past C's
message. C finally returns. Nobody will ever *request* that message again --
every peer's sync window now starts after it. It is still recoverable,
because C recorded a local hint for each peer who missed it regardless of
whether the broadcast reached anyone, and the responder-side hint lookup in
_handle_sync_request ignores the requester's window_start entirely.

These tests pin down that recovery path and its edges: hint durability across
a restart, what happens when the sole holder's hints are lost, the horizon of
the sync window, relay chains at 4+ peers, and the responder never clearing a
hint once served.
"""

import time

import pytest

from tests.helpers import sign_as, wait_for, wait_for_message
from trenchchat.core.messaging import _compute_message_id
from trenchchat.core.protocol import (
    F_CHANNEL_HASH, F_MSG_TYPE, F_SYNC_MESSAGES, F_SYNC_WINDOW_START,
    MT_SYNC_REQUEST, message_id_from_wire, unpack_wire,
)
from trenchchat.core.sync import SYNC_WINDOW_SECS
from trenchchat.core.sync_status import SyncState


# ---------------------------------------------------------------------------
# Helpers (duplicated from tests/test_sync.py -- established pattern)
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
        author_sig=sign_as(sender_hex, ch_hash, msg_id, ts, content),
    )
    return msg_id


# ---------------------------------------------------------------------------
# B1 -- the motivating scenario, end to end
# ---------------------------------------------------------------------------

class TestSenderOnlyHintRecovery:
    def test_message_survives_when_the_broadcast_reaches_nobody(self, peer_factory):
        """
        Carol sends a message while Alice, Bob and Dave are all unreachable, so
        her missed-delivery broadcast lands on nobody -- only Carol's own local
        hints exist. Alice, Bob and Dave later advance their watermarks past
        Carol's message (simulating unrelated conversation elsewhere and a sync
        with each other). When each of them finally asks Carol for sync, she
        still serves it via her local hint, and none of their watermarks moves
        backward.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")
        dave = peer_factory("dave")

        ch_hash = alice.channel_mgr.create_channel("b1-motivating", "", "public")
        for peer in (bob, carol, dave):
            _seed_channel_on_peer(peer, ch_hash, "b1-motivating", alice.identity.hash_hex)

        ts = time.time()
        msg_id = _insert_message(carol.storage, ch_hash, carol.identity.hash_hex,
                                  "only Carol ever has this", ts)

        all_hex = [carol.identity.hash_hex, alice.identity.hash_hex,
                   bob.identity.hash_hex, dave.identity.hash_hex]

        original_send_raw = carol.sync_mgr._send_raw
        carol.sync_mgr._send_raw = lambda dest_hex, fields: False
        try:
            for missed in (alice, bob, dave):
                carol.sync_mgr._on_missed_delivery_event(
                    channel_hash_hex=ch_hash,
                    missed_peer_hex=missed.identity.hash_hex,
                    msg_id=msg_id,
                    subscriber_hashes=all_hex,
                )
        finally:
            carol.sync_mgr._send_raw = original_send_raw

        for missed in (alice, bob, dave):
            assert msg_id in carol.storage.get_missed_message_ids(
                ch_hash, missed.identity.hash_hex
            ), f"Carol did not record a local hint for {missed.name}"
            assert missed.storage.get_missed_message_ids(
                ch_hash, missed.identity.hash_hex
            ) == [], f"{missed.name} received a hint despite the broadcast reaching nobody"

        watermark = ts + 1000
        for peer in (alice, bob, dave):
            peer.storage.update_last_sync(ch_hash, watermark)

        for peer in (alice, bob, dave):
            peer.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, watermark)

        for peer in (alice, bob, dave):
            assert wait_for_message(peer.storage, ch_hash, msg_id, timeout=5), \
                f"{peer.name} never recovered Carol's message via the local hint"
            assert peer.storage.get_last_sync(ch_hash) == pytest.approx(watermark), \
                f"{peer.name}'s watermark moved after accepting a message older than it"


# ---------------------------------------------------------------------------
# B2 / B4 -- hint durability across a restart of the holder
# ---------------------------------------------------------------------------

class TestHintDurabilityAcrossRestart:
    def test_hint_survives_a_restart_of_the_holder(self, peer_factory):
        """
        Hints live in SQLite, so a restart of the holder (fresh Messaging /
        SyncManager / SubscriptionManager / Router over the same data directory,
        identity file and storage.db) must not lose them, and the startup purge
        must not sweep away a hint that is still well within the sync window.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = alice.channel_mgr.create_channel("b2-restart", "", "public")
        _seed_channel_on_peer(bob, ch_hash, "b2-restart", alice.identity.hash_hex)
        _seed_channel_on_peer(carol, ch_hash, "b2-restart", alice.identity.hash_hex)

        ts = time.time()
        msg_id = _insert_message(carol.storage, ch_hash, alice.identity.hash_hex,
                                  "held across a restart", ts)
        carol.storage.record_missed_delivery(ch_hash, bob.identity.hash_hex, msg_id)

        carol.teardown()
        carol2 = peer_factory("carol")

        assert msg_id in carol2.storage.get_missed_message_ids(
            ch_hash, bob.identity.hash_hex
        ), "the fresh hint did not survive the holder's restart"

        bob.sync_mgr._send_sync_request(carol2.identity.hash_hex, ch_hash, ts)

        assert wait_for_message(bob.storage, ch_hash, msg_id, timeout=5), \
            "the restarted holder did not serve the surviving hint"

    def test_hint_older_than_the_sync_window_is_purged_on_restart(self, peer_factory):
        """
        A hint's recovery guarantee is bounded at SYNC_WINDOW_SECS: one recorded
        long enough ago is purged by the startup sweep on the holder's next
        restart, and the message it named becomes unreachable through it.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = alice.channel_mgr.create_channel("b4-old-hint", "", "public")
        _seed_channel_on_peer(bob, ch_hash, "b4-old-hint", alice.identity.hash_hex)
        _seed_channel_on_peer(carol, ch_hash, "b4-old-hint", alice.identity.hash_hex)

        ts = time.time()
        msg_id = _insert_message(carol.storage, ch_hash, alice.identity.hash_hex,
                                  "hint older than the sync window", ts)
        carol.storage.record_missed_delivery(ch_hash, bob.identity.hash_hex, msg_id)

        old_recorded_at = time.time() - SYNC_WINDOW_SECS - 3600
        carol.storage._conn.execute(
            "UPDATE missed_deliveries SET recorded_at = ? "
            "WHERE channel_hash = ? AND recipient_hash = ? AND message_id = ?",
            (old_recorded_at, ch_hash, bob.identity.hash_hex, msg_id),
        )
        carol.storage._conn.commit()

        carol.teardown()
        carol2 = peer_factory("carol")

        assert carol2.storage.get_missed_message_ids(ch_hash, bob.identity.hash_hex) == [], \
            "a hint older than SYNC_WINDOW_SECS survived the restart purge"

        watermark = ts + 1000
        bob.storage.update_last_sync(ch_hash, watermark)
        bob.sync_mgr._send_sync_request(carol2.identity.hash_hex, ch_hash, watermark)
        time.sleep(0.5)

        assert not bob.storage.message_exists(msg_id), \
            "a message named only by a purged hint was still served"


# ---------------------------------------------------------------------------
# B3 -- hint holder's database is wiped
# ---------------------------------------------------------------------------

class TestHintHolderDataLoss:
    def test_message_is_genuinely_unrecoverable_once_the_sole_holder_loses_its_hints(
        self, peer_factory
    ):
        """
        The recovery guarantee only holds as long as some peer still has both the
        message and the hint naming it. If the sole holder's missed_deliveries
        rows are cleared and the requester's watermark is already past the
        message's timestamp, nothing -- no hint, no timestamp sweep -- can
        recover it. This test documents that real horizon: it is not a bug, it
        is the edge of what the mechanism promises.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = alice.channel_mgr.create_channel("b3-wiped", "", "public")
        _seed_channel_on_peer(bob, ch_hash, "b3-wiped", alice.identity.hash_hex)
        _seed_channel_on_peer(carol, ch_hash, "b3-wiped", alice.identity.hash_hex)

        ts = time.time()
        msg_id = _insert_message(carol.storage, ch_hash, carol.identity.hash_hex,
                                  "only Carol ever has this either", ts)
        carol.storage.record_missed_delivery(ch_hash, bob.identity.hash_hex, msg_id)
        carol.storage.clear_missed_deliveries(ch_hash, bob.identity.hash_hex)

        watermark = ts + 1000
        bob.storage.update_last_sync(ch_hash, watermark)
        bob.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, watermark)
        time.sleep(0.5)

        assert not bob.storage.message_exists(msg_id), \
            "a message was recovered despite its only hint being wiped"
        assert bob.storage.get_last_sync(ch_hash) == pytest.approx(watermark), \
            "the watermark moved even though nothing was actually received"


# ---------------------------------------------------------------------------
# B5 -- relay chain, only exists at 4+ peers
# ---------------------------------------------------------------------------

class TestRelayChain:
    def test_a_third_peer_relays_a_hint_neither_sender_nor_holder_can_still_answer(
        self, peer_factory
    ):
        """
        Alice fails to deliver to Bob while Carol and Dave are both online and
        both receive the message directly, plus the missed-delivery hint naming
        it for Bob. Alice and Carol then both become unreachable. Dave alone,
        who never talked to Bob or received anything special beyond the normal
        broadcast, can still serve Bob the message on his return.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")
        dave = peer_factory("dave")

        ch_hash = alice.channel_mgr.create_channel("b5-relay", "", "public")
        for peer in (bob, carol, dave):
            _seed_channel_on_peer(peer, ch_hash, "b5-relay", alice.identity.hash_hex)

        ts = time.time()
        content = "delivered to Carol and Dave, missed by Bob"
        msg_id = _insert_message(alice.storage, ch_hash, alice.identity.hash_hex, content, ts)
        # Model normal delivery succeeding to Carol and Dave: same deterministic
        # message_id (content+sender+ts), inserted directly into their storage.
        _insert_message(carol.storage, ch_hash, alice.identity.hash_hex, content, ts)
        _insert_message(dave.storage, ch_hash, alice.identity.hash_hex, content, ts)

        all_hex = [alice.identity.hash_hex, bob.identity.hash_hex,
                   carol.identity.hash_hex, dave.identity.hash_hex]
        alice.sync_mgr._on_missed_delivery_event(
            channel_hash_hex=ch_hash,
            missed_peer_hex=bob.identity.hash_hex,
            msg_id=msg_id,
            subscriber_hashes=all_hex,
        )

        assert wait_for(
            lambda: msg_id in carol.storage.get_missed_message_ids(
                ch_hash, bob.identity.hash_hex),
            timeout=5,
        ), "Carol never received the relayed hint"
        assert wait_for(
            lambda: msg_id in dave.storage.get_missed_message_ids(
                ch_hash, bob.identity.hash_hex),
            timeout=5,
        ), "Dave never received the relayed hint"

        alice.sync_mgr._send_raw = lambda dest_hex, fields: False
        carol.sync_mgr._send_raw = lambda dest_hex, fields: False

        bob.sync_mgr._send_sync_request(alice.identity.hash_hex, ch_hash, ts)
        bob.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, ts)
        time.sleep(0.5)
        assert not bob.storage.message_exists(msg_id), \
            "Bob received the message from a peer that should be unreachable"

        bob.sync_mgr._send_sync_request(dave.identity.hash_hex, ch_hash, ts)

        assert wait_for_message(bob.storage, ch_hash, msg_id, timeout=5), \
            "Dave, the only remaining relay holder, did not serve the message"


# ---------------------------------------------------------------------------
# B6 -- unresolvable hint at 4-peer scale must not shadow the sweep
# ---------------------------------------------------------------------------

class TestUnresolvableHintAtFourPeerScale:
    def test_every_holder_still_serves_its_own_sweep_history(self, peer_factory):
        """
        A hint reaches every reachable subscriber, so most holders of a given
        hint never actually have the message it names. A fix made one such
        unresolvable hint stop suppressing the timestamp sweep on a single
        responder; this extends the check to two independent responders (Carol
        and Dave) who each hold an unresolvable hint plus their own newer real
        history Bob lacks. Each must still serve its real history, and Bob's
        tracked sync status must not read synced while an answer is still
        outstanding from either of them.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")
        dave = peer_factory("dave")

        ch_hash = alice.channel_mgr.create_channel("b6-unresolvable", "", "public")
        for peer in (bob, carol, dave):
            _seed_channel_on_peer(peer, ch_hash, "b6-unresolvable", alice.identity.hash_hex)

        ts = time.time()
        carol.storage.record_missed_delivery(ch_hash, bob.identity.hash_hex, "de" * 32)
        dave.storage.record_missed_delivery(ch_hash, bob.identity.hash_hex, "ef" * 32)

        carol_new_id = _insert_message(carol.storage, ch_hash, alice.identity.hash_hex,
                                        "Carol's real history", ts + 1)
        dave_new_id = _insert_message(dave.storage, ch_hash, alice.identity.hash_hex,
                                       "Dave's real history", ts + 2)

        bob.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, ts)
        bob.sync_mgr._send_sync_request(dave.identity.hash_hex, ch_hash, ts)

        assert bob.sync_mgr.status.get_state(ch_hash) != SyncState.SYNCED, (
            "status read synced immediately after sending requests, before either "
            "peer had a chance to answer"
        )

        assert wait_for_message(bob.storage, ch_hash, carol_new_id, timeout=5), \
            "an unresolvable hint suppressed Carol's real history"
        assert wait_for_message(bob.storage, ch_hash, dave_new_id, timeout=5), \
            "an unresolvable hint suppressed Dave's real history"

        assert wait_for(
            lambda: bob.sync_mgr.status.get_state(ch_hash) == SyncState.SYNCED,
            timeout=5,
        ), "status never settled to synced once both peers answered with real history"


# ---------------------------------------------------------------------------
# B7 -- the responder never clears a hint once served
# ---------------------------------------------------------------------------

class TestResponderSideHintClearedOnceServed:
    def test_hint_is_served_once_then_retired_without_shadowing_newer_history(
        self, peer_factory
    ):
        """
        A hint that has been served has done its job, so the responder retires
        it. The recipient only ever clears hints naming itself, and a hint
        naming a peer is never broadcast to that peer, so nothing else would
        ever clear this row before the sync window purged it.

        The safety properties the repeated-serve behaviour used to rely on
        still hold: newer history rides alongside the hint, insert_message
        dedupes, and the watermark never regresses.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = alice.channel_mgr.create_channel("b7-repeat-hint", "", "public")
        _seed_channel_on_peer(bob, ch_hash, "b7-repeat-hint", alice.identity.hash_hex)
        _seed_channel_on_peer(carol, ch_hash, "b7-repeat-hint", alice.identity.hash_hex)

        ts = time.time()
        hinted_id = _insert_message(carol.storage, ch_hash, alice.identity.hash_hex,
                                     "hinted, never cleared on the responder", ts)
        carol.storage.record_missed_delivery(ch_hash, bob.identity.hash_hex, hinted_id)

        responses = []
        original_send_raw = carol.sync_mgr._send_raw

        def capture(dest_hex, fields):
            responses.append(fields)
            return True

        carol.sync_mgr._send_raw = capture

        # Past the hinted message, so only the never-cleared hint can serve it.
        request_fields = {
            F_MSG_TYPE:          MT_SYNC_REQUEST,
            F_CHANNEL_HASH:      bytes.fromhex(ch_hash),
            F_SYNC_WINDOW_START: ts + 1,
        }

        carol.sync_mgr._handle_sync_request(request_fields, ch_hash, bob.identity.hash_hex)
        first_ids = {message_id_from_wire(m["message_id"])
                     for m in unpack_wire(responses[0][F_SYNC_MESSAGES])}
        assert hinted_id in first_ids, "the hint was not served at all"
        assert carol.storage.get_missed_message_ids(ch_hash, bob.identity.hash_hex) == [], \
            "the hint was still held after being served"

        carol.sync_mgr._handle_sync_request(request_fields, ch_hash, bob.identity.hash_hex)
        second_ids = {message_id_from_wire(m["message_id"])
                      for m in unpack_wire(responses[1][F_SYNC_MESSAGES])}
        assert hinted_id not in second_ids, "a retired hint was served again"

        # Newer history is unaffected by the hint having been retired.
        newer_id = _insert_message(carol.storage, ch_hash, alice.identity.hash_hex,
                                    "newer history, served on its own merits", ts + 2)
        carol.sync_mgr._handle_sync_request(request_fields, ch_hash, bob.identity.hash_hex)
        last_ids = {message_id_from_wire(m["message_id"])
                    for m in unpack_wire(responses[-1][F_SYNC_MESSAGES])}
        assert newer_id in last_ids, "newer history stopped being served"

        # Drive the real path: Bob must still end up with both messages exactly
        # once, and his watermark must not regress over the older hinted one.
        carol.sync_mgr._send_raw = original_send_raw
        carol.storage.record_missed_delivery(ch_hash, bob.identity.hash_hex, hinted_id)

        bob.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, ts + 1)
        assert wait_for_message(bob.storage, ch_hash, hinted_id, timeout=5)
        assert wait_for_message(bob.storage, ch_hash, newer_id, timeout=5)
        first_watermark = bob.storage.get_last_sync(ch_hash)

        bob.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, ts + 1)
        time.sleep(0.5)

        rows = [m for m in bob.storage.get_messages(ch_hash) if m["message_id"] == hinted_id]
        assert len(rows) == 1, "the hinted message created a duplicate row"
        assert bob.storage.get_last_sync(ch_hash) >= first_watermark, \
            "watermark regressed after re-receiving an already-held hinted message"
