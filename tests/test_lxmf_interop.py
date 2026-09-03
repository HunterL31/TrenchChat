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
from trenchchat.core.interop import carries_only_trenchchat_markup, plain_lxmf_content
from trenchchat.core.messaging import _compute_message_id
from trenchchat.core.naming import dm_hash_for
from trenchchat.core.protocol import (
    DM_ENVELOPE_TYPE, F_CHANNEL_HASH, F_MSG_TYPE, LXMF_FIELD_CUSTOM_DATA,
    LXMF_FIELD_CUSTOM_TYPE, LXMF_FIELD_IMAGE, MT_EMOJI_REQUEST,
    unpack_dm_envelope, unpack_fields,
)

# Stands in for a custom emoji nobody holds the image for.
EMOJI_HASH = "a" * 64


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


def control_messages(sent: list[dict]) -> list[dict]:
    """The TrenchChat control messages among captured outbound fields."""
    return [f for f in (unpack_fields(raw) for raw in sent) if f]


def sent_content(peer, monkeypatch) -> list[str]:
    """Capture the words that actually go on the wire from this peer."""
    captured = []
    original = peer.router.send

    def spy(lxm):
        content = lxm.content or b""
        if isinstance(content, bytes):
            content = content.decode(errors="replace")
        captured.append(content)
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


def test_an_emoji_request_is_not_sent_to_another_lxmf_client(peer_factory, monkeypatch):
    """An inbound message may reference an emoji we lack whatever wrote it.
    Asking the client that wrote it only works if it is TrenchChat."""
    a = peer_factory("alice")
    b = peer_factory("bob")
    befriend(a, b)

    asked = sent_fields(b, monkeypatch)
    a.router.send(plain_lxm(a, b, f"look :wave@{EMOJI_HASH}:"))
    conversation = dm_hash_for(a.identity.hash_hex, b.identity.hash_hex)
    assert wait_for(lambda: b.storage.get_messages(conversation))

    time.sleep(0.3)
    assert not [f for f in control_messages(asked)
                if f.get(F_MSG_TYPE) == MT_EMOJI_REQUEST]


def test_an_emoji_request_still_reaches_a_trenchchat_peer(peer_factory, monkeypatch):
    a = peer_factory("alice")
    b = peer_factory("bob")
    befriend(a, b)

    a.messaging.send_direct(b.identity.hash_hex, "hello")
    assert wait_for(lambda: b.direct_mgr.conversations()
                    and b.direct_mgr.conversations()[0]["peer_is_trenchchat"])
    b.messaging.send_direct(a.identity.hash_hex, "hello yourself")
    assert wait_for(lambda: a.direct_mgr.conversations()
                    and a.direct_mgr.conversations()[0]["peer_is_trenchchat"])

    asked = sent_fields(b, monkeypatch)
    a.messaging.send_direct(b.identity.hash_hex, f"look :wave@{EMOJI_HASH}:")
    assert wait_for(
        lambda: [f for f in control_messages(asked)
                 if f.get(F_MSG_TYPE) == MT_EMOJI_REQUEST]
    )


# ---------------------------------------------------------------------------
# what a message is rewritten to for a client that is not TrenchChat
# ---------------------------------------------------------------------------

def test_a_custom_emoji_keeps_its_name_and_loses_its_hash(peer_factory, monkeypatch):
    """The hash is the half addressed to us: the other client cannot ask for
    the image, so all it would carry is 64 characters of noise."""
    a = peer_factory("alice")
    b = peer_factory("bob")
    befriend(a, b)

    words = sent_content(a, monkeypatch)
    msg_id = a.messaging.send_direct(b.identity.hash_hex, f"hi :wave@{EMOJI_HASH}:")

    assert words == ["hi :wave:"]
    conversation = dm_hash_for(a.identity.hash_hex, b.identity.hash_hex)
    stored = [m for m in a.storage.get_messages(conversation)
              if m["message_id"] == msg_id]
    assert stored[0]["content"] == "hi :wave:"


def test_a_theme_code_is_not_sent_to_another_lxmf_client(peer_factory, monkeypatch):
    a = peer_factory("alice")
    b = peer_factory("bob")
    befriend(a, b)

    words = sent_content(a, monkeypatch)
    a.messaging.send_direct(b.identity.hash_hex, "try this tct1:AbC-_09 nice one")

    assert words == ["try this nice one"]


def test_a_message_of_only_markup_is_refused_rather_than_sent_empty(
        peer_factory, monkeypatch):
    """An empty message is what the other client would show, and it says
    nothing. Refusing hands the words back to the sender instead."""
    a = peer_factory("alice")
    b = peer_factory("bob")
    befriend(a, b)

    words = sent_content(a, monkeypatch)
    assert a.messaging.send_direct(b.identity.hash_hex, "tct1:AbC-_09") is None
    assert words == []

    conversation = dm_hash_for(a.identity.hash_hex, b.identity.hash_hex)
    assert a.storage.get_messages(conversation) == []


def test_a_trenchchat_peer_gets_the_whole_token(peer_factory, monkeypatch):
    a = peer_factory("alice")
    b = peer_factory("bob")
    befriend(a, b)

    b.messaging.send_direct(a.identity.hash_hex, "hello")
    conversation = dm_hash_for(a.identity.hash_hex, b.identity.hash_hex)
    assert wait_for(lambda: a.direct_mgr.conversations()
                    and a.direct_mgr.conversations()[0]["peer_is_trenchchat"])

    words = sent_content(a, monkeypatch)
    a.messaging.send_direct(b.identity.hash_hex, f"hi :wave@{EMOJI_HASH}:")

    assert words == [f"hi :wave@{EMOJI_HASH}:"]


def test_a_channel_message_is_never_rewritten(peer_factory, monkeypatch):
    """Everyone in a channel runs TrenchChat; there is nothing to degrade for."""
    a = peer_factory("alice")
    b = peer_factory("bob")

    channel_hash = a.channel_mgr.create_channel("markup", "")
    words = sent_content(a, monkeypatch)
    a.messaging.send_message(channel_hash, f"hi :wave@{EMOJI_HASH}: tct1:AbC-_09",
                             subscriber_hashes=[b.identity.hash_hex])

    assert words == [f"hi :wave@{EMOJI_HASH}: tct1:AbC-_09"]


# ---------------------------------------------------------------------------
# the rewrite itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("written,read", [
    (f"hi :wave@{EMOJI_HASH}:", "hi :wave:"),
    (f":a@{EMOJI_HASH}::b@{EMOJI_HASH}:", ":a::b:"),
    (f"hi :wave@{EMOJI_HASH.upper()}:", "hi :wave:"),
    (":wave:", ":wave:"),
    ("meet at 12:30, no:emoji:here", "meet at 12:30, no:emoji:here"),
    ("try tct1:AbC-_09 out", "try out"),
    ("tct1:AbC-_09", ""),
    ("", ""),
])
def test_what_a_foreign_client_is_given_to_read(written, read):
    assert plain_lxmf_content(written) == read


def test_only_markup_is_told_apart_from_a_message_that_has_words():
    assert carries_only_trenchchat_markup("tct1:AbC-_09")
    assert not carries_only_trenchchat_markup("look tct1:AbC-_09")
    assert not carries_only_trenchchat_markup(f":wave@{EMOJI_HASH}:")
    assert not carries_only_trenchchat_markup("")
