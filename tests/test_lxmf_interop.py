"""
Direct messages with a client that is not TrenchChat.

A conversation is the one thing TrenchChat sends that can legitimately arrive
at Sideband, NomadNet, or anything else speaking LXMF, so it is carried as a
plain LXMF message: the words in the ordinary content, the attachment in
LXMF's own image field, and everything TrenchChat adds tucked inside the
custom-payload fields the standard sets aside for exactly that.

The "other client" here is a bare LXMessage built by hand -- no TrenchChat
fields at all -- which is precisely what one of them sends.
"""

import time

import LXMF
import RNS
import pytest

from tests.helpers import wait_for
from trenchchat.core.messaging import _compute_message_id
from trenchchat.core.naming import dm_hash_for
from trenchchat.core.protocol import (
    DM_ENVELOPE_TYPE, F_CHANNEL_HASH, LXMF_FIELD_CUSTOM_DATA,
    LXMF_FIELD_CUSTOM_TYPE, LXMF_FIELD_IMAGE, unpack_dm_envelope,
)


def befriend(a, b):
    a.friends_mgr.add_friend(b.identity.hash_hex)
    b.friends_mgr.add_friend(a.identity.hash_hex)


def plain_lxm(sender, recipient, content: str, fields: dict | None = None):
    """A message as any other LXMF client would send it."""
    dest = RNS.Destination(
        recipient.identity.rns_identity, RNS.Destination.OUT,
        RNS.Destination.SINGLE, "lxmf", "delivery",
    )
    lxm = LXMF.LXMessage(dest, sender.router.delivery_destination, content,
                         desired_method=LXMF.LXMessage.DIRECT)
    lxm.fields = fields or {}
    return lxm


def sent_fields(peer, monkeypatch) -> list[dict]:
    """Capture what actually goes on the wire from this peer."""
    captured = []
    original = peer.router.send

    def spy(lxm):
        captured.append(dict(getattr(lxm, "fields", None) or {}))
        return original(lxm)

    monkeypatch.setattr(peer.router, "send", spy)
    return captured


# ---------------------------------------------------------------------------
# what we put on the wire
# ---------------------------------------------------------------------------

def test_a_direct_message_claims_no_lxmf_field_numbers(peer_factory, monkeypatch):
    """The whole reason for the envelope.

    TrenchChat's own field numbers mean other things in LXMF's registry -- 0x02
    is telemetry there, 0x06 an image -- which is harmless between TrenchChat
    peers and wrong the moment a message reaches somebody else. A conversation
    therefore uses only the numbers LXMF sets aside for an application's own
    data.
    """
    a = peer_factory("alice")
    b = peer_factory("bob")
    befriend(a, b)
    captured = sent_fields(a, monkeypatch)

    a.messaging.send_direct(b.identity.hash_hex, "no squatting")

    assert captured, "nothing was sent"
    fields = captured[0]
    assert set(fields) <= {LXMF_FIELD_CUSTOM_TYPE, LXMF_FIELD_CUSTOM_DATA,
                           LXMF_FIELD_IMAGE}
    assert fields[LXMF_FIELD_CUSTOM_TYPE] == DM_ENVELOPE_TYPE
    # Nor a conversation address: the receiver derives that for itself.
    assert F_CHANNEL_HASH not in fields


def test_the_envelope_carries_what_trenchchat_adds(peer_factory, monkeypatch):
    a = peer_factory("alice")
    b = peer_factory("bob")
    befriend(a, b)

    first = a.messaging.send_direct(b.identity.hash_hex, "first")
    captured = sent_fields(a, monkeypatch)
    a.messaging.send_direct(b.identity.hash_hex, "second", reply_to=first)

    envelope = unpack_dm_envelope(captured[0])
    assert envelope is not None
    assert envelope["reply_to"] == first
    assert envelope["display_name"] == a.identity.display_name
    assert isinstance(envelope["author_sig"], bytes)


