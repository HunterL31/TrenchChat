"""
Integration tests for permission and membership-tenure changes that happen
mid-sync -- the window between a responder evaluating a sync request and a
requester independently re-evaluating the response it gets back.

Covers six scenarios (E1-E6): an asymmetric full_sync grant burning the
responder's deep-sync cooldown for nothing, a full_sync revocation partway
through a chained backfill, a role promotion landing between a sync request
and its response, a kick landing in the same window, tenure fail-open
asymmetry between mesh peers, and Messaging.flush_pending's lack of any
permission check of its own.

sync.py's _handle_sync_request/_handle_sync_response now auto-chain a
follow-up request (_continue_sync) the instant a truncated response lands,
via SyncManager._continue_sync, bounded by MAX_SYNC_CONTINUATIONS. That
chain fires within roughly one TestTransport round trip (~0.05-0.1s) of the
first response being processed -- far faster than a test's own thread can
observe "batch one landed" (via polling) and then act, so E2 below drives
SyncManager._handle_sync_request directly (capturing responses through a
monkeypatched _send_raw) to land a permission revocation deterministically
between two batches of a chain, rather than racing the real pipeline's own
near-instant continuation.
"""

import time

import msgpack

from tests.helpers import sign_as, wait_for_member, wait_for_message
from trenchchat.core.messaging import _compute_message_id
from trenchchat.core.permissions import (
    FULL_SYNC, PRESET_PRIVATE, ROLE_ADMIN, ROLE_MEMBER, ROLE_OWNER, SEND_MESSAGE,
)
from trenchchat.core.protocol import F_SYNC_MESSAGES, F_SYNC_TRUNCATED, F_SYNC_WINDOW_START
from trenchchat.core.sync import DEEP_SYNC_COOLDOWN_SECS, MAX_RESPONSE_MESSAGES, SYNC_WINDOW_SECS


# ---------------------------------------------------------------------------
# Helpers (per the brief: _seed_channel_on_peer / _insert_message copied
# verbatim from tests/test_sync.py)
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


def _setup_tenured_channel(peer_factory, member_perms=None):
    """Create alice (owner) and bob (member) on a shared invite-only channel
    with matching membership and permissions state on both sides.

    Mirrors tests/test_sync.py's module-local _setup_invite_channel. Callers
    seed any additional tenure rows themselves -- each scenario in this file
    needs precise control over who has which tenure interval on which side.
    """
    alice = peer_factory("alice")
    bob = peer_factory("bob")
    perms = dict(PRESET_PRIVATE)
    perms[ROLE_MEMBER] = member_perms if member_perms is not None else [SEND_MESSAGE]
    ch_hash = alice.channel_mgr.create_channel("inflight-ch", "", permissions=perms)
    bob.storage.upsert_channel(ch_hash, "inflight-ch", "", alice.identity.hash_hex,
                                perms, time.time())
    bob.storage.subscribe(ch_hash)
    alice.invite_mgr.publish_member_list(ch_hash, add_members=[bob.identity.hash])
    assert wait_for_member(alice.storage, ch_hash, bob.identity.hash_hex)
    bob.storage.upsert_member(ch_hash, bob.identity.hash_hex, "Bob", role=ROLE_MEMBER)
    bob.storage.upsert_member(ch_hash, alice.identity.hash_hex, "Alice", role=ROLE_OWNER)
    bob.storage.set_channel_permissions(ch_hash, perms)
    return alice, bob, ch_hash, perms


# ---------------------------------------------------------------------------
# E1 -- asymmetric permission propagation burns the deep-sync cooldown
# ---------------------------------------------------------------------------

