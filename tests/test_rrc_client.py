"""
The RRC client against a correct hub.

These are the session rules a client has to keep for any hub to talk to it:
nothing but HELLO before WELCOME, join before speaking, a room that is only
a room while the hub says so, and a link that drops taking the whole session
with it. The hub here is deliberately well behaved; the adversarial cases
live in test_adversarial.py.
"""

import tempfile
from pathlib import Path

import pytest

from tests.fake_rrc import FakeHub, FakeHubRegistry, FakeRRCTransport
from tests.helpers import wait_for
from trenchchat.config import Config
from trenchchat.core.rrc import (
    MAX_ROOM_LINES, ROOM_JOINED, ROOM_JOINING, RRCManager,
)
from trenchchat.core.rrc_wire import (
    K_BODY, K_NICK, K_ROOM, K_T, LIMIT_MSG_BODY_BYTES,
    LIMIT_ROOMS_PER_SESSION, T_ACTION, T_HELLO, T_JOIN, T_MSG, T_NOTICE,
    pack_envelope, unpack_envelope,
)
from trenchchat.network.rrc_transport import SESSION_ACTIVE, SESSION_IDLE


class _Identity:
    """The two attributes RRCManager reads off an Identity."""

    def __init__(self, hex_hash: str, display_name: str):
        self.hash = bytes.fromhex(hex_hash)
        self.hash_hex = hex_hash
        self.display_name = display_name


@pytest.fixture
def rrc_factory():
    """Build RRC clients that share one registry of fake hubs."""
    registry = FakeHubRegistry()
    tempdirs: list = []
    transports: list = []

    def make(name: str, hex_hash: str, **kwargs):
        tmp = tempfile.TemporaryDirectory()
        tempdirs.append(tmp)
        config = Config(data_dir=Path(tmp.name))
        identity = _Identity(hex_hash, name.capitalize())
        transport = FakeRRCTransport(hex_hash, registry, **kwargs)
        transports.append(transport)
        return RRCManager(identity, config, transport), transport

    yield registry, make
    for transport in transports:
        transport.join_threads()
    for tmp in tempdirs:
        tmp.cleanup()


ALICE = "a1" * 16
BOB = "b2" * 16
HUB = "cc" * 16


def _connected(registry, make, name="alice", hex_hash=ALICE, **hub_kwargs):
    hub = FakeHub(HUB, **hub_kwargs)
    registry.add(hub)
    client, transport = make(name, hex_hash)
    assert client.connect(HUB)
    wait_for(client.is_active, timeout=2.0)
    return client, transport, hub


def _joined(registry, make, room="#general", **kwargs):
    client, transport, hub = _connected(registry, make, **kwargs)
    assert client.join_room(room)
    wait_for(lambda: client.rooms().get(room) == ROOM_JOINED, timeout=2.0)
    return client, transport, hub


class TestSession:
    def test_a_session_reaches_active_after_welcome(self, rrc_factory):
        registry, make = rrc_factory
        client, _, _ = _connected(registry, make)
        assert client.is_active()
        assert client.session()["state"] == SESSION_ACTIVE

    def test_hello_is_the_first_thing_sent(self, rrc_factory):
        """A client must send nothing but HELLO until WELCOME arrives."""
        registry, make = rrc_factory
        _, transport, hub = _connected(registry, make)
        first = unpack_envelope(transport.sent[0])
        assert first[K_T] == T_HELLO
        assert hub.received[0][K_T] == T_HELLO

    def test_the_welcome_carries_the_hub_name_and_limits(self, rrc_factory):
        registry, make = rrc_factory
        client, _, _ = _connected(registry, make)
        session = client.session()
        assert session["name"] == "fakehub"
        assert session["limits"][LIMIT_MSG_BODY_BYTES] > 0

    def test_a_hub_that_never_welcomes_leaves_the_session_inactive(self, rrc_factory):
        registry, make = rrc_factory
        hub = FakeHub(HUB)
        hub.withhold_welcome = True
        registry.add(hub)
        client, _ = make("alice", ALICE)
        assert client.connect(HUB)
        assert not client.is_active()
        assert client.join_room("#general") is False

    def test_an_unreachable_hub_does_not_look_connected(self, rrc_factory):
        registry, make = rrc_factory
        client, _ = make("alice", ALICE)
        assert client.connect("dd" * 16)
        assert not client.is_active()

    def test_connecting_elsewhere_replaces_the_session(self, rrc_factory):
        registry, make = rrc_factory
        second = FakeHub("ee" * 16, name="second")
        registry.add(second)
        client, _, _ = _joined(registry, make)
        assert client.connect(second.hub_hex)
        wait_for(client.is_active, timeout=2.0)
        assert client.session()["hub"] == second.hub_hex
        assert client.rooms() == {}

    def test_a_bad_hub_hash_is_refused(self, rrc_factory):
        _, make = rrc_factory
        client, _ = make("alice", ALICE)
        assert client.connect("not-hex") is False
        assert client.connect("") is False


