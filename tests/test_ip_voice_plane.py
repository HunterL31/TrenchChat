"""
IPVoiceTransport: voice frames over a real direct session's datagrams.

Two peers with a QUIC session between them, so the hello and accept, the
authorisation over them and the frames after them are the real exchange over a
real socket. The manager above it is tests/test_voice.py; this is the plane
under it, plus the one thing only the manager can answer: which plane a pair
streams over when it has both.
"""

import threading
import time

import pytest

from trenchchat.core.permissions import (
    PRESET_PRIVATE, ROLE_MEMBER, ROLE_OWNER, VOICE_CHAT,
)
from trenchchat.core.voice import VoiceManager
from trenchchat.network.base import (
    DIRECT_VOICE_PACKET_BYTES, PATH_DIRECT, PATH_RETICULUM,
)
from trenchchat.network.ip.voice_plane import IPVoiceTransport
from trenchchat.network.voice_transport import (
    PEER_IDLE, PEER_STREAMING, VOICE_PACKET_RATE_LIMIT,
)
from trenchchat.network.voice_wire import (
    VOICE_MESH_MAX_BITRATE, pack_audio, pack_hello, unpack_audio,
)

CHANNEL = "ab" * 16
FRAME = b"\x78" * 60


def wait_until(predicate, message: str, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(message)


class Heard:
    """Frames a plane handed up, by sender."""

    def __init__(self):
        self.frames: list[tuple[str, int, list[bytes]]] = []
        self.states: list[tuple[str, str]] = []
        self.lock = threading.Lock()

    def on_frames(self, peer_hex, seq, frames):
        with self.lock:
            self.frames.append((peer_hex, seq, list(frames)))

    def on_state(self, peer_hex, state):
        with self.lock:
            self.states.append((peer_hex, state))

    def count(self) -> int:
        with self.lock:
            return len(self.frames)


def plane_for(peer, heard: Heard, *, allow: bool = True) -> IPVoiceTransport:
    """A voice plane on this peer's session transport, in a channel."""
    plane = IPVoiceTransport(peer.ip_transport, peer.identity)
    plane.set_frame_callback(heard.on_frames)
    plane.set_peer_state_callback(heard.on_state)
    plane.set_authorize_callback(lambda _peer, _channel: allow)
    plane.start(CHANNEL)
    return plane


@pytest.fixture
def pair(peer_factory):
    """Two peers with a session, and a voice plane each."""
    alice = peer_factory("alice", direct=True)
    bob = peer_factory("bob", direct=True)
    yield alice, bob


def streaming_pair(alice, bob, heard_a, heard_b, **kwargs):
    """Two planes that have greeted each other and are streaming."""
    plane_a = plane_for(alice, heard_a, **kwargs)
    plane_b = plane_for(bob, heard_b, **kwargs)
    plane_a.connect(bob.identity.hash_hex)
    plane_b.connect(alice.identity.hash_hex)
    return plane_a, plane_b


def test_a_pair_greets_over_the_session_and_streams(pair):
    alice, bob = pair
    heard_a, heard_b = Heard(), Heard()
    plane_a, plane_b = streaming_pair(alice, bob, heard_a, heard_b)

    wait_until(lambda: plane_a.connected_peers() == {bob.identity.hash_hex},
               "alice never started streaming with bob")
    wait_until(lambda: plane_b.connected_peers() == {alice.identity.hash_hex},
               "bob never started streaming with alice")
    assert plane_a.peer_state(bob.identity.hash_hex) == PEER_STREAMING

    plane_a.send_frames(7, [FRAME, FRAME])
    wait_until(lambda: heard_b.count() >= 2, "the frames never arrived")

    with heard_b.lock:
        arrived = list(heard_b.frames)
    assert [(peer, seq) for peer, seq, _f in arrived] == \
        [(alice.identity.hash_hex, 7), (alice.identity.hash_hex, 8)], \
        "a bundle did not arrive as one frame per datagram, numbered on"
    assert all(len(frames) == 1 for _peer, _seq, frames in arrived)


def test_a_peer_the_gate_refuses_never_streams(pair):
    """The core enforcement layer, reached the way a peer reaches it: a hello
    on a session that is already up and authenticated."""
    alice, bob = pair
    heard_a, heard_b = Heard(), Heard()
    plane_a = plane_for(alice, heard_a)
    plane_b = plane_for(bob, heard_b, allow=False)

    plane_a.connect(bob.identity.hash_hex)
    time.sleep(0.3)

    assert plane_b.connected_peers() == set()
    assert plane_a.connected_peers() == set()
    plane_a.send_frames(1, [FRAME])
    time.sleep(0.2)
    assert heard_b.count() == 0


def test_a_hello_for_another_channel_is_ignored(pair):
    alice, bob = pair
    heard_a, heard_b = Heard(), Heard()
    plane_a = plane_for(alice, heard_a)
    plane_b = plane_for(bob, heard_b)

    alice.ip_transport.send_datagram(bob.identity.hash_hex,
                                     pack_hello(bytes.fromhex("cd" * 16)))
    time.sleep(0.3)

    assert plane_b.connected_peers() == set()
    assert plane_a.connected_peers() == set()


def test_frames_before_the_handshake_are_dropped(pair):
    alice, bob = pair
    heard_a, heard_b = Heard(), Heard()
    _plane_a = plane_for(alice, heard_a)
    _plane_b = plane_for(bob, heard_b)

    alice.ip_transport.send_datagram(
        bob.identity.hash_hex, pack_audio(1, [FRAME]))
    time.sleep(0.3)

    assert heard_b.count() == 0


def test_a_peer_that_floods_is_rate_limited(pair):
    alice, bob = pair
    heard_a, heard_b = Heard(), Heard()
    plane_a, plane_b = streaming_pair(alice, bob, heard_a, heard_b)
    wait_until(lambda: plane_b.connected_peers() == {alice.identity.hash_hex},
               "the pair never came up")

    for seq in range(VOICE_PACKET_RATE_LIMIT * 2):
        alice.ip_transport.send_datagram(
            bob.identity.hash_hex, pack_audio(seq, [FRAME]))
    time.sleep(0.5)

    assert heard_b.count() <= VOICE_PACKET_RATE_LIMIT, \
        "a peer sending twice the ceiling was not held to it"


def test_a_bye_ends_the_pair_on_both_sides(pair):
    alice, bob = pair
    heard_a, heard_b = Heard(), Heard()
    plane_a, plane_b = streaming_pair(alice, bob, heard_a, heard_b)
    wait_until(lambda: plane_b.connected_peers(), "the pair never came up")

    plane_a.disconnect(bob.identity.hash_hex)

    wait_until(lambda: not plane_b.connected_peers(),
               "the far side never noticed the goodbye")
    assert plane_a.peer_state(bob.identity.hash_hex) == PEER_IDLE


def test_a_session_that_goes_away_takes_the_pair_with_it(pair):
    alice, bob = pair
    heard_a, heard_b = Heard(), Heard()
    plane_a, plane_b = streaming_pair(alice, bob, heard_a, heard_b)
    wait_until(lambda: plane_a.connected_peers(), "the pair never came up")

    alice.ip_transport.close_session(bob.identity.hash_hex)
    wait_until(lambda: not alice.ip_transport.can_reach(bob.identity.hash_hex),
               "the session never closed")
    plane_a.tick()

    assert plane_a.connected_peers() == set()
    assert plane_a.peer_state(bob.identity.hash_hex) == PEER_IDLE


def test_a_frame_over_the_datagram_budget_is_refused(pair):
    """The budget is the path's, and the packer is told which path it is on."""
    with pytest.raises(ValueError):
        pack_audio(1, [b"y" * 200] * 8, DIRECT_VOICE_PACKET_BYTES)
    packed = pack_audio(1, [b"y" * 200] * 5, DIRECT_VOICE_PACKET_BYTES)
    assert len(packed) <= DIRECT_VOICE_PACKET_BYTES
    assert unpack_audio(packed)[0] == 1


# ---------------------------------------------------------------------------
# The manager over both planes
# ---------------------------------------------------------------------------

def _voice_channel(owner, member):
    """An invite-only channel both peers may hold voice in."""
    perms = dict(PRESET_PRIVATE)
    perms[ROLE_MEMBER] = list(perms.get(ROLE_MEMBER, [])) + [VOICE_CHAT]
    ch_hash = owner.channel_mgr.create_channel("direct-voice", "",
                                               permissions=perms)
    for peer in (owner, member):
        peer.storage.upsert_channel(ch_hash, "direct-voice", "",
                                    owner.identity.hash_hex, perms, time.time())
        peer.storage.subscribe(ch_hash)
        peer.storage.set_channel_permissions(ch_hash, perms)
        peer.storage.upsert_member(ch_hash, owner.identity.hash_hex, "Alice",
                                   role=ROLE_OWNER)
        peer.storage.upsert_member(ch_hash, member.identity.hash_hex, "Bob",
                                   role=ROLE_MEMBER)
    return ch_hash


def _manager(peer, **kwargs) -> VoiceManager:
    """A manager with both planes, driven by the peer's own router."""
    return VoiceManager(
        peer.identity, peer.storage, peer.router, peer.subscription_mgr,
        peer.config, transport=peer.voice_transport,
        direct_transport=IPVoiceTransport(peer.ip_transport, peer.identity),
        state_refresh_secs=0.5, roster_ttl_secs=30.0, **kwargs)


def test_a_pair_with_a_session_streams_over_it_and_the_roster_says_so(pair):
    """The manager picks the plane by the path, and the roster carries it."""
    alice, bob = pair
    ch_hash = _voice_channel(alice, bob)
    managers = [_manager(alice), _manager(bob)]
    try:
        assert managers[0].join_voice(ch_hash)
        assert managers[1].join_voice(ch_hash)

        wait_until(
            lambda: bob.identity.hash_hex in managers[0]._connected_peers(),
            "the pair never streamed", timeout=10.0)
        row = [entry for entry in managers[0].get_roster(ch_hash)
               if entry["identity_hash"] == bob.identity.hash_hex][0]

        assert row["path"] == PATH_DIRECT
        assert row["link_state"] == "streaming"
        assert managers[0].frame_stats()["paths"] == {
            bob.identity.hash_hex: PATH_DIRECT}
        own = [entry for entry in managers[0].get_roster(ch_hash)
               if entry["identity_hash"] == alice.identity.hash_hex][0]
        assert own["path"] is None
    finally:
        for manager in managers:
            manager.leave_voice()


def test_a_pair_with_no_session_stays_on_the_mesh_plane(pair):
    alice, bob = pair
    ch_hash = _voice_channel(alice, bob)
    alice.transport.unreachable.add(bob.identity.hash_hex)
    manager = _manager(alice)
    try:
        assert manager.join_voice(ch_hash)

        assert manager._path_for(bob.identity.hash_hex) == PATH_RETICULUM
        assert manager._plane_for(bob.identity.hash_hex) is \
            alice.voice_transport
    finally:
        manager.leave_voice()


def test_the_session_encodes_at_what_its_slowest_pair_affords(pair):
    """One encoder feeds every pair, so a mesh pair holds the whole session to
    what the mesh carries."""
    alice, bob = pair
    ch_hash = _voice_channel(alice, bob)
    alice.config.voice_bitrate = 64000
    manager = _manager(alice)
    try:
        assert manager.join_voice(ch_hash)
        assert manager.session_bitrate() == 64000, \
            "a session of direct pairs was held to the mesh's bitrate"

        alice.transport.unreachable.add(bob.identity.hash_hex)
        manager._upsert_entry(ch_hash, bob.identity.hash_hex, muted=False,
                              joined_at=time.time(), now=time.time())

        assert manager.session_bitrate() == VOICE_MESH_MAX_BITRATE
    finally:
        manager.leave_voice()
