"""
Integration tests for message send/receive between peers.

Uses TestTransport (from conftest) for in-process delivery.
"""

import time

import pytest

from tests.helpers import (
    wait_for,
    wait_for_message,
)
from trenchchat.core.messaging import _compute_message_id


class TestSendReceive:
    def test_send_receive_message(self, peer_factory):
        """
        Alice creates a public channel, Bob subscribes, Alice sends a message;
        Bob's storage receives it via TestTransport.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("chat", "", "public")
        bob.storage.upsert_channel(ch_hash, "chat", "", alice.identity.hash_hex,
                                   "public", time.time())
        bob.storage.subscribe(ch_hash)

        content = "Hello Bob!"
        alice.messaging.send_message(
            channel_hash_hex=ch_hash,
            content=content,
            subscriber_hashes=[bob.identity.hash_hex],
        )

        # Alice stores her own message immediately
        alice_msgs = alice.storage.get_messages(ch_hash)
        assert len(alice_msgs) == 1
        msg_id = alice_msgs[0]["message_id"]

        # Bob receives it via TestTransport
        assert wait_for_message(bob.storage, ch_hash, msg_id, timeout=5), \
            "Bob did not receive Alice's message"

        msg = bob.storage.get_messages(ch_hash)[0]
        assert msg["content"] == content
        assert msg["sender_hash"] == alice.identity.hash_hex

    def test_message_stored_locally_immediately(self, peer_factory):
        """
        The sender's own message is stored in their local DB as part of
        send_message, even when all subscribers are filtered out (self is
        skipped in the delivery loop but the local insert still happens).
        """
        alice = peer_factory("alice")
        ch_hash = alice.channel_mgr.create_channel("local", "", "public")

        content = "Stored locally"
        alice.messaging.send_message(
            channel_hash_hex=ch_hash,
            content=content,
            subscriber_hashes=[alice.identity.hash_hex],  # self is skipped in loop
        )

        msgs = alice.storage.get_messages(ch_hash)
        assert len(msgs) == 1, "Alice's message was not stored locally"
        assert msgs[0]["content"] == content
        assert msgs[0]["sender_hash"] == alice.identity.hash_hex

    def test_message_idempotency(self, peer_factory):
        """
        Inserting the same message_id twice results in only one stored copy.
        """
        alice = peer_factory("alice")
        ch_hash = alice.channel_mgr.create_channel("idem", "", "public")

        ts = time.time()
        kwargs = dict(
            channel_hash=ch_hash,
            sender_hash=alice.identity.hash_hex,
            sender_name="Alice",
            content="Duplicate",
            timestamp=ts,
            message_id="dup_id_001",
            reply_to=None,
            last_seen_id=None,
            received_at=ts,
        )
        r1 = alice.storage.insert_message(**kwargs)
        r2 = alice.storage.insert_message(**kwargs)

        assert r1 is True
        assert r2 is False
        assert len(alice.storage.get_messages(ch_hash)) == 1

    def test_reply_to_field(self, peer_factory):
        """
        Bob sends a reply referencing Alice's message_id via the reply_to field.
        Both peers store the reply_to reference correctly.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("replies", "", "public")
        bob.storage.upsert_channel(ch_hash, "replies", "", alice.identity.hash_hex,
                                   "public", time.time())
        bob.storage.subscribe(ch_hash)
        alice.storage.subscribe(ch_hash)

        orig_content = "Original message"
        alice.messaging.send_message(
            channel_hash_hex=ch_hash,
            content=orig_content,
            subscriber_hashes=[bob.identity.hash_hex],
        )

        assert wait_for(
            lambda: len(bob.storage.get_messages(ch_hash)) > 0,
            timeout=5,
        ), "Bob did not receive Alice's original message"
        orig_id = bob.storage.get_messages(ch_hash)[0]["message_id"]

        reply_content = "Reply to Alice"
        bob.messaging.send_message(
            channel_hash_hex=ch_hash,
            content=reply_content,
            reply_to=orig_id,
            subscriber_hashes=[alice.identity.hash_hex],
        )

        assert wait_for(
            lambda: any(
                m["reply_to"] == orig_id
                for m in alice.storage.get_messages(ch_hash)
            ),
            timeout=5,
        ), "Alice did not receive Bob's reply"

        msgs = alice.storage.get_messages(ch_hash)
        reply_msg = next((m for m in msgs if m["reply_to"] == orig_id), None)
        assert reply_msg is not None
        assert reply_msg["content"] == reply_content

    def test_message_callback_fires(self, peer_factory):
        """add_message_callback fires when a message is received."""
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("callbacks", "", "public")
        bob.storage.upsert_channel(ch_hash, "callbacks", "", alice.identity.hash_hex,
                                   "public", time.time())
        bob.storage.subscribe(ch_hash)

        received = []
        bob.messaging.add_message_callback(
            lambda ch, mid: received.append((ch, mid))
        )

        alice.messaging.send_message(
            channel_hash_hex=ch_hash,
            content="Callback test",
            subscriber_hashes=[bob.identity.hash_hex],
        )

        assert wait_for(lambda: len(received) > 0, timeout=5), \
            "message callback was not fired on Bob's side"
        assert received[0][0] == ch_hash

    def test_message_not_accepted_for_unsubscribed_channel(self, peer_factory):
        """
        A message for a channel Bob is not subscribed to is silently dropped.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("unsub-test", "", "public")
        # Bob is NOT subscribed

        content = "Should be dropped"
        alice.messaging.send_message(
            channel_hash_hex=ch_hash,
            content=content,
            subscriber_hashes=[bob.identity.hash_hex],
        )

        time.sleep(0.5)
        msgs = bob.storage.get_messages(ch_hash)
        assert len(msgs) == 0, "Bob stored a message for a channel he is not subscribed to"

    def test_multiple_subscribers_receive_message(self, peer_factory):
        """
        A message sent to multiple subscribers is delivered to all of them.
        """
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        carol = peer_factory("carol")

        ch_hash = alice.channel_mgr.create_channel("multi", "", "public")
        for peer in [bob, carol]:
            peer.storage.upsert_channel(ch_hash, "multi", "", alice.identity.hash_hex,
                                        "public", time.time())
            peer.storage.subscribe(ch_hash)

        content = "Broadcast message"
        alice.messaging.send_message(
            channel_hash_hex=ch_hash,
            content=content,
            subscriber_hashes=[bob.identity.hash_hex, carol.identity.hash_hex],
        )

        msg_id = alice.storage.get_messages(ch_hash)[0]["message_id"]

        assert wait_for_message(bob.storage, ch_hash, msg_id, timeout=5), \
            "Bob did not receive the broadcast message"
        assert wait_for_message(carol.storage, ch_hash, msg_id, timeout=5), \
            "Carol did not receive the broadcast message"