class TestRooms:
    def test_joining_moves_through_joining_to_joined(self, rrc_factory):
        registry, make = rrc_factory
        client, _, _ = _connected(registry, make)
        seen: list[tuple[str, str]] = []
        client.add_room_callback(lambda room, state: seen.append((room, state)))
        assert client.join_room("#general")
        wait_for(lambda: client.rooms().get("#general") == ROOM_JOINED, timeout=2.0)
        assert ("#general", ROOM_JOINING) in seen
        assert ("#general", ROOM_JOINED) in seen

    def test_a_room_name_is_normalised_before_it_goes_out(self, rrc_factory):
        """Hubs fold case, so two clients typing it differently must agree."""
        registry, make = rrc_factory
        client, transport, _ = _connected(registry, make)
        assert client.join_room("General")
        wait_for(lambda: client.rooms().get("#general") == ROOM_JOINED, timeout=2.0)
        joins = [unpack_envelope(p) for p in transport.sent]
        assert any(e[K_T] == T_JOIN and e[K_ROOM] == "#general" for e in joins)

    def test_joining_is_refused_without_an_active_session(self, rrc_factory):
        _, make = rrc_factory
        client, _ = make("alice", ALICE)
        assert client.join_room("#general") is False

    def test_parting_drops_the_room_and_its_lines(self, rrc_factory):
        registry, make = rrc_factory
        client, _, _ = _joined(registry, make)
        client.send_message("#general", "before")
        assert client.lines("#general")
        assert client.part_room("#general")
        wait_for(lambda: "#general" not in client.rooms(), timeout=2.0)
        assert client.lines("#general") == []

    def test_parting_a_room_never_joined_is_refused(self, rrc_factory):
        registry, make = rrc_factory
        client, _, _ = _connected(registry, make)
        assert client.part_room("#nowhere") is False

    def test_the_roster_comes_from_the_hub(self, rrc_factory):
        registry, make = rrc_factory
        client, _, _ = _joined(registry, make)
        assert client.roster("#general") == [ALICE]

    def test_the_hubs_room_limit_is_respected(self, rrc_factory):
        registry, make = rrc_factory
        client, _, _ = _connected(
            registry, make,
            limits={LIMIT_ROOMS_PER_SESSION: 1, LIMIT_MSG_BODY_BYTES: 312},
        )
        assert client.join_room("#one")
        wait_for(lambda: client.rooms().get("#one") == ROOM_JOINED, timeout=2.0)
        assert client.join_room("#two") is False

    def test_a_refused_join_leaves_no_half_joined_room(self, rrc_factory):
        registry, make = rrc_factory
        client, _, hub = _connected(registry, make)
        hub.refuse_join_with = "that room is registered"
        errors: list[str] = []
        client.add_session_callback(
            lambda hub_hex, state, reason: errors.append(reason)
            if state == "error" else None)
        assert client.join_room("#locked")
        wait_for(lambda: "that room is registered" in errors, timeout=2.0)
        assert "#locked" not in client.rooms()


class TestMessages:
    def test_a_message_reaches_another_client_in_the_room(self, rrc_factory):
        registry, make = rrc_factory
        alice, _, hub = _joined(registry, make)
        bob, _ = make("bob", BOB)
        assert bob.connect(HUB)
        wait_for(bob.is_active, timeout=2.0)
        assert bob.join_room("#general")
        wait_for(lambda: bob.rooms().get("#general") == ROOM_JOINED, timeout=2.0)

        assert alice.send_message("#general", "hello room")
        wait_for(lambda: any(line["text"] == "hello room"
                             for line in bob.lines("#general")), timeout=2.0)
        line = [l for l in bob.lines("#general") if l["text"] == "hello room"][0]
        assert line["source"] == ALICE
        assert line["own"] is False

    def test_the_senders_own_line_is_recorded_locally(self, rrc_factory):
        """A hub forwards and does not echo, so the sender records its own."""
        registry, make = rrc_factory
        client, _, _ = _joined(registry, make)
        assert client.send_message("#general", "mine")
        lines = client.lines("#general")
        assert lines[-1]["text"] == "mine"
        assert lines[-1]["own"] is True

    def test_a_slash_me_becomes_an_action(self, rrc_factory):
        registry, make = rrc_factory
        client, transport, _ = _joined(registry, make)
        assert client.send_message("#general", "/me waves")
        sent = [unpack_envelope(p) for p in transport.sent]
        action = [e for e in sent if e[K_T] == T_ACTION]
        assert action and action[0][K_BODY] == "waves"
        assert client.lines("#general")[-1]["type"] == T_ACTION

    def test_a_notice_is_sent_as_a_notice(self, rrc_factory):
        registry, make = rrc_factory
        client, transport, _ = _joined(registry, make)
        assert client.send_notice("#general", "heads up")
        sent = [unpack_envelope(p) for p in transport.sent]
        assert any(e[K_T] == T_NOTICE for e in sent)

    def test_sending_to_a_room_not_joined_is_refused(self, rrc_factory):
        registry, make = rrc_factory
        client, _, _ = _connected(registry, make)
        assert client.send_message("#elsewhere", "hi") is False

    def test_an_empty_message_is_refused(self, rrc_factory):
        registry, make = rrc_factory
        client, _, _ = _joined(registry, make)
        assert client.send_message("#general", "   ") is False

    def test_a_long_line_is_trimmed_to_the_hubs_limit(self, rrc_factory):
        """The hub's advertised limit governs, not ours: a line over it
        would be refused, and silently losing the whole line is worse than
        sending the part that fits."""
        registry, make = rrc_factory
        client, transport, _ = _connected(
            registry, make,
            limits={LIMIT_MSG_BODY_BYTES: 16, LIMIT_ROOMS_PER_SESSION: 8},
        )
        assert client.join_room("#general")
        wait_for(lambda: client.rooms().get("#general") == ROOM_JOINED, timeout=2.0)
        assert client.send_message("#general", "x" * 100)
        sent = [unpack_envelope(p) for p in transport.sent]
        body = [e[K_BODY] for e in sent if e[K_T] == T_MSG][0]
        assert len(body.encode("utf-8")) <= 16

    def test_a_message_for_a_room_we_are_not_in_is_dropped(self, rrc_factory):
        """A hub can send anything; only rooms this client joined are shown."""
        registry, make = rrc_factory
        client, transport, hub = _joined(registry, make)
        transport._receive(HUB, pack_envelope(
            T_MSG, src=bytes.fromhex(BOB), room="#uninvited", body="noise"))
        assert client.lines("#uninvited") == []

    def test_the_transcript_is_bounded(self, rrc_factory):
        registry, make = rrc_factory
        client, _, _ = _joined(registry, make)
        for i in range(MAX_ROOM_LINES + 25):
            client.send_message("#general", f"line {i}")
        lines = client.lines("#general")
        assert len(lines) == MAX_ROOM_LINES
        assert lines[-1]["text"] == f"line {MAX_ROOM_LINES + 24}"


