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
