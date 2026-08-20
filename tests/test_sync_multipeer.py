"""
Integration tests for sync fan-out across 3+ peers.

Every scenario in tests/test_sync.py involves at most two responders (plus a
spectator), but production code (_request_sync_for_channel and
on_peer_appeared in trenchchat/core/sync.py) loops over every peer on a
channel. These tests exercise what happens when several responders hold
disjoint, overlapping, or differently-permissioned views of the same
channel's history.
"""

import time

import RNS

from tests.helpers import sign_as, wait_for, wait_for_member, wait_for_message
from trenchchat.core.messaging import _compute_message_id
from trenchchat.core.sync_status import SyncState


# ---------------------------------------------------------------------------
# Helpers (duplicated from tests/test_sync.py -- module-local by convention)
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


def _confirm_membership(peer, ch_hash, timeout: float = 5.0):
    """Confirm a membership an admin created without an invite (see test_sync.py)."""
    assert wait_for(
        lambda: peer.storage.get_pending_member_doc(ch_hash) is not None,
        timeout=timeout,
    ), f"no pending membership arrived to confirm for {peer.name}"
    assert peer.invite_mgr.accept_pending_membership(ch_hash), \
        f"pending membership was rejected on confirmation for {peer.name}"


# ---------------------------------------------------------------------------
# A1 -- disjoint history across two responders
# ---------------------------------------------------------------------------

class TestDisjointHistoryFanout:
    def test_disjoint_history_across_two_responders_natural_ordering(self, peer_factory):
        """
        Carol holds messages 1-25, Dave holds 26-50, Bob holds none. Bob's
        watermark is a single per-channel value, so once Dave answers first
        and advances it, a later request to Carol starts from Dave's newest
        message instead of Bob's true starting point -- stranding Carol's
        older, disjoint history. Bob must end up with all 50 regardless of
        which peer answers first.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")
        dave = peer_factory("dave")

        ch_hash = alice.channel_mgr.create_channel("disjoint-natural", "", "public")
        _seed_channel_on_peer(carol, ch_hash, "disjoint-natural", alice.identity.hash_hex)
        _seed_channel_on_peer(dave, ch_hash, "disjoint-natural", alice.identity.hash_hex)
        _seed_channel_on_peer(bob, ch_hash, "disjoint-natural", alice.identity.hash_hex)
        bob.storage.update_last_sync(ch_hash, 0.0)
        bob.subscription_mgr._subscribers[ch_hash] = {
            carol.identity.hash_hex, dave.identity.hash_hex,
        }

        base = time.time()
        carol_ids = [
            _insert_message(carol.storage, ch_hash, alice.identity.hash_hex,
                            f"carol {i}", base + i)
            for i in range(1, 26)
        ]
        dave_ids = [
            _insert_message(dave.storage, ch_hash, alice.identity.hash_hex,
                            f"dave {i}", base + i)
            for i in range(26, 51)
        ]

        # Dave appears (and is fully synced) first.
        bob.sync_mgr.on_peer_appeared(dave.identity.hash_hex)
        assert wait_for(
            lambda: all(bob.storage.message_exists(mid) for mid in dave_ids),
            timeout=5,
        ), "Bob did not receive Dave's history"

        # Carol appears afterward; on_peer_appeared re-reads the channel's
        # (now-advanced) watermark fresh for this new request.
        bob.sync_mgr.on_peer_appeared(carol.identity.hash_hex)

        assert wait_for(
            lambda: all(bob.storage.message_exists(mid) for mid in carol_ids),
            timeout=5,
        ), (
            "Bob never received Carol's older, disjoint history -- her "
            "request was issued from a watermark Dave's answer had already "
            "advanced past all of Carol's messages"
        )

    def test_disjoint_history_across_two_responders_deterministic(self, peer_factory):
        """
        Deterministic reproduction of the same mechanism, without racing the
        transport thread: Dave's response is driven and applied first,
        confirming the watermark advances; Carol is then asked using that
        now-current watermark, exactly mirroring what on_peer_appeared does
        for a peer that appears afterward.
        """
        from trenchchat.core.protocol import F_CHANNEL_HASH, F_MSG_TYPE, F_SYNC_WINDOW_START
        from trenchchat.core.protocol import MT_SYNC_REQUEST

        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")
        dave = peer_factory("dave")

        ch_hash = alice.channel_mgr.create_channel("disjoint-deterministic", "", "public")
        _seed_channel_on_peer(carol, ch_hash, "disjoint-deterministic", alice.identity.hash_hex)
        _seed_channel_on_peer(dave, ch_hash, "disjoint-deterministic", alice.identity.hash_hex)
        _seed_channel_on_peer(bob, ch_hash, "disjoint-deterministic", alice.identity.hash_hex)
        bob.storage.update_last_sync(ch_hash, 0.0)

        base = time.time()
        carol_ids = [
            _insert_message(carol.storage, ch_hash, alice.identity.hash_hex,
                            f"carol {i}", base + i)
            for i in range(1, 26)
        ]
        dave_ids = [
            _insert_message(dave.storage, ch_hash, alice.identity.hash_hex,
                            f"dave {i}", base + i)
            for i in range(26, 51)
        ]

        dave_responses = []
        dave.sync_mgr._send_raw = lambda dest_hex, fields: dave_responses.append(fields) or True
        carol_responses = []
        carol.sync_mgr._send_raw = (
            lambda dest_hex, fields: carol_responses.append(fields) or True
        )

        # Bob asks Dave first, at his true (early) starting point.
        dave.sync_mgr._handle_sync_request(
            {
                F_MSG_TYPE:          MT_SYNC_REQUEST,
                F_CHANNEL_HASH:      bytes.fromhex(ch_hash),
                F_SYNC_WINDOW_START: 0.0,
            },
            ch_hash, bob.identity.hash_hex,
        )
        assert len(dave_responses) == 1, "Dave never answered the first request"

        bob.sync_mgr._record_pending_request(ch_hash, dave.identity.hash_hex, 0.0)
        bob.sync_mgr._handle_sync_response(dave_responses[0], ch_hash, dave.identity.hash_hex)

        for mid in dave_ids:
            assert bob.storage.message_exists(mid), "Bob did not accept Dave's response"

        advanced_watermark = bob.storage.get_last_sync(ch_hash)
        assert advanced_watermark >= base + 50, \
            "test setup: watermark should have advanced to Dave's newest message"

        # Bob now asks Carol using the current (Dave-advanced) watermark --
        # exactly what on_peer_appeared does for a peer appearing afterward.
        carol.sync_mgr._handle_sync_request(
            {
                F_MSG_TYPE:          MT_SYNC_REQUEST,
                F_CHANNEL_HASH:      bytes.fromhex(ch_hash),
                F_SYNC_WINDOW_START: advanced_watermark,
            },
            ch_hash, bob.identity.hash_hex,
        )
        assert len(carol_responses) == 1, "Carol never answered the second request"

        bob.sync_mgr._record_pending_request(ch_hash, carol.identity.hash_hex,
                                             advanced_watermark)
        bob.sync_mgr._handle_sync_response(carol_responses[0], ch_hash, carol.identity.hash_hex)

        for mid in carol_ids:
            assert bob.storage.message_exists(mid), (
                "Bob never received Carol's disjoint older history -- her "
                "response was computed from the watermark Dave's answer had "
                "already advanced past everything Carol holds"
            )


# ---------------------------------------------------------------------------
# A2 -- two responders answer the same round with identical history
# ---------------------------------------------------------------------------

class TestSameRoundDuplicateAnswers:
    def test_two_responders_same_messages_deduped(self, peer_factory):
        """
        Carol and Dave both hold the same 10 messages. Bob asking both must
        not duplicate anything: each message is stored exactly once, the
        received_count in sync status reflects distinct messages, and the
        channel settles to synced.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")
        dave = peer_factory("dave")

        ch_hash = alice.channel_mgr.create_channel("dup-round", "", "public")
        _seed_channel_on_peer(carol, ch_hash, "dup-round", alice.identity.hash_hex)
        _seed_channel_on_peer(dave, ch_hash, "dup-round", alice.identity.hash_hex)
        _seed_channel_on_peer(bob, ch_hash, "dup-round", alice.identity.hash_hex)
        bob.subscription_mgr._subscribers[ch_hash] = {
            carol.identity.hash_hex, dave.identity.hash_hex,
        }

        window_start = time.time()
        shared_ids = []
        for i in range(10):
            ts = window_start + i + 1
            content = f"shared {i}"
            mid_c = _insert_message(carol.storage, ch_hash, alice.identity.hash_hex,
                                    content, ts)
            mid_d = _insert_message(dave.storage, ch_hash, alice.identity.hash_hex,
                                    content, ts)
            assert mid_c == mid_d
            shared_ids.append(mid_c)

        bob.sync_mgr._request_sync_for_channel(ch_hash, window_start)

        assert wait_for(
            lambda: all(bob.storage.message_exists(mid) for mid in shared_ids),
            timeout=5,
        ), "Bob did not receive the shared messages from either responder"

        time.sleep(0.5)  # let the duplicate response also land

        stored = bob.storage.get_messages(ch_hash)
        assert len(stored) == 10, f"expected 10 deduped messages, got {len(stored)}"
        assert len({m["message_id"] for m in stored}) == 10

        status = bob.sync_mgr.status.get_status(ch_hash)
        assert status["received_count"] == 10, (
            f"received_count should count each message once, got "
            f"{status['received_count']}"
        )
        assert status["state"] == SyncState.SYNCED.value


