"""
Integration tests for sync status honesty and transport/identity edge cases.

Group F targets the class of bug that motivated this file: a channel
reporting "up to date" in the UI while messages are silently missing.
Group G targets transport/identity edges in the pending-request ledger,
router quarantine, and the missed-delivery hint path.
"""

import time

import msgpack
import pytest
import RNS
import LXMF

from tests.helpers import sign_as, delivery_dest_hash_hex, wait_for, wait_for_message
from trenchchat.core import sync_status
from trenchchat.core.image import MAX_IMAGE_BYTES
from trenchchat.core.messaging import _compute_message_id
from trenchchat.core.protocol import (
    F_CHANNEL_HASH, F_MISSED_FOR, F_MISSED_MSG_ID, F_MSG_TYPE,
    F_SYNC_MESSAGES, F_SYNC_TRUNCATED,
    MT_MISSED_DELIVERY, MT_SYNC_RESPONSE,
)
from trenchchat.core.sync_status import SyncState


# ---------------------------------------------------------------------------
# Helpers (duplicated from tests/test_sync.py per the established pattern)
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


def _sync_response_fields(messages: list[dict], truncated: bool = False,
                          channel_hash_hex: str | None = None) -> dict:
    """Build a sync response, signing rows that don't already carry one.

    A real responder relays rows their author signed. A test row assembled by
    hand has no signature, so unless one is supplied deliberately (to exercise
    rejection) it is signed here as the peer it claims to come from.
    """
    signed = []
    for m in messages:
        row = dict(m)
        if "author_sig" not in row:
            ch = channel_hash_hex or row.get("channel_hash_hex")
            sig = sign_as(
                row.get("sender_hash", ""), ch, row.get("message_id", ""),
                row.get("timestamp", 0.0), row.get("content", ""),
                row.get("reply_to"), row.get("last_seen_id"),
                row.get("image_data"),
            ) if ch else None
            if sig is not None:
                row["author_sig"] = sig
        row.pop("channel_hash_hex", None)
        signed.append(row)
    return {
        F_MSG_TYPE:       MT_SYNC_RESPONSE,
        F_SYNC_MESSAGES:  msgpack.packb(signed, use_bin_type=True),
        F_SYNC_TRUNCATED: truncated,
    }


# ---------------------------------------------------------------------------
# Group F: status honesty
# ---------------------------------------------------------------------------

class TestPruneMustBeDrivenFromOutside:
    """F1: SyncStatusTracker resolves a stalled request only when pruned.

    The backend drives it from its periodic tick (backend_core.py); the
    manager itself never does, so a peer wired up without that tick -- every
    TestPeer here -- reports a stalled request as SYNCING indefinitely.
    """

    def test_stalled_request_stays_syncing_forever_without_a_prune_caller(
        self, peer_factory, monkeypatch
    ):
        """
        A peer that never answers should eventually stop being reported as
        actively syncing. Without a prune caller, a channel with a
        permanently silent peer reports SYNCING forever, long past
        PEER_RESPONSE_TIMEOUT_SECS -- the user never sees any signal that the
        sync stalled.
        """
        monkeypatch.setattr(sync_status, "PEER_RESPONSE_TIMEOUT_SECS", 0.05)

        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = alice.channel_mgr.create_channel("f1-stall", "", "public")
        _seed_channel_on_peer(bob, ch_hash, "f1-stall", alice.identity.hash_hex)
        _seed_channel_on_peer(carol, ch_hash, "f1-stall", alice.identity.hash_hex)

        # Simulate a peer that received the request but never answers --
        # crashed, buggy, or malicious.
        carol.sync_mgr._handle_sync_request = lambda *a, **kw: None

        bob.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, time.time())
        assert bob.sync_mgr.status.get_state(ch_hash) == SyncState.SYNCING

        # Comfortably past the (monkeypatched) response timeout.
        time.sleep(0.2)

        assert bob.sync_mgr.status.get_state(ch_hash) == SyncState.SYNCING, (
            "expected the stalled request to still report SYNCING because "
            "nothing prunes this peer -- if this now fails, SyncManager may "
            "have started calling SyncStatusTracker.prune() itself"
        )

    def test_manual_prune_correctly_resolves_the_single_peer_case(
        self, peer_factory, monkeypatch
    ):
        """
        The tracker's own prune() logic is sound in isolation (single
        outstanding peer): calling it manually turns the stalled request into
        the honest WAITING state -- still short of an answer, but not a claim
        that history is missing. This isolates F1 to who calls it, not
        whether it works.
        """
        monkeypatch.setattr(sync_status, "PEER_RESPONSE_TIMEOUT_SECS", 0.05)

        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = alice.channel_mgr.create_channel("f1-manual-prune", "", "public")
        _seed_channel_on_peer(bob, ch_hash, "f1-manual-prune", alice.identity.hash_hex)
        _seed_channel_on_peer(carol, ch_hash, "f1-manual-prune", alice.identity.hash_hex)

        carol.sync_mgr._handle_sync_request = lambda *a, **kw: None
        bob.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, time.time())
        time.sleep(0.2)

        bob.sync_mgr.status.prune()

        assert bob.sync_mgr.status.get_state(ch_hash) == SyncState.WAITING, (
            "prune() itself did not resolve a stalled single-peer request to WAITING"
        )