def test_an_attachment_uses_the_standard_image_field(peer_factory, monkeypatch):
    """So the other client can actually show it."""
    a = peer_factory("alice")
    b = peer_factory("bob")
    befriend(a, b)
    captured = sent_fields(a, monkeypatch)

    payload = b"\xff\xd8\xff\xe0 fake jpeg body"
    a.messaging.send_direct(b.identity.hash_hex, "look", image_data=payload)

    image = captured[0][LXMF_FIELD_IMAGE]
    assert isinstance(image, list) and image[1] == payload


# ---------------------------------------------------------------------------
# what we accept off it
# ---------------------------------------------------------------------------

def test_a_plain_lxmf_message_from_a_friend_is_a_direct_message(peer_factory):
    """The message another client sends: content, and nothing else."""
    a = peer_factory("alice")
    b = peer_factory("bob")
    befriend(a, b)

    a.router.send(plain_lxm(a, b, "sent from another client"))

    conversation = dm_hash_for(a.identity.hash_hex, b.identity.hash_hex)
    assert wait_for(lambda: b.storage.get_messages(conversation))
    stored = b.storage.get_messages(conversation)[0]
    assert stored["content"] == "sent from another client"
    assert stored["sender_hash"] == a.identity.hash_hex


def test_a_plain_message_starts_a_conversation_that_is_listed(peer_factory):
    a = peer_factory("alice")
    b = peer_factory("bob")
    befriend(a, b)

    a.router.send(plain_lxm(a, b, "hello there"))

    assert wait_for(lambda: b.direct_mgr.conversations())
    conversation = b.direct_mgr.conversations()[0]
    assert conversation["peer_hash"] == a.identity.hash_hex
    assert conversation["unread"] == 1
    # Nothing has claimed to be TrenchChat, so the extras stay off.
    assert conversation["peer_is_trenchchat"] is False


def test_a_plain_message_from_a_stranger_is_still_refused(peer_factory):
    """Interoperability is not a way around the gate.

    Dropping the envelope is exactly what an attacker would try, since it is
    the half that carries a signature. It buys nothing: the friendship is
    checked against the sender LXMF authenticated, not against anything the
    message says about itself.
    """
    mallory = peer_factory("mallory")
    bob = peer_factory("bob")

    mallory.router.send(plain_lxm(mallory, bob, "let me in"))

    time.sleep(0.5)
    conversation = dm_hash_for(mallory.identity.hash_hex, bob.identity.hash_hex)
    assert bob.storage.get_messages(conversation) == []
    assert bob.direct_mgr.conversations() == []


def test_an_attachment_from_another_client_is_kept(peer_factory, monkeypatch):
    a = peer_factory("alice")
    b = peer_factory("bob")
    befriend(a, b)
    monkeypatch.setattr("trenchchat.core.messaging.inbound_image_is_sane",
                        lambda data: True)

    payload = b"\xff\xd8\xff\xe0 from sideband"
    a.router.send(plain_lxm(a, b, "a picture",
                            {LXMF_FIELD_IMAGE: ["jpg", payload]}))

    conversation = dm_hash_for(a.identity.hash_hex, b.identity.hash_hex)
    assert wait_for(lambda: b.storage.get_messages(conversation))
    assert bytes(b.storage.get_messages(conversation)[0]["image_data"]) == payload


def test_a_bare_image_payload_is_tolerated(peer_factory, monkeypatch):
    """The image structure is convention, not specification, so be liberal."""
    a = peer_factory("alice")
    b = peer_factory("bob")
    befriend(a, b)
    monkeypatch.setattr("trenchchat.core.messaging.inbound_image_is_sane",
                        lambda data: True)

    payload = b"\xff\xd8\xff\xe0 bare"
    a.router.send(plain_lxm(a, b, "bare bytes", {LXMF_FIELD_IMAGE: payload}))

    conversation = dm_hash_for(a.identity.hash_hex, b.identity.hash_hex)
    assert wait_for(lambda: b.storage.get_messages(conversation))
    assert bytes(b.storage.get_messages(conversation)[0]["image_data"]) == payload


