"""
Hosting an RRC hub, against real clients.

A real RRCHubManager serves real RRCManager clients over the fake transport,
so what is under test is the hub's own behaviour: the WELCOME gate, the room
model, and the rule that the authenticated sender is the only sender a hub
will attribute a message to.

The caps are here too. A hub takes packets from anyone who can reach it, so
every one of its limits is something a stranger can push against, and a cap
that is advertised but not enforced is worse than no cap at all.
"""

import tempfile
from pathlib import Path

import pytest

from tests.fake_rrc import (
    FakeHostTransport, FakeHubRegistry, FakeRRCTransport, unwelcomed_session,
)
from tests.helpers import wait_for
from trenchchat.config import Config
from trenchchat.core.rrc import ROOM_JOINED, RRCManager
from trenchchat.core.rrc_hub import (
    ERR_BAD_ROOM, ERR_NOT_IN_ROOM, ERR_NOT_WELCOMED, ERR_NO_DIRECT, ERR_RATE,
    ERR_TOO_LONG, ERR_TOO_MANY_ROOMS, HUB_CAPABILITIES, HUB_LIMITS,
    RRCHubManager,
)
from trenchchat.core.rrc_wire import (
    CAP_ACTION, K_BODY, K_T, LIMIT_MSGS_PER_MINUTE,
    LIMIT_MSG_BODY_BYTES, LIMIT_ROOMS_PER_SESSION, T_ACTION, T_ERROR, T_MSG,
    T_NOTICE, pack_envelope,
)

HUB = "cc" * 16
ALICE = "a1" * 16
BOB = "b2" * 16
MALLORY = "de" * 16


class _Identity:
    def __init__(self, hex_hash: str, display_name: str):
        self.hash = bytes.fromhex(hex_hash)
        self.hash_hex = hex_hash
        self.display_name = display_name


@pytest.fixture
def hub_factory():
    """A real hub plus clients that connect to it, over the fake transport."""
    registry = FakeHubRegistry()
    tempdirs: list = []
    transports: list = []

    def _config() -> Config:
        tmp = tempfile.TemporaryDirectory()
        tempdirs.append(tmp)
        return Config(data_dir=Path(tmp.name))

    host_transport = FakeHostTransport(HUB, registry)
    hub = RRCHubManager(_config(), host_transport, hub_name="test hub")

    def client(name: str, hex_hash: str):
        transport = FakeRRCTransport(hex_hash, registry)
        transports.append(transport)
        return RRCManager(_Identity(hex_hash, name.capitalize()),
                          _config(), transport), transport

    yield hub, host_transport, client
    for transport in transports:
        transport.join_threads()
    for tmp in tempdirs:
        tmp.cleanup()


def _joined(hub, client, name, hex_hash, room="#general"):
    peer, transport = client(name, hex_hash)
    assert peer.connect(HUB)
    assert wait_for(peer.is_active, timeout=2.0)
    assert peer.join_room(room)
    assert wait_for(lambda: peer.rooms().get(room) == ROOM_JOINED, timeout=2.0)
    return peer, transport


class TestHosting:
    def test_starting_announces_and_reports_the_hash(self, hub_factory):
        hub, host, _ = hub_factory
        assert hub.start() == HUB
        assert hub.is_hosting()
        assert host.announces >= 1
        assert hub.status()["hub_hash"] == HUB

    def test_hosting_is_remembered_across_a_restart(self, hub_factory):
        hub, _, _ = hub_factory
        hub.start()
        hub.stop()
        assert hub.restore() is None
        hub.start()
        assert hub.restore() == HUB

    def test_stopping_drops_every_room(self, hub_factory):
        hub, _, client = hub_factory
        hub.start()
        _joined(hub, client, "alice", ALICE)
        assert hub.status()["rooms"] == {"#general": 1}
        hub.stop()
        assert hub.status()["rooms"] == {}
        assert hub.is_hosting() is False

    def test_the_advertised_limits_are_the_enforced_ones(self, hub_factory):
        """A WELCOME that promises a limit the hub does not keep would make
        every well-behaved client misjudge what it can send."""
        hub, _, _ = hub_factory
        hub.start()
        assert hub.status()["limits"] == HUB_LIMITS
        assert set(HUB_CAPABILITIES) == {CAP_ACTION}


