"""
Integration tests for the subscription protocol.

Covers: subscribe/unsubscribe control messages, subscriber list broadcast,
and the owner's in-memory subscriber set.
"""

import time

import pytest

from tests.helpers import (
    wait_for,
    wait_for_subscriber,
)


class TestSubscribeUnsubscribe:
    def test_subscribe_local_only(self, peer_factory):
        """
        Calling storage.subscribe() directly marks the channel as subscribed.
        """
        alice = peer_factory("alice")
        ch_hash = alice.channel_mgr.create_channel("local-sub", "", "public")
        assert alice.storage.is_subscribed(ch_hash)

    def test_subscribe_notifies_owner(self, peer_factory):
        """
        When Bob subscribes to Alice's channel, Alice's SubscriptionManager
        receives the MT_SUBSCRIBE control message and adds Bob to her
        in-memory subscriber set.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("sub-test", "", "public")
        bob.storage.upsert_channel(ch_hash, "sub-test", "", alice.identity.hash_hex,
                                   "public", time.time())

        bob.subscription_mgr.subscribe(ch_hash, owner_hash_hex=alice.identity.hash_hex)

        assert wait_for_subscriber(alice, ch_hash, bob.identity.hash_hex, timeout=5), \
            "Alice did not receive Bob's subscribe notification"

    def test_unsubscribe_removes_from_owner(self, peer_factory):
        """
        After subscribing, Bob unsubscribes and Alice removes him from her
        subscriber set.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("unsub-test", "", "public")
        bob.storage.upsert_channel(ch_hash, "unsub-test", "", alice.identity.hash_hex,
                                   "public", time.time())

        bob.subscription_mgr.subscribe(ch_hash, owner_hash_hex=alice.identity.hash_hex)
        assert wait_for_subscriber(alice, ch_hash, bob.identity.hash_hex, timeout=5)

        bob.subscription_mgr.unsubscribe(ch_hash, owner_hash_hex=alice.identity.hash_hex)

        assert wait_for(
            lambda: bob.identity.hash_hex not in alice.subscription_mgr.get_subscribers(ch_hash),
            timeout=5,
        ), "Alice still has Bob as a subscriber after unsubscribe"

    def test_subscribe_updates_local_storage(self, peer_factory):
        """
        subscription_mgr.subscribe() persists the subscription to the local DB.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("storage-sub", "", "public")
        bob.storage.upsert_channel(ch_hash, "storage-sub", "", alice.identity.hash_hex,
                                   "public", time.time())

        assert not bob.storage.is_subscribed(ch_hash)
        bob.subscription_mgr.subscribe(ch_hash)
        assert bob.storage.is_subscribed(ch_hash)

    def test_unsubscribe_updates_local_storage(self, peer_factory):
        """
        subscription_mgr.unsubscribe() removes the subscription from the local DB.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("storage-unsub", "", "public")
        bob.storage.upsert_channel(ch_hash, "storage-unsub", "", alice.identity.hash_hex,
                                   "public", time.time())
        bob.subscription_mgr.subscribe(ch_hash)
        assert bob.storage.is_subscribed(ch_hash)

        bob.subscription_mgr.unsubscribe(ch_hash)
        assert not bob.storage.is_subscribed(ch_hash)


