"""
Integration tests for message send/receive between peers.

Uses TestTransport (from conftest) for in-process delivery.
"""

import time

import pytest
import RNS

from tests.helpers import (
    wait_for,
    wait_for_message,
)
from trenchchat.core.messaging import (
    _compute_message_id,
    DELIVERY_PENDING, DELIVERY_DELIVERED, DELIVERY_FAILED,
)
from trenchchat.core.permissions import PRESET_PRIVATE, SEND_MESSAGE


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


class TestSendMessagePermission:
    def test_message_dropped_when_sender_lacks_send_permission(self, peer_factory):
        """A message from a member whose send_message permission has been revoked
        must be silently dropped by the receiver."""
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        # Alice creates an invite-only channel and adds Bob as a member.
        ch_hash = alice.channel_mgr.create_channel("restricted", "", "invite")
        alice.invite_mgr.publish_member_list(
            ch_hash, add_members=[bob.identity.hash]
        )

        from tests.helpers import wait_for_member
        assert wait_for_member(alice.storage, ch_hash, bob.identity.hash_hex)

        # Bob's storage also needs the channel and membership so his receiver accepts it.
        bob.storage.upsert_channel(ch_hash, "restricted", "", alice.identity.hash_hex,
                                   "invite", time.time())
        bob.storage.subscribe(ch_hash)
        bob.storage.upsert_member(ch_hash, bob.identity.hash_hex, "Bob", role="member")

        # Alice revokes send_message from members.
        no_send_perms = dict(PRESET_PRIVATE)
        no_send_perms["member"] = []
        alice.storage.set_channel_permissions(ch_hash, no_send_perms)
        bob.storage.set_channel_permissions(ch_hash, no_send_perms)

        # Bob tries to send, Alice's receiver should drop it.
        bob.messaging.send_message(
            channel_hash_hex=ch_hash,
            content="Should be dropped",
            subscriber_hashes=[alice.identity.hash_hex],
        )

        time.sleep(0.5)
        msgs = alice.storage.get_messages(ch_hash)
        assert all(m["sender_hash"] != bob.identity.hash_hex for m in msgs), \
            "Alice stored a message from Bob even though he lacks send_message permission"

    def test_owner_can_always_send(self, peer_factory):
        """The owner always has send_message regardless of the member permission list."""
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("owner-send", "", "invite")
        bob.storage.upsert_channel(ch_hash, "owner-send", "", alice.identity.hash_hex,
                                   "invite", time.time())
        bob.storage.subscribe(ch_hash)
        bob.storage.upsert_member(ch_hash, alice.identity.hash_hex, "Alice", role="owner")

        # Strip send_message from every non-owner role.
        no_send_perms = dict(PRESET_PRIVATE)
        no_send_perms["member"] = []
        no_send_perms["admin"] = []
        alice.storage.set_channel_permissions(ch_hash, no_send_perms)
        bob.storage.set_channel_permissions(ch_hash, no_send_perms)

        alice.messaging.send_message(
            channel_hash_hex=ch_hash,
            content="Owner message",
            subscriber_hashes=[bob.identity.hash_hex],
        )

        msg_id = alice.storage.get_messages(ch_hash)[0]["message_id"]
        assert wait_for_message(bob.storage, ch_hash, msg_id, timeout=5), \
            "Bob did not receive Alice's message even though she is the owner"


# ---------------------------------------------------------------------------
# Image attachment in messages
# ---------------------------------------------------------------------------

_FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 100