# ---------------------------------------------------------------------------
# A3 -- one responder truncates, the other doesn't
# ---------------------------------------------------------------------------

class TestTruncationAcrossResponders:
    def test_one_responder_truncates_other_does_not(self, peer_factory):
        """
        Carol holds 60 messages (forcing a truncated batch plus a
        continuation), Dave holds 5. Bob must end with the union of both,
        and the channel must settle to synced rather than sticking on
        incomplete -- continuation budgets are scoped per (channel, peer),
        so Dave answering plainly must not disturb Carol's chain.
        """
        from trenchchat.core.sync import MAX_RESPONSE_MESSAGES

        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")
        dave = peer_factory("dave")

        ch_hash = alice.channel_mgr.create_channel("trunc-mix", "", "public")
        _seed_channel_on_peer(carol, ch_hash, "trunc-mix", alice.identity.hash_hex)
        _seed_channel_on_peer(dave, ch_hash, "trunc-mix", alice.identity.hash_hex)
        _seed_channel_on_peer(bob, ch_hash, "trunc-mix", alice.identity.hash_hex)
        bob.subscription_mgr._subscribers[ch_hash] = {
            carol.identity.hash_hex, dave.identity.hash_hex,
        }

        window_start = time.time()
        carol_total = MAX_RESPONSE_MESSAGES + 10
        carol_ids = [
            _insert_message(carol.storage, ch_hash, alice.identity.hash_hex,
                            f"carol {i}", window_start + i + 1)
            for i in range(carol_total)
        ]
        dave_ids = [
            _insert_message(dave.storage, ch_hash, alice.identity.hash_hex,
                            f"dave {i}", window_start + carol_total + i + 1)
            for i in range(5)
        ]

        bob.sync_mgr._request_sync_for_channel(ch_hash, window_start)

        all_ids = carol_ids + dave_ids
        assert wait_for(
            lambda: all(bob.storage.message_exists(mid) for mid in all_ids),
            timeout=10,
        ), "Bob did not end up with the union of both responders' history"

        assert wait_for(
            lambda: bob.sync_mgr.status.get_state(ch_hash) == SyncState.SYNCED,
            timeout=5,
        ), (
            "channel did not settle to synced after the truncated chain "
            f"completed, got {bob.sync_mgr.status.get_state(ch_hash)}"
        )


