"""
Integration tests for direct messages between two peers.

A conversation only carries traffic when both peers hold the other as an
accepted friend, and its address is derived from the pair rather than
announced -- so these cover both the gate and the addressing, plus the
guarantee that a conversation never leaks into anything channel-shaped.
"""

import time

import pytest

from tests.helpers import wait_for
from trenchchat.core import actions
from trenchchat.core.messaging import (
    DELIVERY_DELIVERED, DELIVERY_PENDING, DELIVERY_PROPAGATED,
)
from trenchchat.core.naming import dm_hash_for
from trenchchat.core.storage import FRIEND_PENDING_IN, FRIEND_PENDING_OUT


def befriend(a, b):
    """Make two peers mutual friends without going over the wire."""
    a.friends_mgr.add_friend(b.identity.hash_hex)
    b.friends_mgr.add_friend(a.identity.hash_hex)


# ---------------------------------------------------------------------------
# addressing
# ---------------------------------------------------------------------------

def test_both_peers_derive_the_same_conversation_hash(peer_factory):
    a = peer_factory("alice")
    b = peer_factory("bob")
    befriend(a, b)

    from_a = a.direct_mgr.conversation_hash(b.identity.hash_hex)
    from_b = b.direct_mgr.conversation_hash(a.identity.hash_hex)
    assert from_a == from_b
    assert from_a == dm_hash_for(a.identity.hash_hex, b.identity.hash_hex)
    # Same width as a channel hash, so it rides F_CHANNEL_HASH unchanged.
    assert len(from_a) == 32


def test_conversation_hash_rejects_self_and_malformed(peer_factory):
    a = peer_factory("alice")
    assert a.direct_mgr.conversation_hash(a.identity.hash_hex) is None
    assert a.direct_mgr.conversation_hash("nothex") is None
    assert a.direct_mgr.conversation_hash("ab" * 8) is None


# ---------------------------------------------------------------------------
# send / receive
# ---------------------------------------------------------------------------

def test_direct_message_round_trip(peer_factory):
    a = peer_factory("alice")
    b = peer_factory("bob")
    befriend(a, b)

    sent = a.messaging.send_direct(b.identity.hash_hex, "meet at the ridge")
    assert sent is not None
    assert wait_for(lambda: b.storage.message_exists(sent))

    conversation = b.direct_mgr.conversation_hash(a.identity.hash_hex)
    stored = b.storage.get_message(conversation, sent)
    assert stored["content"] == "meet at the ridge"
    assert stored["sender_hash"] == a.identity.hash_hex

    reply = b.messaging.send_direct(a.identity.hash_hex, "on my way")
    assert wait_for(lambda: a.storage.message_exists(reply))
    assert a.storage.get_message(conversation, reply)["content"] == "on my way"


def test_reply_threading_survives_the_conversation(peer_factory):
    a = peer_factory("alice")
    b = peer_factory("bob")
    befriend(a, b)

    first = a.messaging.send_direct(b.identity.hash_hex, "did you bring the radio")
    assert wait_for(lambda: b.storage.message_exists(first))

    answer = b.messaging.send_direct(a.identity.hash_hex, "yes", reply_to=first)
    assert wait_for(lambda: a.storage.message_exists(answer))

    conversation = a.direct_mgr.conversation_hash(b.identity.hash_hex)
    assert a.storage.get_message(conversation, answer)["reply_to"] == first


def test_image_attachment_rides_a_direct_message(peer_factory, monkeypatch):
    a = peer_factory("alice")
    b = peer_factory("bob")
    befriend(a, b)

    monkeypatch.setattr("trenchchat.core.messaging.inbound_image_is_sane",
                        lambda data: True)
    payload = b"\xff\xd8\xff\xe0 fake jpeg body"
    sent = a.messaging.send_direct(b.identity.hash_hex, "look", image_data=payload)
    assert wait_for(lambda: b.storage.message_exists(sent))

    conversation = b.direct_mgr.conversation_hash(a.identity.hash_hex)
    assert bytes(b.storage.get_message(conversation, sent)["image_data"]) == payload