class TestSyncedIsScopedToKnownPeers:
    def test_synced_means_every_known_peer_answered_not_full_history(
        self, peer_factory
    ):
        """
        SYNCED is a settled, scoped claim: "every peer we know about answered
        and had nothing more" -- not "no history exists anywhere". Carol,
        Dave, and Eve are the only peers Bob's local view knows about for
        this channel, and each genuinely has nothing new for him -- they
        answer honestly with an empty list. Frank is a real member holding a
        message Bob has never seen, but Frank's announce never reached Bob,
        so Bob's local peer list never included him and he was never asked.

        On a partition-tolerant mesh there is no way to enumerate every peer
        who might hold history, so an unknown peer like Frank is out of
        scope by design -- SYNCED honestly reports "up to date with the
        peers I know about", and asserting the answered-peer count that
        backs the claim (rather than just the state) keeps a regression that
        silently narrows peer discovery from passing unnoticed: if Bob's
        peer list ever quietly dropped Carol, Dave, or Eve, this would still
        see SYNCED but the count would fall below 3.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")
        dave = peer_factory("dave")
        eve = peer_factory("eve")
        frank = peer_factory("frank")

        ch_hash = alice.channel_mgr.create_channel("f2-scale", "", "public")
        for peer in (bob, carol, dave, eve, frank):
            _seed_channel_on_peer(peer, ch_hash, "f2-scale", alice.identity.hash_hex)

        _insert_message(
            frank.storage, ch_hash, alice.identity.hash_hex,
            "only Frank has this", time.time(),
        )

        ts = time.time()
        for peer in (carol, dave, eve):
            bob.sync_mgr._send_sync_request(peer.identity.hash_hex, ch_hash, ts)

        assert wait_for(
            lambda: bob.sync_mgr.status.get_status(ch_hash)["pending_peers"] == 0,
            timeout=5,
        ), "Carol, Dave, and Eve never all answered"

        status = bob.sync_mgr.status.get_status(ch_hash)
        assert status["state"] == SyncState.SYNCED.value, (
            f"expected SYNCED once every known peer answered honestly, "
            f"got {status['state']}"
        )
        assert status["answered_peers"] == 3, (
            "the SYNCED claim should be backed by exactly the three peers "
            f"Bob actually knew about and asked, got {status['answered_peers']} -- "
            "Frank is correctly out of scope since his announce never reached Bob"
        )


class TestPartialMultiResponderHonesty:
    def test_partial_round_does_not_report_synced_even_after_pruning(
        self, peer_factory, monkeypatch
    ):
        """
        Of three responders: Carol answers, Dave never responds, and an
        unreachable peer's path never resolves.

        Immediately after the round, Dave's still-pending request correctly
        keeps the channel out of SYNCED -- that part is already honest.  But
        once Dave is pruned to SILENT (what should happen automatically; see
        F1), the state must still not claim the channel is synced: Carol's
        single answer says nothing about what Dave or the unreachable peer
        might be holding.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")
        dave = peer_factory("dave")
        unreachable_hex = "ee" * 16

        ch_hash = alice.channel_mgr.create_channel("f3-partial", "", "public")
        _seed_channel_on_peer(bob, ch_hash, "f3-partial", alice.identity.hash_hex)
        _seed_channel_on_peer(carol, ch_hash, "f3-partial", alice.identity.hash_hex)
        _seed_channel_on_peer(dave, ch_hash, "f3-partial", alice.identity.hash_hex)

        dave.sync_mgr._handle_sync_request = lambda *a, **kw: None

        ts = time.time()
        bob.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, ts)
        bob.sync_mgr._send_sync_request(dave.identity.hash_hex, ch_hash, ts)
        bob.sync_mgr._send_sync_request(unreachable_hex, ch_hash, ts)

        assert wait_for(
            lambda: bob.sync_mgr.status.get_status(ch_hash)["pending_peers"] == 1,
            timeout=5,
        ), "Carol's answer and the unreachable mark never both landed"

        state = bob.sync_mgr.status.get_state(ch_hash)
        assert state == SyncState.SYNCING, (
            f"expected SYNCING while Dave is still pending, got {state}"
        )

        monkeypatch.setattr(sync_status, "PEER_RESPONSE_TIMEOUT_SECS", -1)
        bob.sync_mgr.status.prune()

        state = bob.sync_mgr.status.get_state(ch_hash)
        assert state != SyncState.SYNCED, (
            f"reported {state.value} as up to date after Dave went silent and "
            f"the third peer stayed unreachable -- Carol's single answer masked both"
        )