# ---------------------------------------------------------------------------
# A4 -- deep-sync cooldown is scoped per responder
# ---------------------------------------------------------------------------

class TestDeepSyncCooldownPerResponder:
    def test_cooldown_is_scoped_to_each_responder(self, peer_factory):
        """
        Carol throttles Bob's second deep request within the cooldown, but
        that must not affect Dave: the cooldown map lives on each responder's
        own SyncManager instance, so Bob can still get an immediate deep
        answer from Dave.
        """
        from trenchchat.core.protocol import F_MSG_TYPE, MT_SYNC_RESPONSE
        from trenchchat.core.sync import SYNC_WINDOW_SECS

        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")
        dave = peer_factory("dave")

        ch_hash = alice.channel_mgr.create_channel("deep-per-responder", "", "public")
        _seed_channel_on_peer(carol, ch_hash, "deep-per-responder", alice.identity.hash_hex)
        _seed_channel_on_peer(dave, ch_hash, "deep-per-responder", alice.identity.hash_hex)
        _seed_channel_on_peer(bob, ch_hash, "deep-per-responder", alice.identity.hash_hex)

        old_ts = time.time() - SYNC_WINDOW_SECS - 3600
        carol_id = _insert_message(carol.storage, ch_hash, alice.identity.hash_hex,
                                   "ancient on carol", old_ts)
        dave_id = _insert_message(dave.storage, ch_hash, alice.identity.hash_hex,
                                  "ancient on dave", old_ts)

        carol_responses = []
        original_carol_send = carol.sync_mgr._send_raw

        def carol_capture(dest_hex, fields):
            if fields.get(F_MSG_TYPE) == MT_SYNC_RESPONSE:
                carol_responses.append(fields)
            return original_carol_send(dest_hex, fields)

        carol.sync_mgr._send_raw = carol_capture

        bob.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, old_ts - 10)
        assert wait_for_message(bob.storage, ch_hash, carol_id, timeout=5)
        assert len(carol_responses) == 1

        bob.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, old_ts - 10)
        time.sleep(0.5)
        assert len(carol_responses) == 1, \
            "a second deep request within the cooldown was answered by Carol"

        bob.sync_mgr._send_sync_request(dave.identity.hash_hex, ch_hash, old_ts - 10)
        assert wait_for_message(bob.storage, ch_hash, dave_id, timeout=5), (
            "Dave was incorrectly throttled by Carol's cooldown"
        )


# ---------------------------------------------------------------------------
# A5 -- every responder throttles in the same round
# ---------------------------------------------------------------------------

class TestDeepSyncAllThrottled:
    def test_all_responders_throttled_in_same_round(self, peer_factory):
        """
        Carol and Dave both answer Bob's first deep request, then both
        throttle an immediate repeat. Every responder producing silence in
        the same round must not be reported as an ordinary, fully-synced
        channel -- a deep re-request that got dropped everywhere is not the
        same as one that was answered and had nothing more to say.
        """
        from trenchchat.core.protocol import F_MSG_TYPE, MT_SYNC_RESPONSE

        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")
        dave = peer_factory("dave")

        ch_hash = alice.channel_mgr.create_channel("all-throttled", "", "public")
        _seed_channel_on_peer(carol, ch_hash, "all-throttled", alice.identity.hash_hex)
        _seed_channel_on_peer(dave, ch_hash, "all-throttled", alice.identity.hash_hex)
        _seed_channel_on_peer(bob, ch_hash, "all-throttled", alice.identity.hash_hex)

        responses = {"carol": [], "dave": []}
        for name, peer in (("carol", carol), ("dave", dave)):
            original = peer.sync_mgr._send_raw

            def make_capture(name=name, original=original):
                def capture(dest_hex, fields):
                    if fields.get(F_MSG_TYPE) == MT_SYNC_RESPONSE:
                        responses[name].append(fields)
                    return original(dest_hex, fields)
                return capture

            peer.sync_mgr._send_raw = make_capture()

        # Round 1: both peers answer a first deep request.
        bob.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, 0.0)
        bob.sync_mgr._send_sync_request(dave.identity.hash_hex, ch_hash, 0.0)

        assert wait_for(lambda: len(responses["carol"]) == 1, timeout=5)
        assert wait_for(lambda: len(responses["dave"]) == 1, timeout=5)
        assert wait_for(
            lambda: bob.sync_mgr.status.get_state(ch_hash) == SyncState.SYNCED,
            timeout=5,
        ), "channel did not reach synced after round 1"

        # Round 2: immediate repeat -- both are within the cooldown.
        bob.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, 0.0)
        bob.sync_mgr._send_sync_request(dave.identity.hash_hex, ch_hash, 0.0)
        time.sleep(1.0)

        assert len(responses["carol"]) == 1, "Carol answered a throttled re-request"
        assert len(responses["dave"]) == 1, "Dave answered a throttled re-request"

        status = bob.sync_mgr.status.get_status(ch_hash)
        assert status["state"] != SyncState.SYNCED.value, (
            "the channel reports 'synced' even though every responder "
            f"silently dropped a deep re-request nobody actually answered: {status}"
        )