def test_own_message_is_stored_locally_immediately(peer_factory):
    a = peer_factory("alice")
    b = peer_factory("bob")
    befriend(a, b)

    sent = a.messaging.send_direct(b.identity.hash_hex, "first")
    conversation = a.direct_mgr.conversation_hash(b.identity.hash_hex)
    assert a.storage.get_message(conversation, sent) is not None
    assert a.messaging.get_delivery_state(sent) == DELIVERY_DELIVERED


# ---------------------------------------------------------------------------
# the mutual-friendship gate
# ---------------------------------------------------------------------------

def test_a_stranger_cannot_send_us_a_direct_message(peer_factory):
    a = peer_factory("alice")
    b = peer_factory("bob")
    # Only the sender has added the recipient.
    a.friends_mgr.add_friend(b.identity.hash_hex)

    sent = a.messaging.send_direct(b.identity.hash_hex, "let me in")
    assert sent is not None
    time.sleep(0.4)
    assert not b.storage.message_exists(sent)


def test_sending_to_a_non_friend_is_a_silent_no_op(peer_factory):
    a = peer_factory("alice")
    b = peer_factory("bob")

    assert a.messaging.send_direct(b.identity.hash_hex, "hello") is None
    conversation = dm_hash_for(a.identity.hash_hex, b.identity.hash_hex)
    assert a.storage.get_messages(conversation) == []


@pytest.mark.parametrize("state", [FRIEND_PENDING_IN, FRIEND_PENDING_OUT])
def test_a_pending_friendship_is_not_a_friendship(peer_factory, state):
    a = peer_factory("alice")
    b = peer_factory("bob")
    a.friends_mgr.add_friend(b.identity.hash_hex)
    b.storage.upsert_friend(a.identity.hash_hex, "", "", state)

    sent = a.messaging.send_direct(b.identity.hash_hex, "half a handshake")
    time.sleep(0.4)
    assert not b.storage.message_exists(sent)


def test_removing_a_friend_stops_accepting_their_messages(peer_factory):
    a = peer_factory("alice")
    b = peer_factory("bob")
    befriend(a, b)

    first = a.messaging.send_direct(b.identity.hash_hex, "still friends")
    assert wait_for(lambda: b.storage.message_exists(first))

    b.friends_mgr.remove_friend(a.identity.hash_hex)
    second = a.messaging.send_direct(b.identity.hash_hex, "not any more")
    time.sleep(0.4)
    assert not b.storage.message_exists(second)


# ---------------------------------------------------------------------------
# conversations, unread, and staying out of the channel surfaces
# ---------------------------------------------------------------------------

def test_conversation_is_created_on_first_use_and_listed(peer_factory):
    a = peer_factory("alice")
    b = peer_factory("bob")
    befriend(a, b)

    assert a.direct_mgr.conversations() == []
    conversation = a.direct_mgr.open_conversation(b.identity.hash_hex)
    assert conversation is not None

    listed = a.direct_mgr.conversations()
    assert len(listed) == 1
    assert listed[0]["hash"] == conversation
    assert listed[0]["peer_hash"] == b.identity.hash_hex
    assert listed[0]["is_friend"] is True


def test_unread_counts_only_the_peers_messages_until_read(peer_factory):
    a = peer_factory("alice")
    b = peer_factory("bob")
    befriend(a, b)

    sent = a.messaging.send_direct(b.identity.hash_hex, "one")
    assert wait_for(lambda: b.storage.message_exists(sent))
    assert b.direct_mgr.conversations()[0]["unread"] == 1

    b.messaging.send_direct(a.identity.hash_hex, "answering")
    assert b.direct_mgr.conversations()[0]["unread"] == 1

    conversation = b.direct_mgr.conversation_hash(a.identity.hash_hex)
    assert b.direct_mgr.mark_read(conversation) is True
    assert b.direct_mgr.conversations()[0]["unread"] == 0


def test_a_conversation_is_never_a_channel(peer_factory):
    a = peer_factory("alice")
    b = peer_factory("bob")
    befriend(a, b)
    conversation = a.direct_mgr.open_conversation(b.identity.hash_hex)

    assert [c["hash"] for c in a.storage.get_standalone_channels()] == []
    assert a.storage.is_subscribed(conversation) is False
    assert [s["channel_hash"] for s in a.storage.get_subscriptions()] == []
    assert a.storage.is_dm(conversation) is True


