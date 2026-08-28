"""
The protocol envelope: every channel and control message travels inside
LXMF's custom-payload fields, never as bare field keys in LXMF's reserved
0x00-0x80 range.

LXMF allocates that range for itself (0x01 is FIELD_EMBEDDED_LXMS, 0x02
FIELD_TELEMETRY, 0x06 FIELD_IMAGE, ...), so a bare TrenchChat key is a
misparsed field to every other client and a silent break waiting on the next
upstream allocation. The envelope reduces us to one unknown field elsewhere,
and the Router unwraps it once so handlers only ever see the inner dict.
"""

import time

import LXMF
import RNS

from tests.helpers import wait_for
from trenchchat.core.naming import dm_hash_for
from trenchchat.core.protocol import (
    ENVELOPE_TYPE, F_CHANNEL_HASH, F_MSG_TYPE, LXMF_FIELD_CUSTOM_DATA,
    LXMF_FIELD_CUSTOM_TYPE, is_protocol_envelope, pack_fields, unpack_fields,
)


def befriend(a, b):
    a.friends_mgr.add_friend(b.identity.hash_hex)
    b.friends_mgr.add_friend(a.identity.hash_hex)


def sent_fields(peer, monkeypatch) -> list[dict]:
    """Capture what actually goes on the wire from this peer."""
    captured = []
    original = peer.router.send

    def spy(lxm):
        captured.append(dict(getattr(lxm, "fields", None) or {}))
        return original(lxm)

    monkeypatch.setattr(peer.router, "send", spy)
    return captured


def crafted_lxm(sender, recipient, content: str, fields: dict):
    dest = RNS.Destination(
        recipient.identity.rns_identity, RNS.Destination.OUT,
        RNS.Destination.SINGLE, "lxmf", "delivery",
    )
    lxm = LXMF.LXMessage(dest, sender.router.delivery_destination, content,
                         desired_method=LXMF.LXMessage.DIRECT)
    lxm.fields = fields
    return lxm


# ---------------------------------------------------------------------------
# pack/unpack contract
# ---------------------------------------------------------------------------

def test_pack_unpack_round_trip():
    inner = {
        F_CHANNEL_HASH: b"\xab" * 16,
        F_MSG_TYPE: "sync_request",
        0x03: 1234.5,
        0x08: b"\x00\xff" * 64,
        0x05: None,
    }
    wire = pack_fields(inner)
    assert set(wire) == {LXMF_FIELD_CUSTOM_TYPE, LXMF_FIELD_CUSTOM_DATA}
    assert wire[LXMF_FIELD_CUSTOM_TYPE] == ENVELOPE_TYPE
    assert unpack_fields(wire) == inner


def test_unpack_ignores_what_is_not_ours():
    assert unpack_fields({}) is None
    assert unpack_fields({F_CHANNEL_HASH: b"\x00" * 16}) is None
    assert unpack_fields({LXMF_FIELD_CUSTOM_TYPE: "someoneelse/v1",
                          LXMF_FIELD_CUSTOM_DATA: b"theirs"}) is None


def test_a_corrupt_envelope_is_ours_but_unreadable():
    corrupt = {LXMF_FIELD_CUSTOM_TYPE: ENVELOPE_TYPE,
               LXMF_FIELD_CUSTOM_DATA: b"\xc1 not msgpack"}
    assert unpack_fields(corrupt) is None
    assert is_protocol_envelope(corrupt)
    # A packed non-dict payload is no better than one that will not parse.
    assert unpack_fields({LXMF_FIELD_CUSTOM_TYPE: ENVELOPE_TYPE,
                          LXMF_FIELD_CUSTOM_DATA: b"\x91\x01"}) is None


# ---------------------------------------------------------------------------
# what goes on the wire
# ---------------------------------------------------------------------------

def _reserved_keys(fields: dict) -> set:
    return {k for k in fields if isinstance(k, int) and k <= 0x80}