# ---------------------------------------------------------------------------
# A6 -- a missed-delivery hint fans out to every online holder
# ---------------------------------------------------------------------------

class TestHintFanout:
    def test_hint_reaches_all_holders_message_delivered_once(self, peer_factory):
        """
        Alice fails to deliver a message to Bob while Carol, Dave, and Erin
        are online; all three store a hint. When Bob reconnects and asks
        every one of them, he must get the message exactly once, and none of
        the three should be left holding a hint for a message it already
        served him.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")
        dave = peer_factory("dave")
        erin = peer_factory("erin")

        ch_hash = alice.channel_mgr.create_channel("hint-fanout", "", "public")
        for peer in (carol, dave, erin, bob):
            _seed_channel_on_peer(peer, ch_hash, "hint-fanout", alice.identity.hash_hex)

        ts = time.time()
        content = "missed by bob"
        msg_id = _insert_message(alice.storage, ch_hash, alice.identity.hash_hex,
                                 content, ts)
        # Carol, Dave, and Erin are legitimate subscribers who actually
        # received the message; only Bob missed it.
        for peer in (carol, dave, erin):
            mid = _insert_message(peer.storage, ch_hash, alice.identity.hash_hex,
                                  content, ts)
            assert mid == msg_id

        subscriber_hashes = [
            alice.identity.hash_hex, bob.identity.hash_hex,
            carol.identity.hash_hex, dave.identity.hash_hex, erin.identity.hash_hex,
        ]
        alice.sync_mgr._on_missed_delivery_event(
            channel_hash_hex=ch_hash, missed_peer_hex=bob.identity.hash_hex,
            msg_id=msg_id, subscriber_hashes=subscriber_hashes,
        )

        for peer in (carol, dave, erin):
            assert wait_for(
                lambda p=peer: msg_id in p.storage.get_missed_message_ids(
                    ch_hash, bob.identity.hash_hex),
                timeout=5,
            ), f"{peer.name} never stored the missed-delivery hint"

        bob.subscription_mgr._subscribers[ch_hash] = {
            carol.identity.hash_hex, dave.identity.hash_hex, erin.identity.hash_hex,
        }
        # Window start is past the message's own timestamp, so only the
        # hint -- not the plain timestamp sweep -- can resolve it.
        bob.sync_mgr._request_sync_for_channel(ch_hash, ts + 1)

        assert wait_for_message(bob.storage, ch_hash, msg_id, timeout=5), \
            "Bob never received the message from any of the three hint holders"

        time.sleep(0.5)
        stored = [m for m in bob.storage.get_messages(ch_hash) if m["message_id"] == msg_id]
        assert len(stored) == 1, f"expected exactly one copy on Bob's side, got {len(stored)}"

        for peer in (carol, dave, erin):
            assert wait_for(
                lambda p=peer: msg_id not in p.storage.get_missed_message_ids(
                    ch_hash, bob.identity.hash_hex),
                timeout=5,
            ), (
                f"{peer.name} still holds a stale missed-delivery hint for Bob "
                "after successfully serving the message"
            )


# ---------------------------------------------------------------------------
# A7 -- collectively complete, individually behind the watermark
# ---------------------------------------------------------------------------

class TestCollectivelyCompleteIndividuallyEmpty:
    def test_hints_rescue_history_past_the_watermark(self, peer_factory):
        """
        Carol holds messages 1-10, Dave holds 11-20. Bob's watermark is
        already at 20 (as if he'd been told he was caught up), but he never
        actually got 1-10 -- recorded as hints on Carol. A plain timestamp
        sweep from watermark 20 would find nothing on either responder; only
        the hint path can recover 1-10.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")
        dave = peer_factory("dave")

        ch_hash = alice.channel_mgr.create_channel("collectively-complete", "", "public")
        _seed_channel_on_peer(carol, ch_hash, "collectively-complete",
                              alice.identity.hash_hex)
        _seed_channel_on_peer(dave, ch_hash, "collectively-complete",
                              alice.identity.hash_hex)
        _seed_channel_on_peer(bob, ch_hash, "collectively-complete",
                              alice.identity.hash_hex)

        base = time.time()
        carol_ids = []
        for i in range(1, 11):
            mid = _insert_message(carol.storage, ch_hash, alice.identity.hash_hex,
                                  f"carol {i}", base + i)
            carol.storage.record_missed_delivery(ch_hash, bob.identity.hash_hex, mid)
            carol_ids.append(mid)

        for i in range(11, 21):
            _insert_message(dave.storage, ch_hash, alice.identity.hash_hex,
                            f"dave {i}", base + i)

        watermark = base + 20
        bob.storage.update_last_sync(ch_hash, watermark)
        bob.subscription_mgr._subscribers[ch_hash] = {
            carol.identity.hash_hex, dave.identity.hash_hex,
        }

        bob.sync_mgr._request_sync_for_channel(ch_hash, watermark)

        assert wait_for(
            lambda: all(bob.storage.message_exists(mid) for mid in carol_ids),
            timeout=5,
        ), (
            "Bob's watermark already past 20 hid messages 1-10 that a plain "
            "timestamp sweep from that watermark could never reach again"
        )


# ---------------------------------------------------------------------------
# A8 -- mixed permission views across responders
# ---------------------------------------------------------------------------

class TestMixedPermissionViews:
    def test_receive_side_recheck_governs_final_transcript(self, peer_factory):
        """
        Carol's local permissions doc has already been updated to grant the
        member role full_sync; Dave's has not; Bob's has not either. Carol's
        own send-side filter believes Bob may see pre-join history and sends
        it; Dave's does not. Whichever of them Bob's response happens to
        come from, Bob's own receive-side re-check must still govern what
        actually gets stored -- so the two responders can't produce an
        inconsistent transcript on Bob's side.
        """
        from trenchchat.core.permissions import (
            FULL_SYNC, PRESET_PRIVATE, ROLE_MEMBER, SEND_MESSAGE,
        )

        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")
        dave = peer_factory("dave")

        perms = dict(PRESET_PRIVATE)
        perms[ROLE_MEMBER] = [SEND_MESSAGE]
        ch_hash = alice.channel_mgr.create_channel("mixed-perm-views", "",
                                                    permissions=perms)

        alice.invite_mgr.publish_member_list(
            ch_hash, add_members=[carol.identity.hash, dave.identity.hash])
        assert wait_for_member(alice.storage, ch_hash, carol.identity.hash_hex)
        assert wait_for_member(alice.storage, ch_hash, dave.identity.hash_hex)
        _confirm_membership(carol, ch_hash)
        _confirm_membership(dave, ch_hash)
        assert wait_for(lambda: carol.storage.is_member(ch_hash, carol.identity.hash_hex))
        assert wait_for(lambda: dave.storage.is_member(ch_hash, dave.identity.hash_hex))

        time.sleep(0.02)
        pre_join_ts = time.time()
        pre_join_content = "sent before bob joined"
        pre_join_id = _insert_message(alice.storage, ch_hash, alice.identity.hash_hex,
                                      pre_join_content, pre_join_ts)
        # Carol and Dave are legitimate members who actually received it.
        for peer in (carol, dave):
            mid = _insert_message(peer.storage, ch_hash, alice.identity.hash_hex,
                                  pre_join_content, pre_join_ts)
            assert mid == pre_join_id
        time.sleep(0.02)

        alice.invite_mgr.publish_member_list(ch_hash, add_members=[bob.identity.hash])
        assert wait_for_member(alice.storage, ch_hash, bob.identity.hash_hex)
        _confirm_membership(bob, ch_hash)
        assert wait_for(lambda: bob.storage.is_member(ch_hash, bob.identity.hash_hex))

        after_join_ts = time.time() + 100
        after_join_content = "sent after bob joined"
        after_join_id = _insert_message(alice.storage, ch_hash, alice.identity.hash_hex,
                                        after_join_content, after_join_ts)
        for peer in (carol, dave):
            mid = _insert_message(peer.storage, ch_hash, alice.identity.hash_hex,
                                  after_join_content, after_join_ts)
            assert mid == after_join_id

        # Carol's local permissions doc has already propagated the grant;
        # Dave's and Bob's have not -- a real window during eventual
        # consistency of a permissions broadcast.
        updated_perms = dict(perms)
        updated_perms[ROLE_MEMBER] = [SEND_MESSAGE, FULL_SYNC]
        carol.storage.set_channel_permissions(ch_hash, updated_perms)

        bob.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, 0.0)
        bob.sync_mgr._send_sync_request(dave.identity.hash_hex, ch_hash, 0.0)

        assert wait_for_message(bob.storage, ch_hash, after_join_id, timeout=5), \
            "Bob never received a message sent after he actually joined"

        time.sleep(1.0)
        assert not bob.storage.message_exists(pre_join_id), (
            "Bob received pre-join history via Carol even though his own "
            "local permissions still deny full_sync -- the receive-side "
            "re-check in _handle_sync_response did not govern what was stored"
        )


