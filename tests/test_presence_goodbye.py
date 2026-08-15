"""
Integration tests for the graceful-shutdown notice (MT_GOODBYE).

Alice announces she is going offline; Bob must drop her to offline straight
away instead of waiting out PRESENCE_TIMEOUT_SECS -- and must let her back
online the moment he hears from her again, so a shutdown the user cancels
costs nothing.

Uses the real TestTransport-backed peer_factory (see conftest.py) so the
notice actually travels between two routers, same as test_presence_beacon.py.
"""

import time
from unittest.mock import patch

import RNS

from tests.helpers import wait_for, wait_for_subscriber
from trenchchat.core.presence import PresenceBeacon, PresenceManager
from trenchchat.core.protocol import F_MSG_TYPE, MT_GOODBYE


def _signing_off_peer(peer_factory):
    """Alice owns an open channel Bob subscribes to, so each is a channel peer
    of the other. Returns (alice, bob, ch_hash, alice_beacon, bob_presence)."""
    alice = peer_factory("alice")
    bob = peer_factory("bob")

    ch_hash = alice.channel_mgr.create_channel("goodbye-test", "", "public")
    bob.storage.upsert_channel(ch_hash, "goodbye-test", "", alice.identity.hash_hex,
                               "public", time.time())
    bob.subscription_mgr.subscribe(ch_hash, owner_hash_hex=alice.identity.hash_hex)
    assert wait_for_subscriber(alice, ch_hash, bob.identity.hash_hex, timeout=5), \
        "Alice never saw Bob's subscription"

    alice_presence = PresenceManager(alice.identity.hash_hex)
    alice_beacon = PresenceBeacon(
        alice.identity, alice.storage, alice.router, alice.subscription_mgr,
        alice_presence, jitter_fraction=0.0,
    )

    bob_presence = PresenceManager(bob.identity.hash_hex)
    bob.router.add_delivery_callback(bob_presence.record_inbound)

    return alice, bob, ch_hash, alice_beacon, bob_presence


def test_goodbye_marks_sender_offline(peer_factory):
    alice, bob, ch_hash, alice_beacon, bob_presence = _signing_off_peer(peer_factory)

    bob_presence.record_seen(alice.identity.hash_hex)
    assert bob_presence.is_online(alice.identity.hash_hex)

    alice_beacon.announce_offline()

    assert wait_for(
        lambda: not bob_presence.is_online(alice.identity.hash_hex), timeout=5
    ), "Bob never marked Alice offline on her going-offline notice"


def test_goodbye_fires_offline_callback(peer_factory):
    alice, bob, ch_hash, alice_beacon, bob_presence = _signing_off_peer(peer_factory)

    events: list[tuple] = []
    bob_presence.record_seen(alice.identity.hash_hex)
    bob_presence.add_presence_callback(lambda p, online: events.append((p, online)))

    alice_beacon.announce_offline()

    assert wait_for(lambda: events, timeout=5), "no presence callback fired"
    assert events == [(alice.identity.hash_hex, False)]


def test_sender_comes_back_online_after_goodbye(peer_factory):
    """A cancelled shutdown, or an immediate restart, must not be held against
    the peer: the next thing Bob hears from Alice puts her back online."""
    alice, bob, ch_hash, alice_beacon, bob_presence = _signing_off_peer(peer_factory)

    bob_presence.record_seen(alice.identity.hash_hex)
    alice_beacon.announce_offline()
    assert wait_for(
        lambda: not bob_presence.is_online(alice.identity.hash_hex), timeout=5
    ), "Bob never marked Alice offline"

    alice.messaging.send_message(
        channel_hash_hex=ch_hash,
        content="actually, still here",
        subscriber_hashes=[bob.identity.hash_hex],
    )

    assert wait_for(
        lambda: bob_presence.is_online(alice.identity.hash_hex), timeout=5
    ), "Alice never came back online after her goodbye"


def test_goodbye_message_carries_right_type_and_empty_content(peer_factory):
    alice, bob, ch_hash, alice_beacon, bob_presence = _signing_off_peer(peer_factory)

    received = []
    bob.router.add_delivery_callback(lambda m: received.append(m))

    alice_beacon.announce_offline()

    assert wait_for(lambda: len(received) >= 1, timeout=5)
    msg = received[0]
    assert msg.fields.get(F_MSG_TYPE) == MT_GOODBYE
    assert msg.content == b""
    # No "who is going offline" field: the notice can only ever apply to its own
    # authenticated sender, which is what makes it unspoofable.
    assert set(msg.fields.keys()) == {F_MSG_TYPE}


def test_goodbye_is_inert_for_invite_manager(peer_factory):
    """invite.py's _on_lxmf_message warns about any control message with no
    channel hash. It must ignore a goodbye, exactly as it ignores a beacon."""
    alice, bob, ch_hash, alice_beacon, bob_presence = _signing_off_peer(peer_factory)

    received = []
    bob.router.add_delivery_callback(lambda m: received.append(m))

    logged: list[str] = []
    real_log = RNS.log

    def _capture(message, *args, **kwargs):
        logged.append(message)
        return real_log(message, *args, **kwargs)

    with patch("trenchchat.core.invite.RNS.log", side_effect=_capture):
        alice_beacon.announce_offline()
        assert wait_for(lambda: len(received) >= 1, timeout=5), \
            "goodbye never reached Bob"
        time.sleep(0.2)  # let every registered delivery callback finish

    assert not any("missing channel hash" in m for m in logged), \
        "invite.py must not warn about a goodbye's missing channel hash"


def test_announce_offline_returns_once_sends_settle(peer_factory):
    """The drain must exit as soon as the sends stop moving, not sit out its
    whole budget -- it runs on the way out of the app."""
    alice, bob, ch_hash, alice_beacon, bob_presence = _signing_off_peer(peer_factory)

    started = time.time()
    delivered = alice_beacon.announce_offline(drain_secs=5.0)
    elapsed = time.time() - started

    assert delivered == 1, "the goodbye to Bob should have gone out"
    assert elapsed < 2.0, f"drain took {elapsed:.2f}s; it should exit early"


def test_announce_offline_with_no_channel_peers_is_a_noop(peer_factory):
    alice = peer_factory("alice")
    presence = PresenceManager(alice.identity.hash_hex)
    beacon = PresenceBeacon(
        alice.identity, alice.storage, alice.router, alice.subscription_mgr,
        presence, jitter_fraction=0.0,
    )
    assert beacon.announce_offline() == 0


def test_goodbye_only_marks_its_own_sender_offline(peer_factory):
    """A peer signing off must not take anyone else down with them."""
    alice, bob, ch_hash, alice_beacon, bob_presence = _signing_off_peer(peer_factory)
    carol_hex = "cc" * 16

    bob_presence.record_seen(alice.identity.hash_hex)
    bob_presence.record_seen(carol_hex)

    alice_beacon.announce_offline()

    assert wait_for(
        lambda: not bob_presence.is_online(alice.identity.hash_hex), timeout=5
    )
    assert bob_presence.is_online(carol_hex), \
        "Alice's goodbye must not affect Carol"
