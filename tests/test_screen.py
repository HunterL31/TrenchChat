"""
ScreenShareManager between real peers: shares travel direct sessions only.

Alice and Bob share an invite-only channel, a voice session and a direct
session; Carol shares the channel and the voice session and holds no direct
session with anyone. What Bob sees, Carol never does, and the mesh transport's
outbox shows nothing of it. The plane under this is tests/test_ip_screen_plane.py.
"""

import re
import time
from pathlib import Path

from tests.helpers import wait_for, wait_for_roster
from trenchchat.core.permissions import (
    PRESET_PRIVATE, ROLE_ADMIN, ROLE_MEMBER, ROLE_OWNER, SCREEN_SHARE,
    SEND_MESSAGE, VOICE_CHAT,
)
from trenchchat.core.protocol import (
    F_MSG_TYPE, MT_VOICE_JOIN, MT_VOICE_LEAVE, MT_VOICE_STATE, unpack_fields,
)
from trenchchat.core.screen.manager import (
    MAX_SCREEN_VIEWERS, REASON_NO_DIRECT, REASON_NO_PERMISSION,
    REASON_NO_SHARE, REASON_NOT_IN_VOICE_SELF, REASON_SESSION_LOST,
    REASON_STOPPED, REASON_VOICE_LEFT, SESSION_STARTED, SESSION_STOPPED,
    SHARE_STARTED, SHARE_STOPPED, ScreenShareManager,
)
from trenchchat.network.ip.screen_plane import REASON_FULL
from trenchchat.network.screen_wire import KIND_FULL

_VOICE_TYPES = {MT_VOICE_JOIN, MT_VOICE_LEAVE, MT_VOICE_STATE}


def _mirror(peer, owner, ch_hash, perms, members):
    peer.storage.upsert_channel(ch_hash, "screen-ch", "", owner.identity.hash_hex,
                                perms, time.time())
    peer.storage.subscribe(ch_hash)
    peer.storage.upsert_member(ch_hash, owner.identity.hash_hex, "Alice",
                               role=ROLE_OWNER)
    for member in members:
        peer.storage.upsert_member(ch_hash, member.identity.hash_hex,
                                   member.name.capitalize(), role=ROLE_MEMBER)
    peer.storage.set_channel_permissions(ch_hash, perms)


def setup_call(peer_factory, *, member_perms=None, with_carol=False):
    """Alice (owner) and Bob (member) direct and in voice; Carol in voice on
    the mesh only, when asked for."""
    alice = peer_factory("alice", direct=True)
    bob = peer_factory("bob", direct=True)
    carol = peer_factory("carol", direct=True, open_sessions=False) \
        if with_carol else None
    perms = dict(PRESET_PRIVATE)
    if member_perms is not None:
        perms[ROLE_MEMBER] = list(member_perms)
    ch_hash = alice.channel_mgr.create_channel("screen-ch", "", permissions=perms)
    members = [bob] + ([carol] if carol else [])
    for member in members:
        alice.storage.upsert_member(ch_hash, member.identity.hash_hex,
                                    member.name.capitalize(), role=ROLE_MEMBER)
    for member in members:
        _mirror(member, alice, ch_hash, perms, members)
    peers = [alice] + members
    for peer in peers:
        assert peer.voice_mgr.join_voice(ch_hash), f"{peer.name} could not join voice"
    for peer in peers:
        for other in peers:
            if other is not peer:
                assert wait_for_roster(peer, ch_hash, other.identity.hash_hex)
    return alice, bob, carol, ch_hash


class Recorder:
    def __init__(self, manager: ScreenShareManager):
        self.shares: list = []
        self.sessions: list = []
        self.viewers: list = []
        self.watches: list = []
        manager.add_share_callback(lambda p, c, s: self.shares.append((p, c, s)))
        manager.add_session_callback(lambda s, r: self.sessions.append((s, r)))
        manager.add_viewers_callback(self.viewers.append)
        manager.add_watch_callback(lambda p, r: self.watches.append((p, r)))


