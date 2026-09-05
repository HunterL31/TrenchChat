"""
Integration tests for PresenceBeacon.

Uses the real TestTransport-backed peer_factory (see conftest.py) so beacon
messages actually travel from one peer's router to another's, plus the
time-patching pattern from test_presence.py to make the silence threshold
deterministic without sleeping.
"""

import time
from unittest.mock import patch

import LXMF
import RNS

from tests.helpers import wait_for, wait_for_subscriber
from trenchchat.core.presence import PresenceBeacon, PresenceManager
from trenchchat.core import sync_ranges
from trenchchat.core.protocol import F_SYNC_PROBE, F_MSG_TYPE, MT_PRESENCE
from trenchchat.network.router import Router


def _beacons(received):
    """The presence beacons among captured deliveries. The subscription setup
    makes Alice broadcast a subscriber_list to Bob, which can race into a
    test's delivery capture; keying on MT_PRESENCE keeps that setup traffic
    from being mistaken for (or masking) a beacon."""
    return [m for m in received if m.fields.get(F_MSG_TYPE) == MT_PRESENCE]


def _beaconing_peer(peer_factory, beacon_after_secs: float = 30.0):
    """Alice owns an open channel Bob subscribes to, giving Alice a channel
    peer (Bob) to beacon. Returns (alice, bob, ch_hash, presence_mgr, beacon)."""
    alice = peer_factory("alice")
    bob = peer_factory("bob")

    ch_hash = alice.channel_mgr.create_channel("beacon-test", "", "public")
    bob.storage.upsert_channel(ch_hash, "beacon-test", "", alice.identity.hash_hex,
                               "public", time.time())
    bob.subscription_mgr.subscribe(ch_hash, owner_hash_hex=alice.identity.hash_hex)
    assert wait_for_subscriber(alice, ch_hash, bob.identity.hash_hex, timeout=5), \
        "Alice never saw Bob's subscription"

    presence_mgr = PresenceManager(alice.identity.hash_hex)
    beacon = PresenceBeacon(
        alice.identity, alice.storage, alice.router, alice.subscription_mgr,
        presence_mgr, beacon_after_secs=beacon_after_secs, jitter_fraction=0.0,
    )
    return alice, bob, ch_hash, presence_mgr, beacon


def test_beacon_fires_only_after_silence_threshold(peer_factory):
    alice, bob, ch_hash, presence_mgr, beacon = _beaconing_peer(
        peer_factory, beacon_after_secs=30.0
    )

    received = []
    bob.router.add_delivery_callback(lambda m: received.append(m))

    now = time.time()
    with patch("trenchchat.core.presence.time") as mock_time:
        mock_time.time.return_value = now
        presence_mgr.record_seen(bob.identity.hash_hex)

        # Well under the threshold -- no beacon.
        mock_time.time.return_value = now + 10
        beacon.tick()

    assert not wait_for(lambda: len(_beacons(received)) >= 1, timeout=1), \
        "beacon must not fire before the silence threshold elapses"

    with patch("trenchchat.core.presence.time") as mock_time:
        # Past the threshold -- beacon fires.
        mock_time.time.return_value = now + 31
        beacon.tick()

    assert wait_for(lambda: len(_beacons(received)) >= 1, timeout=5), \
        "beacon must fire once the silence threshold has elapsed"


def test_inbound_traffic_suppresses_the_beacon(peer_factory):
    """A record_seen refresh before the threshold resets the silence clock,
    exactly like PresenceManager's own online/offline timer."""
    alice, bob, ch_hash, presence_mgr, beacon = _beaconing_peer(
        peer_factory, beacon_after_secs=30.0
    )

    received = []
    bob.router.add_delivery_callback(lambda m: received.append(m))

    now = time.time()
    with patch("trenchchat.core.presence.time") as mock_time:
        mock_time.time.return_value = now
        presence_mgr.record_seen(bob.identity.hash_hex)

        # Refresh just before the threshold would have expired.
        mock_time.time.return_value = now + 25
        presence_mgr.record_seen(bob.identity.hash_hex)

        # Past the original threshold, but not the refreshed one.
        mock_time.time.return_value = now + 40
        beacon.tick()

    assert not wait_for(lambda: len(_beacons(received)) >= 1, timeout=1), \
        "fresh inbound evidence must suppress the beacon"