# ---------------------------------------------------------------------------
# A7 -- a responder that acquires older history after the requester's
#       watermark has already passed it
# ---------------------------------------------------------------------------

class TestResponderAcquiresOlderHistoryLater:
    def test_mutual_sync_does_not_disable_the_trust_horizon(self, peer_factory):
        """
        A responder widens its sweep by PEER_TRUST_HORIZON_SECS behind the
        requester's claimed window_start, so history it picked up after that
        watermark was set is still served.

        That widening is computed as max(own_progress, window_start - horizon),
        where own_progress is how far this responder has synced *from* the
        requester -- the opposite direction to what it is about to serve them.
        Once two peers have synced from each other, own_progress is recent
        enough to swallow the widening, and anything older than the requester's
        watermark is stranded on both sides for good.

        Reproduces the four-way partition case in
        docs/testenv-scenarios.md (sync11), where every peer ends up missing
        precisely the oldest message each other peer wrote while partitioned.
        """
        from trenchchat.core.protocol import F_CHANNEL_HASH, F_MSG_TYPE, F_SYNC_WINDOW_START
        from trenchchat.core.protocol import MT_SYNC_REQUEST

        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = alice.channel_mgr.create_channel("late-older-history", "", "public")
        _seed_channel_on_peer(bob, ch_hash, "late-older-history", alice.identity.hash_hex)
        _seed_channel_on_peer(carol, ch_hash, "late-older-history", alice.identity.hash_hex)

        base = time.time()

        # Carol holds a message older than where Bob's watermark with her sits.
        # In the partition case this is history Carol only picked up after Bob
        # had already synced her newer messages.
        stranded_id = _insert_message(carol.storage, ch_hash, alice.identity.hash_hex,
                                      "written during the partition", base + 10)

        # Bob has already synced Carol's newer history.
        bob_watermark = base + 40
        bob.storage.advance_peer_sync_progress(ch_hash, carol.identity.hash_hex,
                                               bob_watermark)

        # ... and Carol has synced *from* Bob, which is the direction
        # own_progress actually tracks.
        carol.storage.advance_peer_sync_progress(ch_hash, bob.identity.hash_hex,
                                                 base + 60)

        carol_responses = []
        carol.sync_mgr._send_raw = (
            lambda dest_hex, fields: carol_responses.append(fields) or True
        )

        carol.sync_mgr._handle_sync_request(
            {
                F_MSG_TYPE:          MT_SYNC_REQUEST,
                F_CHANNEL_HASH:      bytes.fromhex(ch_hash),
                F_SYNC_WINDOW_START: bob_watermark,
            },
            ch_hash, bob.identity.hash_hex,
        )
        assert len(carol_responses) == 1, "Carol never answered the request"

        bob.sync_mgr._record_pending_request(ch_hash, carol.identity.hash_hex,
                                             bob_watermark)
        bob.sync_mgr._handle_sync_response(carol_responses[0], ch_hash,
                                           carol.identity.hash_hex)

        assert bob.storage.message_exists(stranded_id), (
            "Bob never received history older than his watermark with Carol -- "
            "Carol's own sync progress *from* Bob suppressed the trust-horizon "
            "widening that should have covered it"
        )