class TestSession:
    def test_a_client_is_welcomed_and_can_join(self, hub_factory):
        hub, _, client = hub_factory
        hub.start()
        peer, _ = _joined(hub, client, "alice", ALICE)
        assert peer.session()["name"] == "test hub"
        assert hub.status()["clients"] == 1

    def test_nothing_is_processed_before_welcome(self, hub_factory):
        """The hub must refuse work from a client that has not said HELLO,
        which is the other half of the client's own rule."""
        hub, host, client = hub_factory
        hub.start()
        _, transport = client("mallory", MALLORY)
        session = unwelcomed_session(host, transport, MALLORY)

        host.handle(session, pack_envelope(T_MSG, src=bytes.fromhex(MALLORY),
                                           room="#general", body="sneaking in"))
        assert _hub_errors(host) == [ERR_NOT_WELCOMED]
        assert hub.status()["rooms"] == {}

    def test_a_leaving_client_empties_its_rooms(self, hub_factory):
        """A room with no members does not exist."""
        hub, host, client = hub_factory
        hub.start()
        peer, transport = _joined(hub, client, "alice", ALICE)
        assert hub.status()["rooms"] == {"#general": 1}
        transport.drop_link()
        assert wait_for(lambda: hub.status()["rooms"] == {}, timeout=2.0)
        assert hub.status()["clients"] == 0


class TestForwarding:
    def test_a_message_reaches_the_other_members(self, hub_factory):
        hub, _, client = hub_factory
        hub.start()
        alice, _ = _joined(hub, client, "alice", ALICE)
        bob, _ = _joined(hub, client, "bob", BOB)

        assert alice.send_message("#general", "hello room")
        assert wait_for(lambda: any(line["text"] == "hello room"
                                    for line in bob.lines("#general")),
                        timeout=2.0)

    def test_the_sender_does_not_get_its_own_message_back(self, hub_factory):
        """A hub forwards and does not echo; an echo would double every line
        for the one client that already has it."""
        hub, _, client = hub_factory
        hub.start()
        alice, _ = _joined(hub, client, "alice", ALICE)
        bob, _ = _joined(hub, client, "bob", BOB)
        alice.send_message("#general", "just once")
        assert wait_for(lambda: bob.lines("#general"), timeout=2.0)
        assert len([l for l in alice.lines("#general")
                    if l["text"] == "just once"]) == 1

    def test_the_source_is_the_authenticated_sender_not_the_claim(self, hub_factory):
        """A client may put anything in K_SRC. The link is the only thing
        that proves who it is, so the hub overwrites it."""
        hub, host, client = hub_factory
        hub.start()
        bob, _ = _joined(hub, client, "bob", BOB)
        mallory, transport = _joined(hub, client, "mallory", MALLORY)

        session = _raw_session(host, transport, MALLORY)
        host.handle(session, pack_envelope(
            T_MSG, src=bytes.fromhex(ALICE), room="#general",
            body="alice said this"))

        assert wait_for(lambda: any(line["text"] == "alice said this"
                                    for line in bob.lines("#general")),
                        timeout=2.0)
        line = [l for l in bob.lines("#general")
                if l["text"] == "alice said this"][0]
        assert line["source"] == MALLORY

    def test_a_message_to_a_room_not_joined_is_refused(self, hub_factory):
        hub, host, client = hub_factory
        hub.start()
        _, transport = _joined(hub, client, "alice", ALICE)
        session = _raw_session(host, transport, ALICE)

        host.handle(session, pack_envelope(T_MSG, src=bytes.fromhex(ALICE),
                                           room="#elsewhere", body="hi"))
        assert ERR_NOT_IN_ROOM in _hub_errors(host)

    def test_an_action_is_forwarded_as_an_action(self, hub_factory):
        hub, _, client = hub_factory
        hub.start()
        alice, _ = _joined(hub, client, "alice", ALICE)
        bob, _ = _joined(hub, client, "bob", BOB)
        assert alice.send_message("#general", "/me waves")
        assert wait_for(lambda: any(line["type"] == T_ACTION
                                    for line in bob.lines("#general")),
                        timeout=2.0)

    def test_a_notice_is_forwarded(self, hub_factory):
        hub, _, client = hub_factory
        hub.start()
        alice, _ = _joined(hub, client, "alice", ALICE)
        bob, _ = _joined(hub, client, "bob", BOB)
        assert alice.send_notice("#general", "heads up")
        assert wait_for(lambda: any(line["type"] == T_NOTICE
                                    for line in bob.lines("#general")),
                        timeout=2.0)