class TestAsymmetricPermissionPropagationCooldown:
    def test_stale_responder_doc_burns_cooldown_and_blocks_recovery_after_convergence(
        self, peer_factory
    ):
        """
        Bob's own copy of the permissions already grants full_sync to the
        member role; Alice (the responder he asks) has not received that
        update yet. Alice's deep-sync cooldown for Bob is a per-(channel,
        peer) slot spent unconditionally by the fallback timestamp sweep,
        before the tenure/full_sync filter ever runs -- so the throttled,
        doomed request still burns it. Once the docs converge, recovery
        should not stay blocked for a further DEEP_SYNC_COOLDOWN_SECS just
        because the first attempt happened to land inside the window a
        stale request already spent. Uses the real cooldown value (not
        monkeypatched to 0) so the finding is honest about the stall length.
        """
        alice, bob, ch_hash, perms = _setup_tenured_channel(peer_factory)

        join_ts = time.time() - SYNC_WINDOW_SECS - 3600
        alice.storage.open_tenure(ch_hash, alice.identity.hash_hex, join_ts)
        alice.storage.open_tenure(ch_hash, bob.identity.hash_hex, join_ts + 100)

        pre_join_ts = join_ts + 10
        pre_join_id = _insert_message(alice.storage, ch_hash, alice.identity.hash_hex,
                                       "before bob joined", pre_join_ts)

        # Bob's own copy already has the new doc (full_sync granted); Alice,
        # the responder, is still on the old one -- the propagation lag.
        new_perms = dict(perms)
        new_perms[ROLE_MEMBER] = [SEND_MESSAGE, FULL_SYNC]
        bob.storage.set_channel_permissions(ch_hash, new_perms)

        bob.sync_mgr._send_sync_request(alice.identity.hash_hex, ch_hash, join_ts)
        time.sleep(1.0)

        assert not bob.storage.message_exists(pre_join_id), (
            "setup assumption violated: Alice's stale (no full_sync) doc "
            "should have withheld the pre-join message"
        )
        assert (ch_hash, bob.identity.hash_hex) in alice.sync_mgr._deep_sync_last_served, (
            "setup assumption violated: the throttled/doomed request should "
            "still have consumed Alice's deep-sync cooldown slot for Bob"
        )

        # The docs now converge.
        alice.storage.set_channel_permissions(ch_hash, new_perms)

        bob.sync_mgr._send_sync_request(alice.identity.hash_hex, ch_hash, join_ts)
        assert wait_for_message(bob.storage, ch_hash, pre_join_id, timeout=5), (
            f"recovery stayed blocked for the full {DEEP_SYNC_COOLDOWN_SECS}s "
            f"cooldown even though the permission docs had converged and the "
            f"pre-join message was now legitimately deliverable -- the "
            f"deep-sync cooldown (sync.py's _deep_sync_allowed) is keyed only "
            f"by (channel, peer), with no way to distinguish 'a doomed "
            f"request under stale permissions' from 'a request that would "
            f"now succeed', so a single unlucky request timed before "
            f"convergence can strand a legitimate backfill for up to "
            f"DEEP_SYNC_COOLDOWN_SECS after the permission grant lands"
        )


# ---------------------------------------------------------------------------
# E2 -- full_sync revoked mid (manual, watermark-driven) backfill
# ---------------------------------------------------------------------------