class TestPublicChannelAfterRestart:
    def test_empty_local_peer_cache_after_restart_reports_waiting(self, peer_factory):
        """
        SubscriptionManager._subscribers is in-memory only.  After a restart,
        a public channel with real subscribers on the network looks
        peer-less to the local process, and note_no_peers() reports WAITING.

        Bob owns this channel (not just subscribes to it): _get_channel_peers
        always adds the stored channel's creator_hash regardless of the
        subscriber cache, so a peer who merely joined someone else's public
        channel always finds at least the creator and never actually reaches
        an empty peer set here -- only the owner's own post-restart view can
        be genuinely peer-less.  This confirms the current code path; see
        the accompanying report for whether WAITING (implying "asked, nobody
        reachable") or a distinct UNKNOWN/stale-cache state would be the more
        honest label for "we don't actually know who's out there any more".
        """
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = bob.channel_mgr.create_channel("f4-restart", "", "public")
        _seed_channel_on_peer(carol, ch_hash, "f4-restart", bob.identity.hash_hex)

        # Bob previously knew Carol as a subscriber, then "restarted" --
        # SubscriptionManager's in-memory table is wiped, exactly as it would
        # be by a fresh process over the same on-disk storage.
        bob.subscription_mgr._subscribers[ch_hash] = {carol.identity.hash_hex}
        bob.subscription_mgr._subscribers.clear()

        bob.sync_mgr._request_sync_for_channel(ch_hash, time.time())

        state = bob.sync_mgr.status.get_state(ch_hash)
        assert state == SyncState.WAITING, (
            f"expected the post-restart peer-less request to report WAITING, got {state}"
        )


# ---------------------------------------------------------------------------
# Group G: transport / identity edges
# ---------------------------------------------------------------------------

