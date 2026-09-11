"""
Router's per-peer choice of path, and what it does when the fast one fails.

A manager never says which path a message takes; Router picks the direct
session when there is one for that peer and the mesh otherwise. What matters
here is that the choice is per peer rather than per node, that a message never
travels the wrong way (nothing propagated goes over a session), that the
budgets and the inbound control ceiling follow the path, and that a direct
send which loses its acknowledgement is sent once over Reticulum rather than
lost.
"""

import time

import pytest

from tests.helpers import wait_for, wait_for_message, wait_for_subscriber
from trenchchat.core.protocol import F_MSG_TYPE, MT_PRESENCE, pack_fields
from trenchchat.network.base import (
    InboundMessage, PATH_DIRECT, PATH_RETICULUM, SendState, Transport,
    TransportLimits, direct_limits, reticulum_limits,
)
from trenchchat.network.router import CONTROL_RATE_BURST, Router


class FakePath(Transport):
    """A path that is up for exactly the peers a test says it is.

    Stands in for either side of the choice: as the direct path it can lose a
    session mid-message, and as the Reticulum path it simply has nobody on it.
    """

    def __init__(self):
        self.up: set[str] = set()
        self.sent: list[tuple[str, dict, str, bool]] = []
        self.drop_acknowledgements = False
        self._inbound_callback = None

    # --- message plane ---

    def send(self, dest_hex: str, fields: dict, content: str = "", *,
             on_delivered=None, on_failed=None, propagated: bool = False,
             envelope: bool = True) -> SendState:
        """Record the send, then either acknowledge it or lose the session."""
        if propagated or dest_hex not in self.up:
            return SendState.NO_PATH
        self.sent.append((dest_hex, fields, content, envelope))
        if self.drop_acknowledgements:
            if on_failed is not None:
                on_failed(dest_hex)
        elif on_delivered is not None:
            on_delivered(dest_hex)
        return SendState.SENT

    def can_reach(self, dest_hex: str) -> bool:
        """Whether a session is up for this peer."""
        return dest_hex in self.up

    def request_path(self, dest_hex: str) -> None:
        """Nothing to ask on this path."""

    def limits_for(self, dest_hex: str) -> TransportLimits:
        """The direct path's budgets."""
        return direct_limits()

    def drain(self, timeout: float) -> int:
        """Nothing is ever in flight here."""
        return 0

    def set_inbound_callback(self, callback) -> None:
        """Register the callback Router reads inbound messages through."""
        self._inbound_callback = callback

    def stop(self) -> None:
        """Nothing to tear down."""

    # --- for the tests ---

    def arrive(self, message: InboundMessage) -> None:
        """Deliver one message up to Router as this path."""
        self._inbound_callback(message)


@pytest.fixture
def router_with_paths(peer_factory):
    """A peer whose Router has both paths, with the direct one under control."""
    peer = peer_factory("alice")
    direct = FakePath()
    router = Router(peer.config, peer.identity, transport=peer.transport,
                    direct_transport=direct)
    return peer, router, direct


PEER = "bb" * 16


class TestPathSelection:
    def test_a_peer_with_a_session_is_sent_to_over_it(self, router_with_paths):
        peer, router, direct = router_with_paths
        direct.up.add(PEER)
        assert router.send(PEER, {1: b"x"}, "hello") is SendState.SENT
        assert [entry[0] for entry in direct.sent] == [PEER]
        assert peer.transport.outbox == []
        assert router.path_for(PEER) == PATH_DIRECT

    def test_a_peer_without_one_is_sent_to_over_reticulum(self, router_with_paths):
        peer, router, direct = router_with_paths
        router.send(peer.identity.hash_hex, {1: b"x"}, "hello")
        assert direct.sent == []
        assert peer.transport.outbox
        assert router.path_for(PEER) == PATH_RETICULUM

    def test_the_choice_is_per_peer(self, router_with_paths):
        peer, router, direct = router_with_paths
        direct.up.add(PEER)
        router.send(PEER, {}, "over the session")
        router.send(peer.identity.hash_hex, {}, "over the mesh")
        assert [entry[2] for entry in direct.sent] == ["over the session"]
        assert [m.content for m in peer.transport.outbox] == ["over the mesh"]

    def test_a_propagated_message_never_takes_a_session(self, router_with_paths):
        """Leaving mail with a node is for a peer who is not there."""
        peer, router, direct = router_with_paths
        direct.up.add(peer.identity.hash_hex)
        router.send(peer.identity.hash_hex, {}, "held for later", propagated=True)
        assert direct.sent == []
        assert peer.transport.outbox

    def test_reachable_means_either_path(self, router_with_paths):
        peer, router, direct = router_with_paths
        assert not router.can_reach(PEER)
        direct.up.add(PEER)
        assert router.can_reach(PEER)

    def test_a_session_is_a_peer_appearing_and_a_path_changing(self, peer_factory):
        alice = peer_factory("alice", direct=True)
        paths: list = []
        appeared: list = []
        alice.router.add_path_changed_callback(
            lambda peer_hex, path: paths.append((peer_hex, path)))
        alice.router.add_peer_appeared_callback(
            lambda peer_hex, _iface: appeared.append(peer_hex))
        bob = peer_factory("bob", direct=True)

        assert wait_for(lambda: appeared == [bob.identity.hash_hex],
                        msg="bob appearing on a new path")
        assert paths == [(bob.identity.hash_hex, PATH_DIRECT)]
        assert alice.router.path_for(bob.identity.hash_hex) == PATH_DIRECT

        alice.ip_transport.close_session(bob.identity.hash_hex)
        assert wait_for(
            lambda: paths[-1] == (bob.identity.hash_hex, PATH_RETICULUM),
            msg="the path falling back")
        assert alice.router.path_for(bob.identity.hash_hex) == PATH_RETICULUM