class TestImageMessages:
    def test_send_image_stored_locally(self, peer_factory):
        """Sending a message with image_data stores the blob in the sender's DB."""
        alice = peer_factory("alice")
        ch_hash = alice.channel_mgr.create_channel("img-local", "", "public")
        alice.messaging.send_message(
            channel_hash_hex=ch_hash,
            content="Here is a photo",
            subscriber_hashes=[alice.identity.hash_hex],
            image_data=_FAKE_JPEG,
        )
        msgs = alice.storage.get_messages(ch_hash)
        assert len(msgs) == 1
        assert bytes(msgs[0]["image_data"]) == _FAKE_JPEG

    def test_send_image_received_by_peer(self, peer_factory):
        """Bob receives a message with its image_data intact."""
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("img-peer", "", "public")
        bob.storage.upsert_channel(ch_hash, "img-peer", "", alice.identity.hash_hex,
                                   "public", time.time())
        bob.storage.subscribe(ch_hash)

        alice.messaging.send_message(
            channel_hash_hex=ch_hash,
            content="Photo for Bob",
            subscriber_hashes=[bob.identity.hash_hex],
            image_data=_FAKE_JPEG,
        )

        msg_id = alice.storage.get_messages(ch_hash)[0]["message_id"]
        assert wait_for_message(bob.storage, ch_hash, msg_id, timeout=5), \
            "Bob did not receive Alice's image message"

        bob_msgs = bob.storage.get_messages(ch_hash)
        assert len(bob_msgs) == 1
        assert bytes(bob_msgs[0]["image_data"]) == _FAKE_JPEG
        assert bob_msgs[0]["content"] == "Photo for Bob"

    def test_image_only_message(self, peer_factory):
        """A message with no text but with an image is sent and received."""
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("img-only", "", "public")
        bob.storage.upsert_channel(ch_hash, "img-only", "", alice.identity.hash_hex,
                                   "public", time.time())
        bob.storage.subscribe(ch_hash)

        alice.messaging.send_message(
            channel_hash_hex=ch_hash,
            content="",
            subscriber_hashes=[bob.identity.hash_hex],
            image_data=_FAKE_JPEG,
        )

        msg_id = alice.storage.get_messages(ch_hash)[0]["message_id"]
        assert wait_for_message(bob.storage, ch_hash, msg_id, timeout=5), \
            "Bob did not receive Alice's image-only message"

        bob_msgs = bob.storage.get_messages(ch_hash)
        assert bob_msgs[0]["content"] == ""
        assert bytes(bob_msgs[0]["image_data"]) == _FAKE_JPEG

    def test_message_without_image_still_works(self, peer_factory):
        """Plain text messages (no image_data) continue to work after the change."""
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("no-img", "", "public")
        bob.storage.upsert_channel(ch_hash, "no-img", "", alice.identity.hash_hex,
                                   "public", time.time())
        bob.storage.subscribe(ch_hash)

        alice.messaging.send_message(
            channel_hash_hex=ch_hash,
            content="Just text",
            subscriber_hashes=[bob.identity.hash_hex],
        )

        msg_id = alice.storage.get_messages(ch_hash)[0]["message_id"]
        assert wait_for_message(bob.storage, ch_hash, msg_id, timeout=5), \
            "Bob did not receive Alice's plain text message"

        bob_msgs = bob.storage.get_messages(ch_hash)
        assert bob_msgs[0]["content"] == "Just text"
        assert bob_msgs[0]["image_data"] is None