def test_deleting_a_conversation_takes_its_messages_with_it(peer_factory):
    a = peer_factory("alice")
    b = peer_factory("bob")
    befriend(a, b)

    sent = a.messaging.send_direct(b.identity.hash_hex, "forget this")
    conversation = a.direct_mgr.conversation_hash(b.identity.hash_hex)
    assert a.storage.get_message(conversation, sent) is not None

    assert a.direct_mgr.delete_conversation(conversation) is True
    assert a.storage.get_messages(conversation) == []
    assert a.direct_mgr.conversations() == []
    assert a.storage.get_channel(conversation) is None


# ---------------------------------------------------------------------------
# delivery when the peer is not there
# ---------------------------------------------------------------------------

def test_an_unreachable_peer_queues_the_message(peer_factory):
    a = peer_factory("alice")
    b = peer_factory("bob")
    befriend(a, b)
    # A friend whose identity we have never learned: nothing to address, and
    # no propagation node to leave it with either.
    stranger = "cc" * 16
    a.friends_mgr.add_friend(stranger)

    sent = a.messaging.send_direct(stranger, "into the void")
    assert sent is not None
    assert a.messaging.get_delivery_state(sent) == DELIVERY_PENDING


def test_an_offline_peer_goes_through_a_propagation_node(peer_factory, monkeypatch):
    a = peer_factory("alice")
    b = peer_factory("bob")
    befriend(a, b)

    # Presence says the peer is away, and a node is available to hold it.
    a.messaging.set_presence_manager(a.presence_mgr)
    monkeypatch.setattr(type(a.router), "outbound_propagation_node",
                        property(lambda self: b"\x01" * 16))

    sent = a.messaging.send_direct(b.identity.hash_hex, "when you get back")
    assert a.messaging.get_delivery_state(sent) == DELIVERY_PROPAGATED
    conversation = a.direct_mgr.conversation_hash(b.identity.hash_hex)
    assert a.storage.get_message(conversation, sent) is not None


def test_no_missed_delivery_hint_is_broadcast_for_a_conversation(peer_factory):
    a = peer_factory("alice")
    hints = []
    a.messaging.set_missed_delivery_callback(
        lambda *args: hints.append(args)
    )
    a.friends_mgr.add_friend("cc" * 16)

    a.messaging.send_direct("cc" * 16, "nobody should hear about this")
    assert hints == []


# ---------------------------------------------------------------------------
# actions layer
# ---------------------------------------------------------------------------

def test_actions_send_direct_message_matches_the_manager(peer_factory):
    a = peer_factory("alice")
    b = peer_factory("bob")
    befriend(a, b)

    sent = actions.send_direct_message(a.direct_mgr, a.messaging,
                                       b.identity.hash_hex, "via actions")
    assert sent is not None
    assert wait_for(lambda: b.storage.message_exists(sent))


def test_actions_refuse_a_conversation_with_a_non_friend(peer_factory):
    a = peer_factory("alice")
    b = peer_factory("bob")

    assert actions.open_dm(a.direct_mgr, b.identity.hash_hex) is None
    assert actions.send_direct_message(a.direct_mgr, a.messaging,
                                       b.identity.hash_hex, "no") is None


def test_dm_recipients_is_the_other_half_and_nobody_else(peer_factory):
    a = peer_factory("alice")
    b = peer_factory("bob")
    befriend(a, b)
    conversation = a.direct_mgr.open_conversation(b.identity.hash_hex)

    assert actions.dm_recipients(a.direct_mgr, conversation) == [b.identity.hash_hex]
    a.friends_mgr.remove_friend(b.identity.hash_hex)
    assert actions.dm_recipients(a.direct_mgr, conversation) is None


def test_reactions_work_inside_a_conversation(peer_factory):
    a = peer_factory("alice")
    b = peer_factory("bob")
    befriend(a, b)

    sent = a.messaging.send_direct(b.identity.hash_hex, "worth a reaction")
    assert wait_for(lambda: b.storage.message_exists(sent))

    conversation = b.direct_mgr.conversation_hash(a.identity.hash_hex)
    b.reaction_mgr.add_reaction(
        conversation, sent, "👍",
        actions.dm_recipients(b.direct_mgr, conversation),
    )
    assert wait_for(
        lambda: any(r["reactor_hash"] == b.identity.hash_hex
                    for r in a.storage.get_reactions(sent))
    )