class TestLimitsFollowThePath:
    def test_budgets_are_the_paths_own(self, router_with_paths):
        peer, router, direct = router_with_paths
        assert router.limits_for(PEER) == reticulum_limits()
        direct.up.add(PEER)
        assert router.limits_for(PEER) == direct_limits()

    def test_the_control_ceiling_is_the_paths_own(self, router_with_paths):
        """A sender on the mesh is held to sixty a minute, a session to six
        hundred, and the path a message arrived on is what decides."""
        peer, router, direct = router_with_paths
        seen: list = []
        router.add_delivery_callback(seen.append)

        for _ in range(CONTROL_RATE_BURST + 10):
            direct.arrive(InboundMessage(
                source_hex=PEER, fields=pack_fields({F_MSG_TYPE: MT_PRESENCE}),
                path=PATH_DIRECT,
            ))
        assert len(seen) == CONTROL_RATE_BURST + 10

        other = "cc" * 16
        seen.clear()
        for _ in range(CONTROL_RATE_BURST + 10):
            peer.transport.accept(InboundMessage(
                source_hex=other, fields=pack_fields({F_MSG_TYPE: MT_PRESENCE}),
                path=PATH_RETICULUM,
            ))
        assert len(seen) == CONTROL_RATE_BURST


class TestFallbackToReticulum:
    def test_a_send_whose_session_dies_goes_again_over_reticulum(
            self, router_with_paths):
        peer, router, direct = router_with_paths
        direct.up.add(peer.identity.hash_hex)
        direct.drop_acknowledgements = True
        failed: list[str] = []

        state = router.send(peer.identity.hash_hex, {1: b"x"}, "carried anyway",
                            on_failed=failed.append)
        assert state is SendState.SENT
        assert len(direct.sent) == 1
        assert [m.content for m in peer.transport.outbox] == ["carried anyway"]
        assert failed == [], "the caller was told it failed while it was retried"

    def test_it_goes_again_only_once(self, peer_factory):
        """The retry is the mesh's, so a mesh failure is a real failure.

        Both paths are stand-ins here so the count is exact: one attempt over
        the session, one over the mesh, one failure told to the caller.
        """
        peer = peer_factory("alice")
        mesh, direct = FakePath(), FakePath()
        router = Router(peer.config, peer.identity, transport=mesh,
                        direct_transport=direct)
        gone = "dd" * 16
        direct.up.add(gone)
        direct.drop_acknowledgements = True
        failed: list[str] = []

        router.send(gone, {1: b"x"}, "nowhere to go", on_failed=failed.append)
        assert len(direct.sent) == 1
        assert mesh.sent == []
        assert failed == [gone]

    def test_the_receiver_stores_one_copy_of_a_message_sent_twice(
            self, peer_factory):
        """The duplicate a retry can produce is absorbed by the message id.

        Bob takes the message over the session and never acknowledges it, so
        Alice's send fails and goes again over the mesh: the same message id
        arrives twice and one row exists.
        """
        alice = peer_factory("alice", direct=True)
        bob = peer_factory("bob", direct=True)
        ch_hash = alice.channel_mgr.create_channel("both-paths", "", "public")
        bob.storage.upsert_channel(ch_hash, "both-paths", "",
                                   alice.identity.hash_hex, "public", time.time())
        bob.subscription_mgr.subscribe(ch_hash, alice.identity.hash_hex)
        assert wait_for_subscriber(bob, ch_hash, alice.identity.hash_hex)

        inbound = bob.ip_transport.session_for(alice.identity.hash_hex)
        inbound.send_ack = lambda *_args: None

        alice.messaging.send_message(
            channel_hash_hex=ch_hash, content="sent twice, stored once",
            subscriber_hashes=[bob.identity.hash_hex],
        )
        msg_id = alice.storage.get_latest_message_id(ch_hash)
        assert wait_for_message(bob.storage, ch_hash, msg_id)

        outbound = alice.ip_transport.session_for(bob.identity.hash_hex)
        assert outbound.pending_acks() == 1
        outbound.expire_pending(0.0)

        assert wait_for(lambda: alice.transport.outbox,
                        msg="the message going again over the mesh")
        time.sleep(0.3)
        stored = [row for row in bob.storage.get_messages(ch_hash, limit=50)
                  if row["message_id"] == msg_id]
        assert len(stored) == 1