class TestFullSyncRevokedMidBackfill:
    def test_revocation_between_chained_batches_serves_first_batch_then_nothing_further(
        self, peer_factory
    ):
        """
        A backfill spans two batches chained by SyncManager._continue_sync
        (fired automatically off a truncated response). full_sync is revoked
        between them. The first, already-authorised batch must stand; the
        second must come back correctly empty under the now-current
        permissions, not still carrying rows that were only ever permitted
        under the doc the first batch was served under.

        Drives _handle_sync_request directly (see module docstring) rather
        than the real send/receive pipeline: the real continuation chain
        fires within about one TestTransport round trip of the first
        response landing, which leaves no reliable window for a test's own
        thread to revoke permissions between the two batches.
        """
        member_perms = [SEND_MESSAGE, FULL_SYNC]
        alice, bob, ch_hash, perms = _setup_tenured_channel(peer_factory,
                                                              member_perms=member_perms)

        join_ts = time.time()
        history_start = join_ts - 1000
        alice.storage.open_tenure(ch_hash, alice.identity.hash_hex, history_start)
        alice.storage.open_tenure(ch_hash, bob.identity.hash_hex, join_ts)

        total = MAX_RESPONSE_MESSAGES + 20
        msg_ids = []
        for i in range(total):
            ts = history_start + i + 1  # all before bob's own join
            mid = _insert_message(alice.storage, ch_hash, alice.identity.hash_hex,
                                   f"pre-join {i}", ts)
            msg_ids.append(mid)

        sent = []
        alice.sync_mgr._send_raw = lambda dest, fields: sent.append(fields) or True

        alice.sync_mgr._handle_sync_request(
            {F_SYNC_WINDOW_START: 0.0}, ch_hash, bob.identity.hash_hex
        )
        assert len(sent) == 1, "setup assumption violated: Alice should have responded once"
        batch1 = msgpack.unpackb(sent[0][F_SYNC_MESSAGES], raw=False)
        assert [m["message_id"] for m in batch1] == msg_ids[:MAX_RESPONSE_MESSAGES], (
            "setup assumption violated: first batch should be exactly the "
            "first MAX_RESPONSE_MESSAGES full_sync-eligible rows"
        )
        assert sent[0][F_SYNC_TRUNCATED] is True
        resume_ts = batch1[-1]["timestamp"]

        # The grant is revoked between the first batch and the chained
        # follow-up request for the rest.
        revoked_perms = dict(perms)
        revoked_perms[ROLE_MEMBER] = [SEND_MESSAGE]
        alice.storage.set_channel_permissions(ch_hash, revoked_perms)

        alice.sync_mgr._handle_sync_request(
            {F_SYNC_WINDOW_START: resume_ts}, ch_hash, bob.identity.hash_hex
        )
        assert len(sent) == 2, "setup assumption violated: Alice should have answered again"
        batch2 = msgpack.unpackb(sent[1][F_SYNC_MESSAGES], raw=False)

        assert batch2 == [], (
            "a row that was only ever permitted under the doc the first "
            "batch was served under is still being swept up and served "
            "after the grant that authorised it was revoked -- "
            "_collect_permitted_rows re-derives full_sync from the current "
            "stored permissions on every call, so this indicates a stale "
            "read somewhere in that path"
        )
        assert sent[1][F_SYNC_TRUNCATED] is False, (
            "an empty batch with nothing left to scan should not claim more "
            "remains"
        )


# ---------------------------------------------------------------------------
# E3 -- role promoted between request and response
# ---------------------------------------------------------------------------