class TestLinkLoss:
    def test_a_dropped_link_clears_every_room(self, rrc_factory):
        """RRC keeps no continuity across a link: a new one is a new session,
        so nothing may be left looking joined."""
        registry, make = rrc_factory
        client, transport, _ = _joined(registry, make)
        client.send_message("#general", "before the drop")
        transport.drop_link()
        wait_for(lambda: not client.is_active(), timeout=2.0)
        assert client.rooms() == {}
        assert client.lines("#general") == []

    def test_disconnecting_reports_idle(self, rrc_factory):
        registry, make = rrc_factory
        client, _, _ = _joined(registry, make)
        client.disconnect()
        assert client.session()["state"] == SESSION_IDLE
        assert client.session()["hub"] is None


class TestPing:
    def test_a_ping_is_answered_with_a_pong(self, rrc_factory):
        registry, make = rrc_factory
        client, _, hub = _connected(registry, make)
        session = hub.sessions[ALICE]
        hub.ping(ALICE)
        wait_for(lambda: session.pongs >= 1, timeout=2.0)


class TestHubsAndBookmarks:
    def test_a_heard_hub_is_recorded_once(self, rrc_factory):
        _, make = rrc_factory
        client, _ = make("alice", ALICE)
        heard: list[str] = []
        client.add_hub_callback(lambda hub_hex, name: heard.append(hub_hex))
        client.note_hub(HUB, "coast hub")
        client.note_hub(HUB, "coast hub")
        assert heard == [HUB]
        assert client.known_hubs()[0]["name"] == "coast hub"

    def test_a_hub_name_is_a_label_not_an_identity(self, rrc_factory):
        """Announce text is unsigned, so control characters and length are
        the announcer's choice and must not be ours to inherit."""
        _, make = rrc_factory
        client, _ = make("alice", ALICE)
        client.note_hub(HUB, "evil\nname" + "x" * 200)
        name = client.known_hubs()[0]["name"]
        assert "\n" not in name and len(name) <= 64

    def test_bookmarks_survive_and_reject_rubbish(self, rrc_factory):
        _, make = rrc_factory
        client, _ = make("alice", ALICE)
        assert client.add_bookmark(HUB)
        assert client.bookmarks() == [HUB]
        assert client.add_bookmark("not-hex") is False
        assert client.remove_bookmark(HUB)
        assert client.bookmarks() == []


class TestNickname:
    def test_the_display_name_is_the_default_nickname(self, rrc_factory):
        _, make = rrc_factory
        client, _ = make("alice", ALICE)
        assert client.nickname() == "Alice"

    def test_a_nickname_is_set_and_sent(self, rrc_factory):
        registry, make = rrc_factory
        client, transport, _ = _joined(registry, make)
        assert client.set_nickname("al")
        assert client.nickname() == "al"
        client.send_message("#general", "hi")
        sent = [unpack_envelope(p) for p in transport.sent]
        assert any(e.get(K_NICK) == "al" for e in sent if e[K_T] == T_MSG)

    def test_a_nickname_with_control_characters_is_refused(self, rrc_factory):
        _, make = rrc_factory
        client, _ = make("alice", ALICE)
        assert client.set_nickname("bad\x00nick") is False