def test_a_channel_message_claims_no_reserved_field_numbers(
        peer_factory, monkeypatch):
    alice = peer_factory("alice")
    bob = peer_factory("bob")
    ch_hash = alice.channel_mgr.create_channel("wire", "", "public")
    bob.storage.upsert_channel(ch_hash, "wire", "", alice.identity.hash_hex,
                               "public", time.time())
    bob.storage.subscribe(ch_hash)
    captured = sent_fields(alice, monkeypatch)

    alice.messaging.send_message(
        channel_hash_hex=ch_hash, content="enveloped",
        subscriber_hashes=[bob.identity.hash_hex],
    )

    assert captured, "nothing was sent"
    for fields in captured:
        assert _reserved_keys(fields) == set()
        assert fields[LXMF_FIELD_CUSTOM_TYPE] == ENVELOPE_TYPE


def test_control_messages_claim_no_reserved_field_numbers(
        peer_factory, monkeypatch):
    """Subscribe is representative: every control sender routes through the
    same pack_fields envelope."""
    alice = peer_factory("alice")
    bob = peer_factory("bob")
    ch_hash = alice.channel_mgr.create_channel("wire-ctl", "", "public")
    bob.storage.upsert_channel(ch_hash, "wire-ctl", "", alice.identity.hash_hex,
                               "public", time.time())
    captured = sent_fields(bob, monkeypatch)

    bob.subscription_mgr.subscribe(ch_hash, alice.identity.hash_hex)

    assert captured, "nothing was sent"
    for fields in captured:
        assert _reserved_keys(fields) == set()
        assert fields[LXMF_FIELD_CUSTOM_TYPE] == ENVELOPE_TYPE


# ---------------------------------------------------------------------------
# the Router's unwrap
# ---------------------------------------------------------------------------

def test_an_enveloped_channel_message_is_delivered(peer_factory):
    """End to end through the real send path and the Router's unwrap."""
    alice = peer_factory("alice")
    bob = peer_factory("bob")
    ch_hash = alice.channel_mgr.create_channel("e2e", "", "public")
    bob.storage.upsert_channel(ch_hash, "e2e", "", alice.identity.hash_hex,
                               "public", time.time())
    bob.storage.subscribe(ch_hash)

    alice.messaging.send_message(
        channel_hash_hex=ch_hash, content="through the envelope",
        subscriber_hashes=[bob.identity.hash_hex],
    )

    assert wait_for(lambda: bob.storage.get_messages(ch_hash))
    assert bob.storage.get_messages(ch_hash)[0]["content"] == "through the envelope"


def test_a_corrupt_envelope_is_dropped_at_the_router(peer_factory):
    """Claiming our envelope with an unreadable payload gets a message
    dropped, not fed to handlers or mistaken for a direct message."""
    alice = peer_factory("alice")
    bob = peer_factory("bob")
    befriend(alice, bob)

    alice.router.send(crafted_lxm(alice, bob, "corrupt", {
        LXMF_FIELD_CUSTOM_TYPE: ENVELOPE_TYPE,
        LXMF_FIELD_CUSTOM_DATA: b"\xc1 not msgpack",
    }))

    time.sleep(0.5)
    conversation = dm_hash_for(alice.identity.hash_hex, bob.identity.hash_hex)
    assert bob.storage.get_messages(conversation) == []


def test_bare_reserved_keys_from_another_client_are_not_a_channel_message(
        peer_factory):
    """LXMF's 0x01 is FIELD_EMBEDDED_LXMS; only fields unwrapped from our
    envelope may name a channel. A foreign message carrying 0x01 is a plain
    direct message, whatever those bytes happen to look like."""
    alice = peer_factory("alice")
    bob = peer_factory("bob")
    befriend(alice, bob)
    fake_channel = b"\xcd" * 16
    bob.storage.upsert_channel(fake_channel.hex(), "decoy", "",
                               alice.identity.hash_hex, "public", time.time())
    bob.storage.subscribe(fake_channel.hex())

    alice.router.send(crafted_lxm(alice, bob, "not a channel message",
                                  {F_CHANNEL_HASH: fake_channel}))

    conversation = dm_hash_for(alice.identity.hash_hex, bob.identity.hash_hex)
    assert wait_for(lambda: bob.storage.get_messages(conversation))
    assert bob.storage.get_messages(fake_channel.hex()) == []