# ---------------------------------------------------------------------------
# A8 -- a batch where one row is refused and a newer one is accepted
# ---------------------------------------------------------------------------

class TestRejectedRowBoundsTheWatermark:
    def test_unverifiable_row_is_not_skipped_past(self, peer_factory):
        """
        A row dropped for an unverifiable author signature must hold the
        watermark back, the same as a row whose insert failed.

        "Unverifiable" and "forged" are the same answer from verify_message:
        an author whose public key this peer has never learned fails the check
        exactly like a tampered row does. The first is ordinary -- the author
        may simply be offline -- and the key usually arrives later. But if the
        watermark advances past the refused row in the meantime,
        get_messages_after's strict `>` hides it from every future sweep, so
        honest history is lost permanently on the strength of a key that was
        merely late.
        """
        import msgpack

        from trenchchat.core.authorship import sign_message
        from trenchchat.core.protocol import (
            F_CHANNEL_HASH, F_MSG_TYPE, F_SYNC_MESSAGES, MT_SYNC_RESPONSE,
        )

        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = alice.channel_mgr.create_channel("late-key", "", "public")
        _seed_channel_on_peer(bob, ch_hash, "late-key", alice.identity.hash_hex)

        base = time.time() - 60
        alice_hex = alice.identity.hash_hex
        bob.storage.remember_identity_key(
            alice_hex, alice.identity.rns_identity.get_public_key()
        )

        def row(content, ts, signed):
            msg_id = _compute_message_id(content, alice_hex, ts)
            sig = sign_message(alice.identity.rns_identity, ch_hash, msg_id, ts,
                               content, None, None, None) if signed else b"\x00" * 64
            return {"message_id": msg_id, "sender_hash": alice_hex,
                    "sender_name": "Alice", "content": content, "timestamp": ts,
                    "reply_to": None, "last_seen_id": None, "author_sig": sig}

        refused = row("author key not learned yet", base + 10, signed=False)
        accepted = row("verifiable and newer", base + 20, signed=True)

        bob.sync_mgr._record_pending_request(ch_hash, carol.identity.hash_hex, base)
        bob.sync_mgr._handle_sync_response(
            {
                F_MSG_TYPE:      MT_SYNC_RESPONSE,
                F_CHANNEL_HASH:  bytes.fromhex(ch_hash),
                F_SYNC_MESSAGES: msgpack.packb([refused, accepted], use_bin_type=True),
            },
            ch_hash, carol.identity.hash_hex,
        )

        assert bob.storage.message_exists(accepted["message_id"]), (
            "the verifiable message was not stored"
        )
        assert not bob.storage.message_exists(refused["message_id"]), (
            "a message with an invalid author signature was stored"
        )
        assert bob.storage.get_last_sync(ch_hash) < refused["timestamp"], (
            "the watermark advanced past a refused message, so no future sweep "
            "will ever offer it again -- history is lost the moment its author's "
            "key turns up late"
        )


# ---------------------------------------------------------------------------
# A9 -- history whose author is no longer reachable
# ---------------------------------------------------------------------------