def test_outbound_only_contact_suppresses_beacon_but_not_online(peer_factory):
    """record_sent must silence the beacon without ever touching presence_mgr
    -- the two clocks are independent, and only inbound evidence marks a
    peer online."""
    alice, bob, ch_hash, presence_mgr, beacon = _beaconing_peer(
        peer_factory, beacon_after_secs=30.0
    )

    received = []
    bob.router.add_delivery_callback(lambda m: received.append(m))

    now = time.time()
    with patch("trenchchat.core.presence.time") as mock_time:
        mock_time.time.return_value = now
        beacon.record_sent(bob.identity.hash_hex)

        mock_time.time.return_value = now + 20
        beacon.tick()

        assert not presence_mgr.is_online(bob.identity.hash_hex), \
            "outbound-only contact must never mark the peer online"

    assert not wait_for(lambda: len(_beacons(received)) >= 1, timeout=1), \
        "outbound-only contact must suppress the beacon"


def test_beacon_message_carries_type_and_a_probe_per_shared_channel(peer_factory):
    """A beacon is the liveness type plus, for every channel shared with the
    peer, a probe of what we hold there. Nothing else rides on it."""
    alice, bob, ch_hash, presence_mgr, beacon = _beaconing_peer(
        peer_factory, beacon_after_secs=5.0
    )

    received = []
    bob.router.add_delivery_callback(lambda m: received.append(m))

    beacon.tick()  # never heard from Bob -- beacons immediately

    assert wait_for(lambda: len(_beacons(received)) >= 1, timeout=5)
    msg = _beacons(received)[0]
    assert msg.fields.get(F_MSG_TYPE) == MT_PRESENCE
    assert msg.content == b""
    assert set(msg.fields.keys()) == {F_MSG_TYPE, F_SYNC_PROBE}
    probes = sync_ranges.unpack_probes(msg.fields[F_SYNC_PROBE])
    assert probes is not None, "the beacon's probes did not validate"
    assert [entry[0].hex() for entry in probes] == [ch_hash]
    assert probes[0][1:] == alice.sync_mgr.local_probe(ch_hash)

def test_inbound_beacon_is_inert_for_invite_manager(peer_factory):
    """Regression test: invite.py's _on_lxmf_message used to log a WARNING
    ("control message missing channel hash, dropping") for any control
    message without F_CHANNEL_HASH -- a beacon storm would have spammed that
    on every tick. It must now ignore MT_PRESENCE before that check."""
    alice, bob, ch_hash, presence_mgr, beacon = _beaconing_peer(
        peer_factory, beacon_after_secs=5.0
    )

    received = []
    bob.router.add_delivery_callback(lambda m: received.append(m))

    logged: list[str] = []
    real_log = RNS.log

    def _capture(message, *args, **kwargs):
        logged.append(message)
        return real_log(message, *args, **kwargs)

    with patch("trenchchat.core.invite.RNS.log", side_effect=_capture):
        beacon.tick()  # never heard from Bob -- beacons immediately
        assert wait_for(lambda: len(_beacons(received)) >= 1, timeout=5), \
            "beacon never reached Bob"
        time.sleep(0.2)  # let every registered delivery callback finish, invite_mgr included

    assert not any("missing channel hash" in m for m in logged), \
        "invite.py must not warn about a beacon's missing channel hash"


def test_router_add_outbound_callback_fires_with_dest_identity_hex(peer_factory):
    """Router.add_outbound_callback (what backend_core.py wires to
    beacon.record_sent) must fire on every send with the recipient's raw
    identity hex -- the same value PresenceBeacon keys last_sent by.

    TestTransport replaces Router.send() on the instance to deliver
    in-process (see conftest.py), so it cannot exercise Router's own
    _notify_outbound path -- this test drives it directly instead.
    """
    alice = peer_factory("alice")
    bob = peer_factory("bob")

    seen: list[str] = []
    alice.router.add_outbound_callback(seen.append)

    identity_hash = bytes.fromhex(bob.identity.hash_hex)
    delivery_dest_hash = RNS.Destination.hash(identity_hash, "lxmf", "delivery")
    dest_identity = RNS.Identity.recall(delivery_dest_hash)
    assert dest_identity is not None, "Bob's identity must be locally known in-process"

    dest = RNS.Destination(
        dest_identity, RNS.Destination.OUT, RNS.Destination.SINGLE, "lxmf", "delivery",
    )
    lxm = LXMF.LXMessage(
        dest, alice.router.delivery_destination, "", desired_method=LXMF.LXMessage.DIRECT,
    )

    Router.send(alice.router, lxm)  # bypass the TestTransport instance patch

    assert seen == [bob.identity.hash_hex]