class TestCaps:
    def test_a_message_naming_no_room_is_refused(self, hub_factory):
        """A room name that is not one never gets this far: unpack_envelope
        drops it (test_rrc_wire covers that). What reaches the hub and has
        to be refused here is an envelope that names no room at all."""
        hub, host, client = hub_factory
        hub.start()
        _, transport = _joined(hub, client, "alice", ALICE)
        session = _raw_session(host, transport, ALICE)

        host.handle(session, _forge(T_MSG, ALICE, body="roomless"))
        assert ERR_BAD_ROOM in _hub_errors(host)
        assert hub.status()["rooms"] == {"#general": 1}

    def test_the_room_cap_bites(self, hub_factory):
        hub, host, client = hub_factory
        hub.start()
        peer, transport = _joined(hub, client, "alice", ALICE)
        session = _raw_session(host, transport, ALICE)

        for i in range(HUB_LIMITS[LIMIT_ROOMS_PER_SESSION] + 2):
            host.handle(session, _forge_join(ALICE, f"#room{i}"))
        assert ERR_TOO_MANY_ROOMS in _hub_errors(host)
        assert len(hub.status()["rooms"]) <= \
            HUB_LIMITS[LIMIT_ROOMS_PER_SESSION] + 1

    def test_an_oversized_body_is_refused(self, hub_factory):
        hub, host, client = hub_factory
        hub.start()
        peer, transport = _joined(hub, client, "alice", ALICE)
        bob, _ = _joined(hub, client, "bob", BOB)
        session = _raw_session(host, transport, ALICE)

        oversized = "x" * (HUB_LIMITS[LIMIT_MSG_BODY_BYTES] + 50)
        host.handle(session, _forge(T_MSG, ALICE, room="#general",
                                    body=oversized))
        assert ERR_TOO_LONG in _hub_errors(host)
        assert bob.lines("#general") == []

    def test_the_message_rate_limit_bites(self, hub_factory):
        hub, host, client = hub_factory
        hub.start()
        peer, transport = _joined(hub, client, "alice", ALICE)
        session = _raw_session(host, transport, ALICE)

        for i in range(HUB_LIMITS[LIMIT_MSGS_PER_MINUTE] + 5):
            host.handle(session, _forge(T_MSG, ALICE, room="#general",
                                        body=f"flood {i}"))
        assert ERR_RATE in _hub_errors(host)

    def test_a_direct_notice_is_refused_because_it_is_not_advertised(
            self, hub_factory):
        """The hub does not advertise CAP_DIRECT_NOTICE, so a client that
        sends one is told rather than left waiting for a delivery that will
        never happen."""
        hub, host, client = hub_factory
        hub.start()
        peer, transport = _joined(hub, client, "alice", ALICE)
        session = _raw_session(host, transport, ALICE)

        host.handle(session, pack_envelope(T_NOTICE, src=bytes.fromhex(ALICE),
                                           dst=bytes.fromhex(BOB), body="psst"))
        assert ERR_NO_DIRECT in _hub_errors(host)

    def test_an_unknown_message_type_is_ignored_not_refused(self, hub_factory):
        """Ignoring what it does not know is what lets the protocol grow."""
        hub, host, client = hub_factory
        hub.start()
        peer, transport = _joined(hub, client, "alice", ALICE)
        session = _raw_session(host, transport, ALICE)

        host.handle(session, pack_envelope(199, src=bytes.fromhex(ALICE),
                                           body="from the future"))
        assert _hub_errors(host) == []
        assert hub.status()["clients"] == 1


def _raw_session(host, transport, client_hex):
    """The FakeSession a client transport already opened on the host.

    Tests use it to put an envelope on the wire that no honest client would
    send, which is the only way to exercise the hub's own refusals.
    """
    return transport._session


def _hub_errors(host) -> list:
    """The error text of every ERROR the hub emitted, in order."""
    return [envelope.get(K_BODY) for _, envelope in host.sent
            if envelope.get(K_T) == T_ERROR]


def _forge(msg_type: int, src_hex: str, **fields) -> bytes:
    return pack_envelope(msg_type, src=bytes.fromhex(src_hex), **fields)


def _forge_join(src_hex: str, room: str) -> bytes:
    from trenchchat.core.rrc_wire import T_JOIN
    return pack_envelope(T_JOIN, src=bytes.fromhex(src_hex), room=room)