class TestAuthorKeyTravelsWithTheBatch:
    def test_relayed_history_stays_readable_after_its_author_leaves(
        self, peer_factory, monkeypatch
    ):
        """
        A responder sends the authors' public keys with the batch, so a
        requester who has never met an author can still verify their messages.

        Without this, a peer who leaves the network takes their history with
        them: verifying needs their public key, the only route to one is an
        announce they will never send again, and "cannot verify yet" is
        dropped exactly like "forged". Every peer who joins afterwards sees a
        transcript with that author's messages missing and nothing to say so
        (integrity2 in docs/testenv-scenarios.md).

        The relay is not trusted here -- a key is accepted only if it hashes
        back to the identity claiming it, which is what makes passing one
        through a third party safe.

        RNS.Identity.recall is stubbed out for the newcomer's half of the
        exchange because every peer in this suite shares one Reticulum
        instance, so recall always succeeds here and the departure being
        modelled -- an author whose announce can no longer be resolved --
        cannot otherwise be reached in-process.
        """
        from trenchchat.core.authorship import sign_message
        from trenchchat.core.protocol import (
            F_CHANNEL_HASH, F_MSG_TYPE, F_SYNC_WINDOW_START, MT_SYNC_REQUEST,
        )

        author = peer_factory("author")
        relay = peer_factory("relay")
        newcomer = peer_factory("newcomer")

        ch_hash = author.channel_mgr.create_channel("outlives-author", "", "public")
        _seed_channel_on_peer(relay, ch_hash, "outlives-author", author.identity.hash_hex)
        _seed_channel_on_peer(newcomer, ch_hash, "outlives-author",
                              author.identity.hash_hex)

        ts = time.time() - 30
        author_hex = author.identity.hash_hex
        content = "written before the author left"
        msg_id = _compute_message_id(content, author_hex, ts)
        signature = sign_message(author.identity.rns_identity, ch_hash, msg_id, ts,
                                 content, None, None, None)
        relay.storage.insert_message(
            channel_hash=ch_hash, sender_hash=author_hex, sender_name="Author",
            content=content, timestamp=ts, message_id=msg_id, reply_to=None,
            last_seen_id=None, received_at=ts, author_sig=signature,
        )
        # The relay knows the author; the newcomer never has.
        relay.storage.remember_identity_key(
            author_hex, author.identity.rns_identity.get_public_key()
        )
        assert newcomer.storage.get_identity_key(author_hex) is None

        responses = []
        relay.sync_mgr._send_raw = (
            lambda dest_hex, fields: responses.append(fields) or True
        )
        relay.sync_mgr._handle_sync_request(
            {
                F_MSG_TYPE:          MT_SYNC_REQUEST,
                F_CHANNEL_HASH:      bytes.fromhex(ch_hash),
                F_SYNC_WINDOW_START: ts - 60,
            },
            ch_hash, newcomer.identity.hash_hex,
        )
        assert len(responses) == 1, "the relay never answered"

        monkeypatch.setattr(RNS.Identity, "recall", staticmethod(lambda *a, **k: None))
        newcomer.sync_mgr._record_pending_request(ch_hash, relay.identity.hash_hex,
                                                  ts - 60)
        newcomer.sync_mgr._handle_sync_response(responses[0], ch_hash,
                                                relay.identity.hash_hex)

        assert newcomer.storage.message_exists(msg_id), (
            "a peer that never met the author lost their history -- the key "
            "did not travel with the batch"
        )
        assert newcomer.storage.get_identity_key(author_hex) is not None, (
            "the author's key was used but not kept, so the next batch pays for "
            "it again"
        )

    def test_a_key_that_is_not_the_authors_is_refused(self, peer_factory):
        """A relay cannot substitute its own key for an author's.

        The signature check would otherwise pass against whatever key the
        relay supplied, which would let any relay rewrite any message it
        passes on and sign it as someone else.
        """
        from trenchchat.core.authorship import remember_relayed_key

        author = peer_factory("author")
        impostor = peer_factory("impostor")
        receiver = peer_factory("receiver")

        accepted = remember_relayed_key(
            receiver.storage, author.identity.hash_hex,
            impostor.identity.rns_identity.get_public_key(),
        )
        assert accepted is False, "a key that is not the author's was cached as theirs"
        assert receiver.storage.get_identity_key(author.identity.hash_hex) is None

    def test_an_oversized_author_key_map_is_refused_whole(self, peer_factory):
        """
        The map is the one bulk container carried as a bare LXMF dict rather
        than as bytes through unpack_wire, so nothing else bounds it -- and an
        identity hash is derived *from* its key, so valid pairs cost a hash
        rather than a keypair.
        """
        import hashlib

        from trenchchat.core.sync import MAX_AUTHOR_KEYS

        receiver = peer_factory("receiver")
        flood = {}
        for i in range(MAX_AUTHOR_KEYS + 1):
            key = hashlib.sha512(str(i).encode()).digest()[:64]
            flood[hashlib.sha256(key).hexdigest()[:32]] = key

        receiver.sync_mgr._learn_author_keys(flood)

        assert all(receiver.storage.get_identity_key(h) is None for h in flood), \
            "an unbounded author-key map was ingested"

    def test_a_non_string_author_key_does_not_raise(self, peer_factory):
        """It reaches a log line that slices the key; an int aborted the whole
        response after its pending request had already been claimed."""
        receiver = peer_factory("receiver")
        receiver.sync_mgr._learn_author_keys({1: b"\x00" * 64})
        receiver.sync_mgr._learn_author_keys({b"not-hex": b"\x00" * 64})


# ---------------------------------------------------------------------------
# A10 -- the answer to a request from a peer we cannot yet address
# ---------------------------------------------------------------------------

class TestSyncAnswerSurvivesAnUnresolvedPath:
    def test_a_response_is_held_and_re_sent_rather_than_dropped(self, peer_factory,
                                                                monkeypatch):
        """
        A responder that cannot yet address the requester holds its answer.

        This is the shape of a peer returning from an outage: it asks everyone
        for what it missed, and each responder can read the request but cannot
        resolve a path back yet. The answer used to be dropped on the floor --
        without even requesting a path -- so the requester sat at `pending`
        forever, unable to tell silence from a refusal, and nothing re-sent
        anything (sync2 in docs/testenv-scenarios.md).
        """
        from trenchchat.core.protocol import (
            F_CHANNEL_HASH, F_MSG_TYPE, F_SYNC_WINDOW_START, MT_SYNC_REQUEST,
        )

        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("answer-held", "", "public")
        _seed_channel_on_peer(bob, ch_hash, "answer-held", alice.identity.hash_hex)

        ts = time.time() - 30
        _insert_message(alice.storage, ch_hash, alice.identity.hash_hex,
                        "sent while bob was away", ts)

        monkeypatch.setattr(RNS.Identity, "recall", staticmethod(lambda *a, **k: None))
        alice.sync_mgr._handle_sync_request(
            {
                F_MSG_TYPE:          MT_SYNC_REQUEST,
                F_CHANNEL_HASH:      bytes.fromhex(ch_hash),
                F_SYNC_WINDOW_START: ts - 60,
            },
            ch_hash, bob.identity.hash_hex,
        )
        assert alice.sync_mgr._retry.pending_for(bob.identity.hash_hex) == 1, (
            "the answer was dropped instead of being held until bob is addressable"
        )

        monkeypatch.undo()
        bob.sync_mgr._record_pending_request(ch_hash, alice.identity.hash_hex, ts - 60)
        alice.sync_mgr.on_peer_appeared(bob.identity.hash_hex)

        assert wait_for(lambda: bob.storage.get_messages(ch_hash), timeout=5), (
            "the held answer never reached the requester once its path resolved"
        )