def test_an_oversized_image_from_another_client_is_stripped_not_trusted(
        peer_factory, monkeypatch):
    a = peer_factory("alice")
    b = peer_factory("bob")
    befriend(a, b)
    monkeypatch.setattr("trenchchat.core.messaging.MAX_IMAGE_BYTES", 8)

    a.router.send(plain_lxm(a, b, "too big",
                            {LXMF_FIELD_IMAGE: ["jpg", b"x" * 64]}))

    conversation = dm_hash_for(a.identity.hash_hex, b.identity.hash_hex)
    assert wait_for(lambda: b.storage.get_messages(conversation))
    stored = b.storage.get_messages(conversation)[0]
    assert stored["image_data"] is None
    assert stored["image_stripped"] == 1


def test_a_foreign_custom_envelope_is_read_as_a_plain_message(peer_factory):
    """Another application's payload on the same fields is not ours to parse."""
    a = peer_factory("alice")
    b = peer_factory("bob")
    befriend(a, b)

    a.router.send(plain_lxm(a, b, "someone else's protocol", {
        LXMF_FIELD_CUSTOM_TYPE: "someoneelse/v1",
        LXMF_FIELD_CUSTOM_DATA: b"\x00 not ours",
    }))

    conversation = dm_hash_for(a.identity.hash_hex, b.identity.hash_hex)
    assert wait_for(lambda: b.storage.get_messages(conversation))
    assert b.storage.get_messages(conversation)[0]["content"] == "someone else's protocol"
    assert b.direct_mgr.conversations()[0]["peer_is_trenchchat"] is False


def test_a_trenchchat_sender_is_recognised_as_one(peer_factory):
    a = peer_factory("alice")
    b = peer_factory("bob")
    befriend(a, b)

    sent = a.messaging.send_direct(b.identity.hash_hex, "from trenchchat")
    assert wait_for(lambda: b.storage.message_exists(sent))

    assert b.direct_mgr.conversations()[0]["peer_is_trenchchat"] is True


def test_a_trenchchat_sender_must_still_sign(peer_factory):
    """Relaxing the signature is for clients that have none, not for peers
    claiming to be TrenchChat -- otherwise the envelope would be a way to
    assert authorship without proving it."""
    a = peer_factory("alice")
    b = peer_factory("bob")
    befriend(a, b)

    ts = time.time()
    content = "unsigned but claiming to be one of us"
    from trenchchat.core.protocol import pack_dm_envelope
    a.router.send(plain_lxm(a, b, content, {
        LXMF_FIELD_CUSTOM_TYPE: DM_ENVELOPE_TYPE,
        LXMF_FIELD_CUSTOM_DATA: pack_dm_envelope(
            message_id=_compute_message_id(content, a.identity.hash_hex, ts),
            timestamp=ts, display_name="Alice", reply_to=None,
            last_seen_id=None, author_sig=None,
        ),
    }))

    time.sleep(0.5)
    conversation = dm_hash_for(a.identity.hash_hex, b.identity.hash_hex)
    assert b.storage.get_messages(conversation) == []


# ---------------------------------------------------------------------------
# what we do not send to a client that cannot use it
# ---------------------------------------------------------------------------

def test_reactions_are_not_sent_to_another_lxmf_client(peer_factory):
    """A reaction is a TrenchChat control message; to another client it is an
    empty one. It is still recorded locally, just not transmitted."""
    from trenchchat.core import actions

    a = peer_factory("alice")
    b = peer_factory("bob")
    befriend(a, b)

    a.router.send(plain_lxm(a, b, "reactable"))
    conversation = dm_hash_for(a.identity.hash_hex, b.identity.hash_hex)
    assert wait_for(lambda: b.storage.get_messages(conversation))
    msg_id = b.storage.get_messages(conversation)[0]["message_id"]

    assert actions.dm_recipients(b.direct_mgr, conversation,
                                 trenchchat_only=True) == []
    # Without the guard it would go to the peer, which is what we are avoiding.
    assert actions.dm_recipients(b.direct_mgr, conversation) == [a.identity.hash_hex]

    b.reaction_mgr.add_reaction(conversation, msg_id, "👍", [])
    assert any(r["reactor_hash"] == b.identity.hash_hex
               for r in b.storage.get_reactions(msg_id))