class TestDeliveryState:
    """A message sent to an unreachable peer must be distinguishable from a
    delivered one -- the sender tracks a per-message delivery state the client
    can read (catalogue #35)."""

    def test_message_to_unknown_path_is_pending_not_delivered(self, peer_factory):
        alice = peer_factory("alice")
        ch_hash = alice.channel_mgr.create_channel("pending", "", "public")

        # A recipient no identity was ever created for: recall() returns None,
        # so the message is queued rather than sent.
        unreachable = "ab" * 16
        alice.messaging.send_message(
            channel_hash_hex=ch_hash,
            content="Nobody home",
            subscriber_hashes=[unreachable],
        )

        msg_id = alice.storage.get_messages(ch_hash)[0]["message_id"]
        assert alice.messaging.get_delivery_state(msg_id) == DELIVERY_PENDING

    def test_pending_message_becomes_delivered_when_flushed(self, peer_factory, monkeypatch):
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("flush-state", "", "public")
        bob.storage.upsert_channel(ch_hash, "flush-state", "", alice.identity.hash_hex,
                                   "public", time.time())
        bob.storage.subscribe(ch_hash)

        # Force the path to be unknown at send time so the message is queued.
        monkeypatch.setattr(RNS.Identity, "recall", staticmethod(lambda *a, **k: None))
        alice.messaging.send_message(
            channel_hash_hex=ch_hash,
            content="Reaches Bob later",
            subscriber_hashes=[bob.identity.hash_hex],
        )
        msg_id = alice.storage.get_messages(ch_hash)[0]["message_id"]
        assert alice.messaging.get_delivery_state(msg_id) == DELIVERY_PENDING

        # Path resolves; flushing hands the message to the transport.
        monkeypatch.undo()
        alice.messaging.flush_pending(bob.identity.hash_hex)

        assert wait_for(
            lambda: alice.messaging.get_delivery_state(msg_id) == DELIVERY_DELIVERED,
            timeout=5,
        ), "delivery state did not upgrade to delivered after flush"
        assert wait_for_message(bob.storage, ch_hash, msg_id, timeout=5)

    def test_reachable_recipient_is_delivered(self, peer_factory):
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("reachable", "", "public")
        bob.storage.upsert_channel(ch_hash, "reachable", "", alice.identity.hash_hex,
                                   "public", time.time())
        bob.storage.subscribe(ch_hash)

        alice.messaging.send_message(
            channel_hash_hex=ch_hash,
            content="Straight through",
            subscriber_hashes=[bob.identity.hash_hex],
        )
        msg_id = alice.storage.get_messages(ch_hash)[0]["message_id"]
        assert alice.messaging.get_delivery_state(msg_id) == DELIVERY_DELIVERED

    def test_unretriable_failure_is_marked_failed(self, peer_factory):
        alice = peer_factory("alice")
        bob = peer_factory("bob")

        ch_hash = alice.channel_mgr.create_channel("failed-state", "", "public")
        bob.storage.upsert_channel(ch_hash, "failed-state", "", alice.identity.hash_hex,
                                   "public", time.time())
        bob.storage.subscribe(ch_hash)

        alice.messaging.send_message(
            channel_hash_hex=ch_hash,
            content="Will fail with no retry",
            subscriber_hashes=[bob.identity.hash_hex],
        )
        msg_id = alice.storage.get_messages(ch_hash)[0]["message_id"]

        # Drop the retry params so the failure cannot be re-queued, then fire
        # the failed callback as LXMF would on a delivery failure.
        alice.messaging._params_by_id.pop(msg_id, None)
        alice.messaging._on_delivery_failed(
            bob.identity.hash_hex, ch_hash, msg_id, [bob.identity.hash_hex]
        )
        assert alice.messaging.get_delivery_state(msg_id) == DELIVERY_FAILED

    def test_delivery_status_callback_fires_on_change(self, peer_factory):
        alice = peer_factory("alice")
        ch_hash = alice.channel_mgr.create_channel("cb-state", "", "public")

        events = []
        alice.messaging.add_delivery_status_callback(
            lambda ch, mid, state: events.append((ch, mid, state))
        )

        unreachable = "cd" * 16
        alice.messaging.send_message(
            channel_hash_hex=ch_hash,
            content="Fires a status event",
            subscriber_hashes=[unreachable],
        )
        msg_id = alice.storage.get_messages(ch_hash)[0]["message_id"]
        assert (ch_hash, msg_id, DELIVERY_PENDING) in events

    def test_own_only_message_has_no_delivery_state(self, peer_factory):
        alice = peer_factory("alice")
        ch_hash = alice.channel_mgr.create_channel("self-only", "", "public")

        alice.messaging.send_message(
            channel_hash_hex=ch_hash,
            content="Just me",
            subscriber_hashes=[alice.identity.hash_hex],
        )
        msg_id = alice.storage.get_messages(ch_hash)[0]["message_id"]
        assert alice.messaging.get_delivery_state(msg_id) is None


class TestPendingQueueIsBounded:
    """_params_by_id is capped, but _pending holds the same dicts -- image
    payloads included -- and was bounded only by a successful flush, which a
    peer that never comes back never produces.
    """

    def test_one_peers_queue_is_capped(self, peer_factory):
        from trenchchat.core.messaging import MAX_PENDING_PER_PEER

        alice = peer_factory("alice")
        unreachable = "ab" * 16
        for i in range(MAX_PENDING_PER_PEER + 20):
            alice.messaging._queue_pending(unreachable, {"msg_id": f"m{i}"})

        assert len(alice.messaging._pending[unreachable]) == MAX_PENDING_PER_PEER

    def test_the_newest_messages_are_the_ones_kept(self, peer_factory):
        from trenchchat.core.messaging import MAX_PENDING_PER_PEER

        alice = peer_factory("alice")
        peer = "cd" * 16
        for i in range(MAX_PENDING_PER_PEER + 1):
            alice.messaging._queue_pending(peer, {"msg_id": f"m{i}"})

        held = [p["msg_id"] for p in alice.messaging._pending[peer]]
        assert "m0" not in held, "the oldest message was kept over a newer one"
        assert f"m{MAX_PENDING_PER_PEER}" in held

    def test_the_number_of_tracked_peers_is_capped(self, peer_factory):
        from trenchchat.core.messaging import MAX_PENDING_PEERS

        alice = peer_factory("alice")
        for i in range(MAX_PENDING_PEERS + 30):
            alice.messaging._queue_pending(f"{i:032x}", {"msg_id": f"m{i}"})

        assert len(alice.messaging._pending) <= MAX_PENDING_PEERS