# ---------------------------------------------------------------------------
# A11 -- a request that nothing will ever ask again
# ---------------------------------------------------------------------------

class TestUnansweredRequestIsAskedAgain:
    def test_a_request_nobody_answered_is_asked_again(self, peer_factory, monkeypatch):
        """
        A sync request that goes unanswered is asked again.

        Every trigger in the app is an event -- a peer announcing, or this
        node's own link returning -- and both fire in a burst and then stop,
        because Reticulum suppresses announce replays for a destination it has
        already propagated. A responder's deep-sync cooldown refuses silently
        and lasts a minute, which is long enough to swallow an entire burst.
        The requester then waits forever on an answer nobody will send: in
        sync2 the returning peer asked eight times in thirty seconds, was
        refused every time, and never asked again in the three minutes left.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("silent-refusal", "", "public")
        _seed_channel_on_peer(bob, ch_hash, "silent-refusal", alice.identity.hash_hex)

        asked = []
        real_send = bob.sync_mgr._send_sync_request

        def record(dest_hex, channel_hash_hex, since_ts, continuation=False):
            asked.append((dest_hex, channel_hash_hex))
            return real_send(dest_hex, channel_hash_hex, since_ts, continuation)

        monkeypatch.setattr(bob.sync_mgr, "_send_sync_request", record)

        bob.sync_mgr._record_pending_request(ch_hash, alice.identity.hash_hex, 0.0)

        bob.sync_mgr.tick()
        assert asked == [], "a request was re-asked before it was old enough"

        # Age it out by shortening the window rather than moving the clock,
        # which every other manager in this process also reads.
        monkeypatch.setattr("trenchchat.core.sync.SYNC_RETRY_SECS", 0.0)
        bob.sync_mgr.tick()

        assert [c for _, c in asked] == [ch_hash], (
            "an unanswered request was never asked again, so a peer refused "
            "once waits forever"
        )
        assert asked[0][0] == alice.identity.hash_hex

    def test_the_retry_actually_recovers_the_history(self, peer_factory, monkeypatch):
        """End to end: the retry is what delivers, with nothing else running."""
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("silent-refusal-e2e", "", "public")
        _seed_channel_on_peer(bob, ch_hash, "silent-refusal-e2e",
                              alice.identity.hash_hex)
        _insert_message(alice.storage, ch_hash, alice.identity.hash_hex,
                        "sent while bob was away", time.time() - 30)

        bob.sync_mgr._record_pending_request(ch_hash, alice.identity.hash_hex, 0.0)
        assert not bob.storage.get_messages(ch_hash)

        monkeypatch.setattr("trenchchat.core.sync.SYNC_RETRY_SECS", 0.0)
        bob.sync_mgr.tick()

        assert wait_for(lambda: bob.storage.get_messages(ch_hash), timeout=5), (
            "the retry never reached the responder"
        )


# ---------------------------------------------------------------------------
# A peer that never answers must not be re-asked forever
# ---------------------------------------------------------------------------

class TestSyncRetryIsBounded:
    """tick() re-asks whoever did not answer.

    Our progress with a peer never advances while they stay silent, so every
    re-ask is a whole-transcript request -- and one peer that keeps its path
    resolvable and simply never replies would otherwise draw one out of every
    member of the channel, at a flat interval, indefinitely.
    """

    def test_re_asks_stop_after_the_budget(self, peer_factory, monkeypatch):
        from trenchchat.core.sync import MAX_SYNC_RETRIES

        alice = peer_factory("alice")
        ch_hash = alice.channel_mgr.create_channel("public", "", "public")
        silent = "ab" * 16

        sent = []
        monkeypatch.setattr(alice.sync_mgr, "_send_raw",
                            lambda dest, fields: sent.append(dest) or True)

        real_time = time.time
        clock = {"now": real_time()}
        monkeypatch.setattr("trenchchat.core.sync.time.time",
                            lambda: clock["now"])

        alice.sync_mgr._send_sync_request(silent, ch_hash, 0.0)
        for _ in range(MAX_SYNC_RETRIES + 5):
            # Far enough ahead to clear any backoff the count has grown to.
            clock["now"] += 10 ** 6
            alice.sync_mgr.tick()

        assert len(sent) <= MAX_SYNC_RETRIES + 1, (
            f"{len(sent)} requests sent to a peer that never answered"
        )

    def test_a_sync_request_is_not_held_in_the_generic_retry_queue(
            self, peer_factory, monkeypatch):
        """The announce path re-issues it through _send_sync_request, which is
        what records a claimable pending entry; a queued copy arrives with
        none, so its answer is dropped while still burning the responder's
        deep-sync budget for that pair."""
        alice = peer_factory("alice")
        ch_hash = alice.channel_mgr.create_channel("public", "", "public")
        unreachable = "cd" * 16

        monkeypatch.setattr("trenchchat.core.sync.RNS.Identity.recall",
                            lambda h: None)
        monkeypatch.setattr("trenchchat.core.sync.RNS.Transport.request_path",
                            lambda h: None)

        alice.sync_mgr._send_sync_request(unreachable, ch_hash, 0.0)

        assert alice.sync_mgr._retry.pending_for(unreachable) == 0, \
            "a sync request was held in the generic retry queue"