class TestRolePromotionMidSync:
    def test_promotion_after_partial_response_leaves_withheld_history_stranded(
        self, peer_factory
    ):
        """
        Bob asks for everything while still a plain member (no full_sync):
        a partial answer -- only the post-join message -- comes back, and
        the watermark advances to it. Bob is then promoted to an
        admin/full_sync-bearing role, fully propagated to both sides
        (isolating this from E1's propagation-lag scenario). The normal
        reconnect/recovery path (SyncManager.on_peer_appeared /
        _request_sync_for_channel) resumes incrementally from the stored
        watermark, not from scratch -- so the pre-join message, which
        predates that watermark, can never be revisited by it.
        """
        alice, bob, ch_hash, perms = _setup_tenured_channel(peer_factory)

        alice_join_ts = time.time() - 1000
        bob_join_ts = time.time()
        for peer in (alice, bob):
            peer.storage.open_tenure(ch_hash, alice.identity.hash_hex, alice_join_ts)
            peer.storage.open_tenure(ch_hash, bob.identity.hash_hex, bob_join_ts)

        pre_join_id = _insert_message(alice.storage, ch_hash, alice.identity.hash_hex,
                                       "before bob joined", alice_join_ts + 10)
        post_join_id = _insert_message(alice.storage, ch_hash, alice.identity.hash_hex,
                                        "after bob joined", bob_join_ts + 10)

        bob.sync_mgr._send_sync_request(alice.identity.hash_hex, ch_hash, 0.0)
        assert wait_for_message(bob.storage, ch_hash, post_join_id, timeout=5)
        time.sleep(0.3)
        assert not bob.storage.message_exists(pre_join_id), (
            "setup assumption violated: pre-join message should have been "
            "withheld while Bob was still a plain member"
        )

        watermark = bob.storage.get_subscriptions()[0]["last_sync_at"]
        assert watermark > alice_join_ts + 10, (
            "setup assumption violated: watermark should already sit past "
            "the withheld pre-join message's timestamp"
        )

        # Bob is promoted to admin with full_sync; fully propagated.
        promoted_perms = dict(perms)
        promoted_perms[ROLE_ADMIN] = [SEND_MESSAGE, FULL_SYNC]
        for peer in (alice, bob):
            peer.storage.upsert_member(ch_hash, bob.identity.hash_hex, "Bob", role=ROLE_ADMIN)
            peer.storage.set_channel_permissions(ch_hash, promoted_perms)

        # The real reconnect path resumes incrementally from the watermark.
        bob.sync_mgr._request_sync_for_channel(ch_hash, watermark)

        assert wait_for_message(bob.storage, ch_hash, pre_join_id, timeout=5), (
            "the pre-join message stayed permanently unreachable through the "
            "normal incremental reconnect path even after Bob's promotion -- "
            "history withheld before a permission grant is stranded behind "
            "the watermark forever unless something explicitly re-requests "
            "from before it, and nothing in this codebase does that "
            "automatically on a role/permission change"
        )

    def test_explicit_full_resync_after_promotion_does_recover_the_history(
        self, peer_factory, monkeypatch
    ):
        """
        Companion to the test above: the withheld history is not
        unrecoverable in principle -- an explicit re-request from before the
        withheld message's timestamp does retrieve it once the promotion has
        taken effect on both sides. This isolates the problem demonstrated
        above to "nothing triggers that re-request automatically", not to a
        broken tenure/full_sync filter.

        Both the pre- and post-promotion requests here start from 0.0, so
        both are "deep" backfills against the same (channel, peer) pair;
        DEEP_SYNC_COOLDOWN_SECS is monkeypatched to 0 so the unrelated
        cooldown from E1 doesn't block the second one -- this test is about
        the tenure/full_sync filter, not the cooldown.
        """
        monkeypatch.setattr("trenchchat.core.sync.DEEP_SYNC_COOLDOWN_SECS", 0)
        alice, bob, ch_hash, perms = _setup_tenured_channel(peer_factory)

        alice_join_ts = time.time() - 1000
        bob_join_ts = time.time()
        for peer in (alice, bob):
            peer.storage.open_tenure(ch_hash, alice.identity.hash_hex, alice_join_ts)
            peer.storage.open_tenure(ch_hash, bob.identity.hash_hex, bob_join_ts)

        pre_join_id = _insert_message(alice.storage, ch_hash, alice.identity.hash_hex,
                                       "before bob joined", alice_join_ts + 10)

        bob.sync_mgr._send_sync_request(alice.identity.hash_hex, ch_hash, 0.0)
        time.sleep(0.3)
        assert not bob.storage.message_exists(pre_join_id)

        promoted_perms = dict(perms)
        promoted_perms[ROLE_ADMIN] = [SEND_MESSAGE, FULL_SYNC]
        for peer in (alice, bob):
            peer.storage.upsert_member(ch_hash, bob.identity.hash_hex, "Bob", role=ROLE_ADMIN)
            peer.storage.set_channel_permissions(ch_hash, promoted_perms)

        bob.sync_mgr._send_sync_request(alice.identity.hash_hex, ch_hash, 0.0)

        assert wait_for_message(bob.storage, ch_hash, pre_join_id, timeout=5), (
            "an explicit full re-request from 0.0 should recover the "
            "pre-promotion history once full_sync has actually been granted"
        )


# ---------------------------------------------------------------------------
# E4 -- kick lands between issuing a sync request and applying its response
# ---------------------------------------------------------------------------