class TestMessageIdWireEncoding:
    """Message ids travel as their 32 digest bytes, and hex text is still read."""

    @staticmethod
    def _channel(alice, bob) -> str:
        ch_hash = alice.channel_mgr.create_channel("wire", "", "public")
        bob.storage.upsert_channel(ch_hash, "wire", "", alice.identity.hash_hex,
                                   "public", time.time())
        bob.storage.subscribe(ch_hash)
        return ch_hash

    @staticmethod
    def _capture(peer, monkeypatch) -> list[dict]:
        captured = []
        original = peer.router.send

        def spy(lxm):
            captured.append(dict(getattr(lxm, "fields", None) or {}))
            return original(lxm)

        monkeypatch.setattr(peer.router, "send", spy)
        return captured

    def test_helpers_round_trip(self):
        from trenchchat.core.protocol import message_id_from_wire, message_id_to_wire

        hex_id = "ab" * 32
        assert message_id_to_wire(hex_id) == bytes.fromhex(hex_id)
        assert message_id_from_wire(bytes.fromhex(hex_id)) == hex_id
        assert message_id_from_wire(hex_id) == hex_id
        assert message_id_from_wire(hex_id.encode()) == hex_id
        assert message_id_to_wire(None) is None
        assert message_id_to_wire("") is None
        assert message_id_to_wire("not-a-digest") == "not-a-digest"
        assert message_id_from_wire(None) == ""

    def test_channel_message_carries_binary_ids(self, peer_factory, monkeypatch):
        from trenchchat.core.protocol import (
            F_LAST_SEEN_ID, F_MESSAGE_ID, F_REPLY_TO, unpack_fields,
        )

        alice = peer_factory("alice")
        bob = peer_factory("bob")
        ch_hash = self._channel(alice, bob)
        recipients = [bob.identity.hash_hex]

        alice.messaging.send_message(ch_hash, "first", subscriber_hashes=recipients)
        first = alice.storage.get_latest_message_id(ch_hash)
        assert wait_for_message(bob.storage, ch_hash, first)

        captured = self._capture(alice, monkeypatch)
        alice.messaging.send_message(ch_hash, "second", reply_to=first,
                                     subscriber_hashes=recipients)
        second = alice.storage.get_latest_message_id(ch_hash)

        fields = unpack_fields(captured[0])
        assert fields[F_MESSAGE_ID] == bytes.fromhex(second)
        assert fields[F_REPLY_TO] == bytes.fromhex(first)
        assert fields[F_LAST_SEEN_ID] == bytes.fromhex(first)

        assert wait_for_message(bob.storage, ch_hash, second)
        row = bob.storage.get_message(ch_hash, second)
        assert row["message_id"] == second
        assert row["reply_to"] == first
        assert row["last_seen_id"] == first

    def test_hex_ids_from_an_older_peer_are_still_read(self, peer_factory, monkeypatch):
        alice = peer_factory("alice")
        bob = peer_factory("bob")
        ch_hash = self._channel(alice, bob)
        recipients = [bob.identity.hash_hex]

        alice.messaging.send_message(ch_hash, "first", subscriber_hashes=recipients)
        first = alice.storage.get_latest_message_id(ch_hash)
        assert wait_for_message(bob.storage, ch_hash, first)

        monkeypatch.setattr("trenchchat.core.messaging.message_id_to_wire",
                            lambda value: value)
        captured = self._capture(alice, monkeypatch)
        alice.messaging.send_message(ch_hash, "second", reply_to=first,
                                     subscriber_hashes=recipients)
        second = alice.storage.get_latest_message_id(ch_hash)

        from trenchchat.core.protocol import F_REPLY_TO, unpack_fields
        assert unpack_fields(captured[0])[F_REPLY_TO] == first

        assert wait_for_message(bob.storage, ch_hash, second)
        row = bob.storage.get_message(ch_hash, second)
        assert row["reply_to"] == first
        assert row["last_seen_id"] == first