class TestPendingRequestKeyCollision:
    def test_second_request_to_same_peer_overwrites_the_first(self, peer_factory):
        """
        _record_pending_request keys pending state by (channel, peer) alone.
        Two independent triggers (e.g. a startup sync and an announce-driven
        request) firing before either answer lands means the second request's
        entry replaces the first's.  Whichever response arrives claims the
        sole entry; the other is dropped as unsolicited even though it
        genuinely answers a request we sent.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = alice.channel_mgr.create_channel("g1-overwrite", "", "public")
        _seed_channel_on_peer(bob, ch_hash, "g1-overwrite", alice.identity.hash_hex)
        _seed_channel_on_peer(carol, ch_hash, "g1-overwrite", alice.identity.hash_hex)

        ts = time.time()

        # Two independent triggers fire in close succession, before either
        # answer lands -- modeled directly since the real race is timing
        # dependent.
        bob.sync_mgr._record_pending_request(ch_hash, carol.identity.hash_hex, ts)
        bob.sync_mgr._record_pending_request(ch_hash, carol.identity.hash_hex, ts + 0.5)

        msg_one_id = _compute_message_id("answers the first request",
                                         alice.identity.hash_hex, ts + 1)
        msg_two_id = _compute_message_id("answers the second request",
                                         alice.identity.hash_hex, ts + 2)

        fields_1 = _sync_response_fields([{
            "sender_hash":  alice.identity.hash_hex,
            "sender_name":  "Alice",
            "content":      "answers the first request",
            "timestamp":    ts + 1,
            "message_id":   msg_one_id,
            "reply_to":     None,
            "last_seen_id": None,
        }], channel_hash_hex=ch_hash)
        fields_1[F_CHANNEL_HASH] = bytes.fromhex(ch_hash)

        fields_2 = _sync_response_fields([{
            "sender_hash":  alice.identity.hash_hex,
            "sender_name":  "Alice",
            "content":      "answers the second request",
            "timestamp":    ts + 2,
            "message_id":   msg_two_id,
            "reply_to":     None,
            "last_seen_id": None,
        }], channel_hash_hex=ch_hash)
        fields_2[F_CHANNEL_HASH] = bytes.fromhex(ch_hash)

        bob.sync_mgr._handle_sync_response(fields_1, ch_hash, carol.identity.hash_hex)
        bob.sync_mgr._handle_sync_response(fields_2, ch_hash, carol.identity.hash_hex)

        assert wait_for_message(bob.storage, ch_hash, msg_one_id, timeout=3), \
            "the first legitimate response never landed"
        assert wait_for_message(bob.storage, ch_hash, msg_two_id, timeout=3), (
            "the second legitimate response was dropped as unsolicited because "
            "_record_pending_request's (channel, peer) key overwrote the first "
            "request's entry before either could be claimed"
        )

    def test_peer_key_forms_both_claim_the_same_pending_request(self, peer_factory):
        """
        A response may identify its sender by identity hash or by the LXMF
        delivery-destination hash for the same identity; either form must be
        able to claim the outstanding request recorded under the other.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = alice.channel_mgr.create_channel("g1-keyforms", "", "public")
        _seed_channel_on_peer(bob, ch_hash, "g1-keyforms", alice.identity.hash_hex)

        bob.sync_mgr._record_pending_request(ch_hash, carol.identity.hash_hex, time.time())

        delivery_hex = delivery_dest_hash_hex(carol.identity.hash_hex)
        claimed = bob.sync_mgr._claim_pending_request(ch_hash, delivery_hex)
        assert claimed is not None, (
            "a response identified by the delivery-destination hash form could not "
            "claim a request recorded under the identity-hash form"
        )