class TestSubscriberListBroadcast:
    def test_subscriber_list_sent_on_subscribe(self, peer_factory):
        """
        When Bob subscribes to Alice's channel, Alice broadcasts the updated
        subscriber list to all subscribers. Carol (already subscribed) receives
        the updated list containing Bob.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = alice.channel_mgr.create_channel("list-test", "", "public")

        for peer in [bob, carol]:
            peer.storage.upsert_channel(ch_hash, "list-test", "", alice.identity.hash_hex,
                                        "public", time.time())

        # Carol subscribes first
        carol.subscription_mgr.subscribe(ch_hash, owner_hash_hex=alice.identity.hash_hex)
        assert wait_for_subscriber(alice, ch_hash, carol.identity.hash_hex, timeout=5)

        # Now Bob subscribes — Alice broadcasts the updated list to Carol and Bob
        bob.subscription_mgr.subscribe(ch_hash, owner_hash_hex=alice.identity.hash_hex)
        assert wait_for_subscriber(alice, ch_hash, bob.identity.hash_hex, timeout=5)

        # Carol should receive the updated subscriber list containing Bob's identity hash
        assert wait_for(
            lambda: bob.identity.hash_hex in carol.subscription_mgr.get_subscribers(ch_hash),
            timeout=5,
        ), "Carol did not receive the updated subscriber list containing Bob"

    def test_subscriber_list_includes_owner(self, peer_factory):
        """
        Regression test: the owner is never itself added to
        SubscriptionManager._subscribers (that set only tracks peers who sent
        MT_SUBSCRIBE), but _broadcast_subscriber_list() used to send exactly
        that set verbatim -- so a subscriber's local get_subscribers() view
        never included the owner at all.

        This broke actions.compute_channel_recipients() for every non-owner
        subscriber on a public channel: their reactions (which have no
        offline-sync/backfill fallback, unlike chat messages) never listed
        the owner as a recipient and were silently never delivered to them,
        even though the owner could see the subscriber's chat messages fine
        (those happened to still arrive via the separate sync mechanism,
        masking the same underlying gap).
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("owner-visible", "", "public")
        bob.storage.upsert_channel(ch_hash, "owner-visible", "", alice.identity.hash_hex,
                                   "public", time.time())

        bob.subscription_mgr.subscribe(ch_hash, owner_hash_hex=alice.identity.hash_hex)
        assert wait_for_subscriber(alice, ch_hash, bob.identity.hash_hex, timeout=5)

        assert wait_for(
            lambda: alice.identity.hash_hex in bob.subscription_mgr.get_subscribers(ch_hash),
            timeout=5,
        ), "Bob's subscriber list never included the channel owner"

    def test_subscriber_list_rejected_from_non_owner(self, peer_factory):
        """
        A MT_SUBSCRIBER_LIST message from a non-owner is rejected.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("reject-test", "", "public")
        bob.storage.upsert_channel(ch_hash, "reject-test", "", alice.identity.hash_hex,
                                   "public", time.time())

        # Bob's subscriber set for this channel starts empty
        assert bob.subscription_mgr.get_subscribers(ch_hash) == set()

        # Bob is not the owner, so his subscriber set should remain empty
        # (The guard in _on_lxmf_message checks channel["creator_hash"] == sender_hex)
        assert bob.subscription_mgr.get_subscribers(ch_hash) == set()


class TestSubscriberVersionSurvivesRestart:
    def test_owner_restart_does_not_renumber_into_replay_rejection(self, peer_factory):
        """
        A restarted owner keeps numbering subscriber lists upward.

        The counter used to live only in memory, so a restarted owner reissued
        v1 while its subscribers still held the higher number from before.
        Receivers reject anything not newer than what they hold, so every list
        published after the restart was discarded as a replay -- leaving
        existing subscribers permanently unaware of anyone who joined
        afterwards (restart1 in docs/testenv-scenarios.md).
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = alice.channel_mgr.create_channel("version-restart", "", "public")
        for peer in (bob, carol):
            peer.storage.upsert_channel(ch_hash, "version-restart", "",
                                        alice.identity.hash_hex, "public", time.time())

        bob.subscription_mgr.subscribe(ch_hash, owner_hash_hex=alice.identity.hash_hex)
        assert wait_for_subscriber(alice, ch_hash, bob.identity.hash_hex, timeout=5)
        assert wait_for(
            lambda: bob.identity.hash_hex in bob.subscription_mgr.get_subscribers(ch_hash),
            timeout=5,
        ), "Bob never received the first subscriber list"

        issued = alice.storage.get_all_subscriber_list_versions().get(ch_hash, 0)
        assert issued > 0, "the owner never persisted a subscriber-list version"

        # Restart the owner's manager against the same storage, exactly what a
        # process restart does: memory gone, database intact.
        from trenchchat.core.subscription import SubscriptionManager
        restarted = SubscriptionManager(alice.identity, alice.storage, alice.router)
        assert restarted._next_subscriber_version(ch_hash) > issued, (
            "a restarted owner reissued a version its subscribers already hold, "
            "so every later list is rejected as a replay"
        )

        # Carol joining is what the surviving subscriber must learn about.
        carol.subscription_mgr.subscribe(ch_hash, owner_hash_hex=alice.identity.hash_hex)
        assert wait_for_subscriber(alice, ch_hash, carol.identity.hash_hex, timeout=5)
        assert wait_for(
            lambda: carol.identity.hash_hex in bob.subscription_mgr.get_subscribers(ch_hash),
            timeout=5,
        ), "Bob never learned about a peer that joined after the owner restarted"


class TestSubscribeSurvivesAnUnresolvedPath:
    def test_a_queued_subscribe_reaches_the_owner_once_the_path_resolves(
        self, peer_factory, monkeypatch
    ):
        """
        MT_SUBSCRIBE is held and re-sent, rather than dropped, when the
        owner's path is not yet known.

        A join is the very first thing a peer does on a channel, and it is
        exactly when a path is least likely to be resolved. The message used
        to be dropped outright -- no queue, no retry, no error -- so the owner
        never learned of the subscriber, the subscriber was silently absent
        from every send, and only joining a second time recovered (restart3 in
        docs/testenv-scenarios.md).
        """
        import RNS

        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("cold-path", "", "public")
        bob.storage.upsert_channel(ch_hash, "cold-path", "",
                                   alice.identity.hash_hex, "public", time.time())

        monkeypatch.setattr(RNS.Identity, "recall", staticmethod(lambda *a, **k: None))
        bob.subscription_mgr.subscribe(ch_hash, owner_hash_hex=alice.identity.hash_hex)

        assert bob.subscription_mgr._retry.pending_for(alice.identity.hash_hex) == 1, (
            "the subscribe was dropped instead of being held for retry"
        )
        assert not wait_for(
            lambda: bob.identity.hash_hex in alice.subscription_mgr.get_subscribers(ch_hash),
            timeout=1,
        ), "the owner registered a subscriber whose message could not be sent"

        monkeypatch.undo()
        assert bob.subscription_mgr.flush_pending(alice.identity.hash_hex) == 1

        assert wait_for_subscriber(alice, ch_hash, bob.identity.hash_hex, timeout=5), (
            "the owner never learned of a subscriber whose queued join was flushed"
        )
