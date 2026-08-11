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
    wait_for_member,
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

        ch_hash = alice.channel_mgr.create_channel("capped-sync", "", "public")
        _seed_channel_on_peer(carol, ch_hash, "capped-sync", alice.identity.hash_hex)
        _seed_channel_on_peer(bob, ch_hash, "capped-sync", alice.identity.hash_hex)

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

        last_sync_at = bob.storage.get_subscriptions()[0]["last_sync_at"]
        expected_ts = window_start + MAX_RESPONSE_MESSAGES
        assert abs(last_sync_at - expected_ts) < 1, (
            "last_sync_at did not advance to the last delivered message's "
            f"timestamp: expected ~= {expected_ts}, got {last_sync_at}"
        )

        bob.sync_mgr._send_sync_request(carol.identity.hash_hex, ch_hash, last_sync_at)

        for mid in msg_ids[MAX_RESPONSE_MESSAGES:]:
            assert wait_for_message(bob.storage, ch_hash, mid, timeout=5), \
                f"Bob never received message {mid[:12]}… stranded past the cap"

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

        def spy(dest_hex, channel_hash_hex, since_ts):
            sync_requests_seen.append((dest_hex, channel_hash_hex))
            return orig_send_sync_request(dest_hex, channel_hash_hex, since_ts)

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

from trenchchat.core.permissions import (
    FULL_SYNC, PRESET_PRIVATE, ROLE_ADMIN, ROLE_MEMBER, ROLE_OWNER, SEND_MESSAGE,
)


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

        ch_hash = alice.channel_mgr.create_channel("img-sync", "", "public")
        _seed_channel_on_peer(bob, ch_hash, "img-sync", alice.identity.hash_hex)

        # Timestamped after channel creation, not before -- create_channel()
        # opens the owner's own tenure at creation time, so a message
        # "sent" earlier than that would describe an impossible timeline
        # (Alice sending in a channel before it existed) and gets correctly
        # rejected as untenured by the sync tenure filter.
        ts = time.time()
        msg_id = alice.storage.get_messages(ch_hash)
        # Insert directly with image data
        alice.storage.insert_message(
            channel_hash=ch_hash,
            sender_hash=alice.identity.hash_hex,
            sender_name="Alice",
            content="synced image",
            timestamp=ts,
            message_id="sync_img_001",
            reply_to=None,
            last_seen_id=None,
            received_at=ts,
            image_data=_FAKE_JPEG,
        )

        bob.sync_mgr._send_sync_request(alice.identity.hash_hex, ch_hash, ts - 100)

        assert wait_for_message(bob.storage, ch_hash, "sync_img_001", timeout=5), \
            "Bob did not receive the synced image message"

        bob_msgs = bob.storage.get_messages(ch_hash)
        synced = next(m for m in bob_msgs if m["message_id"] == "sync_img_001")
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