class TestQuarantineExpiry:
    def _quarantinable_hint(self, sender, recipient, ch_hash, msg_id):
        dest = RNS.Destination(
            recipient.identity.rns_identity, RNS.Destination.OUT,
            RNS.Destination.SINGLE, "lxmf", "delivery",
        )
        lxm = LXMF.LXMessage(dest, sender.router.delivery_destination, "",
                             desired_method=LXMF.LXMessage.DIRECT)
        lxm.fields = {
            F_MSG_TYPE:      MT_MISSED_DELIVERY,
            F_CHANNEL_HASH:  bytes.fromhex(ch_hash),
            F_MISSED_FOR:    recipient.identity.hash_hex,
            F_MISSED_MSG_ID: msg_id,
        }
        lxm.pack()  # real signature, so re-validation on release genuinely succeeds
        lxm.signature_validated = False
        lxm.unverified_reason = LXMF.LXMessage.SOURCE_UNKNOWN
        return lxm

    def test_release_before_ttl_dispatches_the_held_hint(self, peer_factory):
        """
        Control baseline: a message quarantined for an unknown source and
        released before QUARANTINE_TTL_SECS elapses is re-validated and
        dispatched normally.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("g2-baseline", "", "public")
        _seed_channel_on_peer(bob, ch_hash, "g2-baseline", alice.identity.hash_hex)

        msg_id = _insert_message(alice.storage, ch_hash, alice.identity.hash_hex,
                                 "hint target")
        lxm = self._quarantinable_hint(alice, bob, ch_hash, msg_id)

        bob.router._on_message_received(lxm)
        assert sum(len(v) for v in bob.router._quarantine.values()) == 1

        bob.router.release_quarantined(alice.identity.hash_hex)

        assert wait_for(
            lambda: msg_id in bob.storage.get_missed_message_ids(
                ch_hash, bob.identity.hash_hex),
            timeout=3,
        ), "a genuinely valid quarantined hint was not dispatched on release"

    def test_release_after_ttl_silently_loses_the_hint(self, peer_factory, monkeypatch):
        """
        release_quarantined() re-validates and dispatches held messages, but
        _prune_quarantine_locked() drops anything older than
        QUARANTINE_TTL_SECS first -- and it runs on every release_quarantined
        call, including the one that would otherwise have delivered this
        message.  An announce arriving after the TTL (entirely plausible for
        a peer offline longer than 5 minutes) releases nothing: the hint is
        gone with no error, no warning log, and no retry anywhere in
        on_peer_appeared.
        """
        import trenchchat.network.router as router_module
        monkeypatch.setattr(router_module, "QUARANTINE_TTL_SECS", 0.05)

        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("g2-ttl", "", "public")
        _seed_channel_on_peer(bob, ch_hash, "g2-ttl", alice.identity.hash_hex)

        msg_id = _insert_message(alice.storage, ch_hash, alice.identity.hash_hex,
                                 "hint target 2")
        lxm = self._quarantinable_hint(alice, bob, ch_hash, msg_id)

        bob.router._on_message_received(lxm)
        assert sum(len(v) for v in bob.router._quarantine.values()) == 1

        time.sleep(0.2)  # comfortably past the monkeypatched TTL

        bob.router.release_quarantined(alice.identity.hash_hex)

        assert not wait_for(
            lambda: msg_id in bob.storage.get_missed_message_ids(
                ch_hash, bob.identity.hash_hex),
            timeout=1,
        ), (
            "expected the TTL-expired quarantine entry to be dropped on release "
            "(current, confirmed behavior) -- if this now fails, quarantine's TTL "
            "pruning or release path has changed and the finding below is stale"
        )
        assert sum(len(v) for v in bob.router._quarantine.values()) == 0, \
            "the expired entry should have been pruned out of the quarantine table"


class TestMissedDeliveryHintNoRetry:
    def test_hint_to_a_momentarily_unresolvable_peer_is_never_retried(
        self, peer_factory, monkeypatch
    ):
        """
        _send_raw returns False when RNS.Identity.recall() fails, and
        _on_missed_delivery_event ignores that return value entirely: unlike
        messaging.py, it never calls RNS.Transport.request_path() and never
        queues the hint for retry.  Once the peer becomes reachable,
        on_peer_appeared only flushes pending chat messages and issues fresh
        sync requests -- it never re-broadcasts missed-delivery hints -- so
        the hint aimed at that peer is lost even after they reappear.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        dave = peer_factory("dave")

        ch_hash = alice.channel_mgr.create_channel("g3-hint-drop", "", "public")
        _seed_channel_on_peer(bob, ch_hash, "g3-hint-drop", alice.identity.hash_hex)
        _seed_channel_on_peer(dave, ch_hash, "g3-hint-drop", alice.identity.hash_hex)

        msg_id = _insert_message(alice.storage, ch_hash, alice.identity.hash_hex,
                                 "Bob missed this")

        dave_delivery_hash = RNS.Destination.hash(
            bytes.fromhex(dave.identity.hash_hex), "lxmf", "delivery"
        )
        real_recall = RNS.Identity.recall

        def flaky_recall(dest_hash, *args, **kwargs):
            if dest_hash == dave_delivery_hash:
                return None
            return real_recall(dest_hash, *args, **kwargs)

        monkeypatch.setattr(RNS.Identity, "recall", flaky_recall)

        # Alice broadcasts the hint while Dave's identity momentarily fails
        # to recall -- _send_raw returns False, silently.
        alice.sync_mgr._on_missed_delivery_event(
            channel_hash_hex=ch_hash,
            missed_peer_hex=bob.identity.hash_hex,
            msg_id=msg_id,
            subscriber_hashes=[alice.identity.hash_hex, bob.identity.hash_hex,
                               dave.identity.hash_hex],
        )

        monkeypatch.undo()  # Dave's identity resolves normally again

        # Dave "reappears" -- the real trigger for on_peer_appeared.
        alice.sync_mgr.on_peer_appeared(dave.identity.hash_hex)

        assert wait_for(
            lambda: msg_id in dave.storage.get_missed_message_ids(
                ch_hash, bob.identity.hash_hex),
            timeout=3,
        ), (
            "the missed-delivery hint to Dave should reach him once he becomes "
            "reachable, but _on_missed_delivery_event has no retry and "
            "on_peer_appeared never re-broadcasts hints -- it was dropped for good"
        )