def held_from(peer, sharer) -> bool:
    return any(s["peer"] == sharer.identity.hash_hex
               for s in peer.screen_mgr.held_shares())


def start_and_hold(alice, bob, ch_hash):
    assert alice.screen_mgr.start_share(ch_hash) is None
    assert wait_for(lambda: held_from(bob, alice), msg="bob never held alice's share")


class TestSharing:
    def test_a_share_reaches_the_direct_participant_and_not_the_mesh_one(
            self, peer_factory):
        alice, bob, carol, ch_hash = setup_call(peer_factory, with_carol=True)
        bob_seen = Recorder(bob.screen_mgr)
        start_and_hold(alice, bob, ch_hash)
        held = bob.screen_mgr.held_shares()[0]
        assert held["channel"] == ch_hash and held["width"] == 320
        assert bob_seen.shares == [(alice.identity.hash_hex, ch_hash, SHARE_STARTED)]
        time.sleep(1.5)
        assert carol.screen_mgr.held_shares() == []
        assert alice.screen_mgr.sharing()["viewers"] == []

    def test_a_viewer_gets_a_full_frame_and_then_tiles(self, peer_factory):
        alice, bob, _carol, ch_hash = setup_call(peer_factory)
        start_and_hold(alice, bob, ch_hash)
        client = bob.screen_mgr.new_client()
        assert bob.screen_mgr.watch(alice.identity.hash_hex) is None
        assert wait_for(lambda: bob.screen_mgr.watching()["updates"] >= 3,
                        msg="updates never flowed to bob")
        first = bob.screen_mgr.next_for_client(client)
        assert first is not None and first.kind == KIND_FULL
        assert wait_for(lambda: bob.screen_mgr.next_for_client(client) is not None,
                        msg="no tile update for the client")
        assert wait_for(
            lambda: alice.screen_mgr.sharing()["viewers"][0]["updates"] >= 3,
            msg="alice never counted bob's updates")
        viewer = alice.screen_mgr.sharing()["viewers"][0]
        assert viewer["peer"] == bob.identity.hash_hex
        assert bob.screen_mgr.watching()["peer"] == alice.identity.hash_hex

    def test_nothing_of_a_share_touches_the_mesh(self, peer_factory):
        alice, bob, _carol, ch_hash = setup_call(peer_factory)
        before = 0
        start_and_hold(alice, bob, ch_hash)
        assert bob.screen_mgr.watch(alice.identity.hash_hex) is None
        assert wait_for(lambda: bob.screen_mgr.watching()["updates"] >= 5,
                        msg="updates never flowed")
        bob.screen_mgr.unwatch()
        alice.screen_mgr.stop_share()
        assert wait_for(lambda: not held_from(bob, alice), msg="share never dropped")
        crossed = (alice.transport.outbox + bob.transport.outbox)[before:]
        for message in crossed:
            fields = unpack_fields(message.fields) or {}
            assert fields.get(F_MSG_TYPE) in _VOICE_TYPES, \
                f"a non-voice message crossed the mesh during a share: {fields}"

    def test_protocol_has_no_screen_constant(self):
        source = Path("trenchchat/core/protocol.py").read_text()
        assert not re.search(r"SCREEN|screen", source), \
            "protocol.py names a screen field or type, which would let a share " \
            "be packed into an LXMF message"

    def test_stopping_drops_the_held_share_and_ends_the_watch(self, peer_factory):
        alice, bob, _carol, ch_hash = setup_call(peer_factory)
        alice_seen, bob_seen = Recorder(alice.screen_mgr), Recorder(bob.screen_mgr)
        start_and_hold(alice, bob, ch_hash)
        assert bob.screen_mgr.watch(alice.identity.hash_hex) is None
        assert wait_for(lambda: alice_seen.viewers and alice_seen.viewers[-1] == 1)
        assert alice.screen_mgr.stop_share()
        assert wait_for(lambda: not held_from(bob, alice), msg="share never dropped")
        assert bob.screen_mgr.watching() is None
        assert bob_seen.watches[-1] == (None, REASON_STOPPED)
        assert bob_seen.shares[-1] == (alice.identity.hash_hex, ch_hash, SHARE_STOPPED)
        assert alice_seen.sessions[0][0] == SESSION_STARTED
        assert alice_seen.sessions[-1][0] == SESSION_STOPPED
        assert alice.screen_mgr.sharing() is None

    def test_a_lost_session_ends_everything_and_a_returned_one_tells_again(
            self, peer_factory):
        alice, bob, _carol, ch_hash = setup_call(peer_factory)
        bob_seen = Recorder(bob.screen_mgr)
        start_and_hold(alice, bob, ch_hash)
        assert bob.screen_mgr.watch(alice.identity.hash_hex) is None
        assert wait_for(lambda: bob.screen_mgr.watching()["updates"] >= 1)
        before = len(bob.transport.outbox)
        assert alice.ip_transport.close_session(bob.identity.hash_hex, "test")
        assert wait_for(lambda: not held_from(bob, alice), msg="share never dropped")
        assert bob.screen_mgr.watching() is None
        assert bob_seen.watches[-1] == (None, REASON_SESSION_LOST)
        assert alice.screen_mgr.sharing()["viewers"] == []
        assert len(bob.transport.outbox) - before <= 2
        assert bob.ip_transport.open_session(
            alice.identity.hash_hex, "127.0.0.1", alice.ip_transport.listen_port,
            alice.ip_transport.certificate_der)
        assert wait_for(lambda: held_from(bob, alice), msg="bob was never told again")

    def test_the_share_ends_with_the_voice_session(self, peer_factory):
        alice, bob, _carol, ch_hash = setup_call(peer_factory)
        alice_seen = Recorder(alice.screen_mgr)
        start_and_hold(alice, bob, ch_hash)
        alice.voice_mgr.leave_voice()
        assert wait_for(lambda: alice.screen_mgr.sharing() is None)
        assert alice_seen.sessions[-1] == (SESSION_STOPPED, REASON_VOICE_LEFT)
        assert wait_for(lambda: not held_from(bob, alice), msg="share never dropped")

    def test_a_viewer_that_leaves_voice_is_dropped(self, peer_factory):
        alice, bob, _carol, ch_hash = setup_call(peer_factory)
        start_and_hold(alice, bob, ch_hash)
        assert bob.screen_mgr.watch(alice.identity.hash_hex) is None
        assert wait_for(lambda: len(alice.screen_mgr.sharing()["viewers"]) == 1)
        bob.voice_mgr.leave_voice()
        assert wait_for(lambda: alice.screen_mgr.sharing()["viewers"] == [],
                        msg="bob was never dropped as a viewer")
        assert bob.screen_mgr.watching() is None

    def test_a_slow_viewer_gets_fewer_updates_and_never_a_backlog(self, peer_factory):
        alice, bob, _carol, ch_hash = setup_call(peer_factory)
        original = bob.screen_mgr._on_update

        def slow(peer_hex, update):
            time.sleep(0.4)
            return original(peer_hex, update)

        bob.screen_mgr._plane.set_update_callback(slow)
        start_and_hold(alice, bob, ch_hash)
        assert bob.screen_mgr.watch(alice.identity.hash_hex) is None
        time.sleep(3.0)
        encoded = alice.screen_mgr.sharing()["encoder"]["updates_out"]
        received = bob.screen_mgr.watching()["updates"]
        assert 0 < received < encoded, (received, encoded)
        assert bob.screen_mgr.watching()["width"] == 320