class TestKickInFlightDuringSyncResponse:
    def test_response_applied_after_requesters_own_kick_uses_no_current_membership_check(
        self, peer_factory
    ):
        """
        _peer_may_participate gates the REQUEST on the responder's side, but
        _handle_sync_response never re-checks the requester's own *current*
        membership when applying an incoming response -- only each message's
        historical tenure at send-time. Bob issues a sync request, is kicked
        before the response is applied, and the response carries a message
        that genuinely was legitimate history (sent while Bob was still a
        member). Because nothing re-checks whether Bob is *currently* a
        member of the channel, it is still inserted.
        """
        alice, bob, ch_hash, perms = _setup_tenured_channel(peer_factory)
        # Bob's own tenure view of the channel, seeded directly rather than
        # waiting on the real broadcast document to propagate and be
        # accepted -- that path isn't this test's concern (_setup_invite_
        # channel in test_sync.py seeds members/permissions the same way,
        # precisely to avoid depending on that timing).
        tenure_start = time.time() - 500
        bob.storage.open_tenure(ch_hash, alice.identity.hash_hex, tenure_start)
        bob.storage.open_tenure(ch_hash, bob.identity.hash_hex, tenure_start)

        valid_ts = time.time()
        valid_content = "sent while Bob was still a member"
        valid_msg_id = _compute_message_id(valid_content, alice.identity.hash_hex, valid_ts)

        # Bob issues a sync request (recorded as outstanding on his own side).
        bob.sync_mgr._record_pending_request(ch_hash, alice.identity.hash_hex)

        # Bob is kicked while that request is still in flight.
        bob.storage.remove_member(ch_hash, bob.identity.hash_hex)
        assert not bob.storage.is_member(ch_hash, bob.identity.hash_hex)

        # Alice's response, answering the request Bob issued before the
        # kick, now arrives and is applied.
        packed = msgpack.packb([{
            "sender_hash": alice.identity.hash_hex,
            "sender_name": "Alice",
            "content": valid_content,
            "timestamp": valid_ts,
            "message_id": valid_msg_id,
            "reply_to": None,
            "last_seen_id": None,
        }], use_bin_type=True)
        bob.sync_mgr._handle_sync_response(
            {F_SYNC_MESSAGES: packed}, ch_hash, alice.identity.hash_hex
        )

        assert not bob.storage.message_exists(valid_msg_id), (
            "Bob's own current membership (already revoked before this "
            "response was applied) is never re-checked when a sync response "
            "is ingested (sync.py's _handle_sync_response) -- only each "
            "message's *historical* tenure at send-time is checked. A "
            "message that was legitimate history when sent is still "
            "inserted into a client that is, by the time it arrives, fully "
            "kicked, which is inconsistent with 'kicked means kicked' and, "
            "combined with E6's flush_pending gap, means a kicked client "
            "keeps absorbing channel state through more than one path"
        )


# ---------------------------------------------------------------------------
# E5 -- tenure fail-open asymmetry (most security-relevant scenario)
# ---------------------------------------------------------------------------