class TestOversizedImageViaSync:
    def test_oversized_image_is_cleared_but_message_still_arrives(self, peer_factory):
        """
        _handle_sync_response nulls an image over MAX_IMAGE_BYTES but keeps
        the message.  This is exercised for live chat delivery elsewhere
        (test_adversarial.py); this confirms the same protection holds on
        the sync path, where the payload arrives via F_SYNC_MESSAGES instead
        of individual image fields.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = alice.channel_mgr.create_channel("g4-oversized-sync", "", "public")
        _seed_channel_on_peer(bob, ch_hash, "g4-oversized-sync", alice.identity.hash_hex)
        _seed_channel_on_peer(carol, ch_hash, "g4-oversized-sync", alice.identity.hash_hex)

        ts = time.time()
        bob.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, ts)

        oversized = b"\x00" * (MAX_IMAGE_BYTES + 1)
        oversized_id = _compute_message_id("picture attached",
                                           alice.identity.hash_hex, ts + 1)
        fields = _sync_response_fields([{
            "sender_hash":  alice.identity.hash_hex,
            "sender_name":  "Alice",
            "content":      "picture attached",
            "timestamp":    ts + 1,
            "message_id":   oversized_id,
            "reply_to":     None,
            "last_seen_id": None,
            "image_data":   oversized,
        }], channel_hash_hex=ch_hash)
        fields[F_CHANNEL_HASH] = bytes.fromhex(ch_hash)

        bob.sync_mgr._handle_sync_response(fields, ch_hash, carol.identity.hash_hex)

        assert wait_for_message(bob.storage, ch_hash, oversized_id, timeout=5), \
            "the message itself should still be delivered via sync"

        rows = [m for m in bob.storage.get_messages(ch_hash)
                if m["message_id"] == oversized_id]
        assert rows, "message missing entirely"
        assert not rows[0]["image_data"], "an over-cap image was stored instead of cleared"
        assert rows[0]["content"] == "picture attached", \
            "an unrelated field was corrupted while clearing the oversized image"