class TestRefusals:
    def test_no_direct_transport_means_no_share_and_no_watch(self, peer_factory):
        alice, bob, _carol, ch_hash = setup_call(peer_factory)
        lonely = ScreenShareManager(alice.identity, alice.storage, alice.router,
                                    alice.voice_mgr, plane=None)
        assert lonely.start_share(ch_hash) == REASON_NO_DIRECT
        assert lonely.watch(bob.identity.hash_hex) == REASON_NO_DIRECT
        assert lonely.status()["available"]["ok"] is False

    def test_sharing_outside_voice_is_refused(self, peer_factory):
        alice, bob, _carol, ch_hash = setup_call(peer_factory)
        alice.voice_mgr.leave_voice()
        assert alice.screen_mgr.start_share(ch_hash) == REASON_NOT_IN_VOICE_SELF

    def test_sharing_without_the_permission_is_refused(self, peer_factory):
        alice, bob, _carol, ch_hash = setup_call(
            peer_factory, member_perms=[SEND_MESSAGE, VOICE_CHAT])
        assert bob.screen_mgr.start_share(ch_hash) == REASON_NO_PERMISSION
        assert alice.screen_mgr.start_share(ch_hash) is None

    def test_watching_a_share_nobody_offered_is_refused(self, peer_factory):
        alice, bob, _carol, ch_hash = setup_call(peer_factory)
        assert bob.screen_mgr.watch(alice.identity.hash_hex) == REASON_NO_SHARE

    def test_a_fifth_viewer_is_refused_full(self, peer_factory, monkeypatch):
        alice, bob, _carol, ch_hash = setup_call(peer_factory)
        start_and_hold(alice, bob, ch_hash)
        monkeypatch.setattr(alice.voice_mgr, "is_participant", lambda c, p: True)
        monkeypatch.setattr(alice.voice_mgr, "may_voice", lambda c, p: True)
        # A viewer with no session is dropped the moment its first update
        # cannot go; here the cap is under test, so every send succeeds.
        monkeypatch.setattr(alice.screen_mgr._plane, "send_update",
                            lambda peer, update, on_result=None: True)
        for index in range(MAX_SCREEN_VIEWERS):
            assert alice.screen_mgr._on_watch(f"{index:032x}", ch_hash, 640, 480) is None
        assert alice.screen_mgr._on_watch("ee" * 16, ch_hash, 640, 480) == REASON_FULL
        assert alice.screen_mgr._on_watch(f"{0:032x}", ch_hash, 640, 480) is None

    def test_a_share_from_a_peer_without_the_permission_is_dropped(
            self, peer_factory):
        alice, bob, _carol, ch_hash = setup_call(
            peer_factory, member_perms=[SEND_MESSAGE, VOICE_CHAT])
        info = {"channel": ch_hash, "width": 320, "height": 200, "tile_shift": 7,
                "fps": 15}
        assert alice.screen_mgr._on_started(bob.identity.hash_hex, info) is False
        assert alice.screen_mgr.held_shares() == []
        assert bob.screen_mgr._on_started(alice.identity.hash_hex, info) is True
        assert bob.screen_mgr.held_shares()[0]["peer"] == alice.identity.hash_hex

    def test_the_permission_reads_voice_chat_when_a_blob_predates_it(self, peer_factory):
        alice, bob, _carol, ch_hash = setup_call(peer_factory)
        older = {ROLE_ADMIN: [SEND_MESSAGE, VOICE_CHAT],
                 ROLE_MEMBER: [SEND_MESSAGE, VOICE_CHAT]}
        for peer in (alice, bob):
            peer.storage.set_channel_permissions(ch_hash, older)
        assert alice.storage.has_permission(ch_hash, bob.identity.hash_hex, SCREEN_SHARE)
        assert bob.screen_mgr.may_share(ch_hash, bob.identity.hash_hex)
        narrowed = {ROLE_ADMIN: [SEND_MESSAGE, VOICE_CHAT, SCREEN_SHARE],
                    ROLE_MEMBER: [SEND_MESSAGE, VOICE_CHAT]}
        alice.storage.set_channel_permissions(ch_hash, narrowed)
        assert not alice.screen_mgr.may_share(ch_hash, bob.identity.hash_hex)