class TestTenureFailOpenAsymmetry:
    def test_requesters_own_tenure_data_rejects_a_gap_message_a_tenure_blind_responder_served(
        self, peer_factory
    ):
        """
        Carol has zero tenure rows for the channel (has_any_tenure == False
        on her side), so _handle_sync_request's tenure filter never engages
        for her and she serves a gap message from a kicked member fully
        unfiltered. Alice, the requester, has her own complete, independent
        tenure records including the kick. sync.py's own docstring at the
        _handle_sync_response tenure check states the intent plainly: "so a
        malicious or buggy responder can't hand us history we aren't
        entitled to just by skipping its own filtering." This asserts that
        holds for exactly the case it describes.
        """
        alice, bob, ch_hash, perms = _setup_tenured_channel(peer_factory)
        carol = peer_factory("carol")
        carol.storage.upsert_channel(ch_hash, "inflight-ch", "", alice.identity.hash_hex,
                                      perms, time.time())
        carol.storage.subscribe(ch_hash)
        # Carol knows the real membership (needed for _peer_may_participate
        # to let Alice's request through at all) but deliberately has no
        # tenure rows -- has_any_tenure and is_member are independent
        # tables, and this scenario is specifically about the former being
        # empty while the latter is populated.
        carol.storage.upsert_member(ch_hash, alice.identity.hash_hex, "Alice", role=ROLE_OWNER)
        carol.storage.upsert_member(ch_hash, bob.identity.hash_hex, "Bob", role=ROLE_MEMBER)

        join_ts = time.time() - 300
        # publish_member_list already opened a tenure interval for Bob on
        # Alice's side synchronously (at ~now); replace it with one on our
        # own controlled timeline so close_tenure below closes the interval
        # we actually mean to close, not that auto-opened one.
        alice.storage.close_tenure(ch_hash, bob.identity.hash_hex, time.time())
        alice.storage.open_tenure(ch_hash, bob.identity.hash_hex, join_ts)

        kick_ts = join_ts + 100
        alice.storage.close_tenure(ch_hash, bob.identity.hash_hex, kick_ts)
        alice.storage.remove_member(ch_hash, bob.identity.hash_hex)

        gap_ts = kick_ts + 50
        gap_content = "Gap message Carol will serve unfiltered"
        gap_msg_id = _compute_message_id(gap_content, bob.identity.hash_hex, gap_ts)
        carol.storage.insert_message(
            channel_hash=ch_hash, sender_hash=bob.identity.hash_hex, sender_name="Bob",
            content=gap_content, timestamp=gap_ts, message_id=gap_msg_id,
            reply_to=None, last_seen_id=None, received_at=gap_ts,
            author_sig=sign_as(bob.identity.hash_hex, ch_hash, gap_msg_id,
                               gap_ts, gap_content),
        )

        assert not carol.storage.has_any_tenure(ch_hash), \
            "setup assumption violated: Carol should have no tenure data"
        assert alice.storage.has_any_tenure(ch_hash), \
            "setup assumption violated: Alice should have real tenure data"

        # Confirm Carol really does serve it unfiltered (the vulnerable
        # precondition), captured directly rather than over the wire.
        sent = []
        carol.sync_mgr._send_raw = lambda dest, fields: sent.append((dest, fields))
        carol.sync_mgr._handle_sync_request(
            {F_SYNC_WINDOW_START: join_ts}, ch_hash, alice.identity.hash_hex
        )
        assert sent, "setup assumption violated: Carol should have responded"
        served_ids = {
            m["message_id"] for m in msgpack.unpackb(sent[0][1][F_SYNC_MESSAGES], raw=False)
        }
        assert gap_msg_id in served_ids, (
            "setup assumption violated: Carol (no tenure data) should have "
            "served the gap message with no filtering applied"
        )

        # Alice now actually receives and processes that unfiltered response.
        alice.sync_mgr._record_pending_request(ch_hash, carol.identity.hash_hex)
        alice.sync_mgr._handle_sync_response(sent[0][1], ch_hash, carol.identity.hash_hex)

        assert not alice.storage.message_exists(gap_msg_id), (
            "Alice accepted a gap message from a kicked member that a "
            "tenure-blind responder served unfiltered -- her own independent "
            "receive-side re-check (sync.py's _handle_sync_response) should "
            "have caught it regardless of what Carol did"
        )


# ---------------------------------------------------------------------------
# E6 -- Messaging.flush_pending bypasses every permission check
# ---------------------------------------------------------------------------

class TestPendingRetryBypassesPermissionChecks:
    def test_kicked_peer_still_receives_a_message_queued_before_the_kick_once_reachable(
        self, peer_factory
    ):
        """
        Messaging.flush_pending has no membership, tenure, or permission
        check of its own. The only related guard, cancel_pending_for_channel
        (fired from sync.py's _on_member_list_updated), only clears *our
        own* outbound queue when *we* are the one removed from a channel --
        never when we are the one doing the kicking. A message Alice queued
        for Bob before kicking him is still sitting in her _pending dict
        afterwards and gets pushed at him unconditionally the moment he
        becomes reachable again, even though both sides now fully agree Bob
        is no longer a member.
        """
        alice, bob, ch_hash, perms = _setup_tenured_channel(peer_factory)

        ts = time.time()
        content = "queued before Bob was kicked"
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

        # Alice kicks Bob; fully applied on both sides (isolating this from
        # E4's in-flight-request scenario).
        for peer in (alice, bob):
            peer.storage.remove_member(ch_hash, bob.identity.hash_hex)
        assert not bob.storage.is_member(ch_hash, bob.identity.hash_hex)

        # Bob reappears; Alice's queue for him is flushed.
        alice.messaging.flush_pending(bob.identity.hash_hex)
        time.sleep(1.0)

        assert not bob.storage.message_exists(msg_id), (
            "neither layer stopped this: Messaging.flush_pending "
            "(messaging.py) sends every queued message unconditionally with "
            "no membership/tenure/permission check of its own, and the "
            "receiver's own inbound handler (Messaging._on_lxmf_message) "
            "only checks the *sender's* standing (is_member/has_permission "
            "on Alice), never the receiver's own current membership -- so a "
            "peer kicked while offline still absorbs messages that were "
            "queued for them before the kick, once reachable again"
        )
