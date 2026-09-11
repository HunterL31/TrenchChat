"""
The upgrade handshake: its wire, its candidates, its punch and its manager.

Two eligible peers trade one offer and one answer over Reticulum, punch a UDP
path between the candidates they name, and open a direct session over it.
Everything in the offer is asserted by a peer, so the first class here is
about what is refused on the way in; the last drives the whole flow between
two peers on loopback, with real probes and a real session at the end.
"""

import sys
import threading
import time

import pytest

from tests.helpers import wait_for
from trenchchat.core import actions
from trenchchat.core.permissions import PRESET_PRIVATE, ROLE_MEMBER, ROLE_OWNER
from trenchchat.core.protocol import (
    MAX_UPGRADE_CANDIDATES, MAX_UPGRADE_CERT_BYTES, MAX_UPGRADE_HOST_CHARS,
    UPGRADE_KIND_LAN, UPGRADE_KIND_MAPPED, UPGRADE_KIND_OBSERVED,
    F_UPGRADE_OBSERVED, UPGRADE_NONCE_BYTES, upgrade_address, upgrade_candidates,
    upgrade_certificate, upgrade_nonce, upgrade_punch_at,
)
from trenchchat.core.storage import Storage
from trenchchat.core.upgrade import (
    BACKOFF_MAX_SECS, BACKOFF_START_SECS, FALLBACK_OFFER_SECS, REASON_BACKOFF,
    REASON_DISABLED, REASON_HANDSHAKE_FAILED, REASON_INELIGIBLE,
    OFFER_TIMEOUT_SECS, REASON_NO_ANSWER, REASON_PUNCH_FAILED,
    UpgradeManager, is_eligible,
)
from trenchchat.network.base import PATH_DIRECT
from trenchchat.network.ip import candidates, punch


def _candidate(host: str = "10.0.0.5", port: int = 42420,
               kind: str = UPGRADE_KIND_LAN) -> list:
    return [host, port, kind]


class TestCandidateBounds:
    """What a candidate list may carry, checked before anything is stored."""

    def test_a_plain_list_round_trips(self):
        parsed = upgrade_candidates([_candidate(), _candidate("192.168.1.9", 1234,
                                                              UPGRADE_KIND_MAPPED)])
        assert parsed == [("10.0.0.5", 42420, UPGRADE_KIND_LAN),
                          ("192.168.1.9", 1234, UPGRADE_KIND_MAPPED)]

    def test_ipv6_is_a_candidate_like_any_other(self):
        assert upgrade_candidates([_candidate("fd00::1", 5000)]) == [
            ("fd00::1", 5000, UPGRADE_KIND_LAN)]

    def test_more_than_the_cap_refuses_the_whole_list(self):
        assert upgrade_candidates([_candidate()] * (MAX_UPGRADE_CANDIDATES + 1)) is None

    def test_exactly_the_cap_is_accepted(self):
        parsed = upgrade_candidates([_candidate()] * MAX_UPGRADE_CANDIDATES)
        assert len(parsed) == MAX_UPGRADE_CANDIDATES

    def test_a_host_that_is_not_an_address_is_dropped(self):
        assert upgrade_candidates([_candidate("peer.example.com")]) == []

    def test_an_overlong_host_is_dropped(self):
        assert upgrade_candidates([_candidate("a" * (MAX_UPGRADE_HOST_CHARS + 1))]) == []

    def test_a_port_outside_the_range_is_dropped(self):
        assert upgrade_candidates([_candidate(port=0)]) == []
        assert upgrade_candidates([_candidate(port=70000)]) == []

    def test_an_unknown_kind_is_dropped(self):
        assert upgrade_candidates([_candidate(kind="relay")]) == []

    def test_one_bad_entry_does_not_refuse_the_rest(self):
        parsed = upgrade_candidates([_candidate(), _candidate("not-an-address")])
        assert parsed == [("10.0.0.5", 42420, UPGRADE_KIND_LAN)]

    def test_anything_that_is_not_a_list_is_refused(self):
        assert upgrade_candidates(b"candidates") is None
        assert upgrade_candidates(None) is None

    def test_bytes_fields_are_read_as_text(self):
        assert upgrade_candidates([[b"10.0.0.5", 42420, b"lan"]]) == [
            ("10.0.0.5", 42420, UPGRADE_KIND_LAN)]


class TestObservedAddress:
    def test_a_pair_round_trips(self):
        assert upgrade_address(["203.0.113.7", 33445]) == ("203.0.113.7", 33445)

    def test_the_kind_it_is_read_as_is_observed(self):
        assert upgrade_candidates([_candidate(kind=UPGRADE_KIND_OBSERVED)]) == [
            ("10.0.0.5", 42420, UPGRADE_KIND_OBSERVED)]

    def test_a_malformed_pair_is_refused(self):
        assert upgrade_address(["203.0.113.7"]) is None
        assert upgrade_address(["nowhere", 1]) is None
        assert upgrade_address(None) is None


class TestNonceAndCertificateBounds:
    def test_a_sixteen_byte_nonce_is_the_only_one_accepted(self):
        nonce = b"\x01" * UPGRADE_NONCE_BYTES
        assert upgrade_nonce(nonce) == nonce
        assert upgrade_nonce(b"\x01" * (UPGRADE_NONCE_BYTES - 1)) is None
        assert upgrade_nonce(b"\x01" * (UPGRADE_NONCE_BYTES + 1)) is None
        assert upgrade_nonce(nonce.hex()) is None

    def test_a_certificate_over_two_kilobytes_is_refused(self):
        assert upgrade_certificate(b"\x30" * MAX_UPGRADE_CERT_BYTES)
        assert upgrade_certificate(b"\x30" * (MAX_UPGRADE_CERT_BYTES + 1)) is None
        assert upgrade_certificate(b"") is None
        assert upgrade_certificate("not bytes") is None


class TestPunchTime:
    def test_a_time_a_few_seconds_out_is_accepted(self):
        now = time.time()
        assert upgrade_punch_at(now + 3, now) == now + 3

    def test_a_time_an_hour_out_is_refused(self):
        now = time.time()
        assert upgrade_punch_at(now + 3600, now) is None

    def test_a_time_already_past_means_probe_now(self):
        now = time.time()
        assert upgrade_punch_at(now - 30, now) == now - 30

    def test_nonsense_is_refused(self):
        assert upgrade_punch_at("soon") is None
        assert upgrade_punch_at(float("inf")) is None
        assert upgrade_punch_at(None) is None


class TestCandidateGathering:
    """What this node offers a peer, gathered from the routing table alone."""

    def test_loopback_is_never_a_candidate(self):
        assert not candidates.is_reachable_address("127.0.0.1")
        assert not candidates.is_reachable_address("::1")
        assert all(host != "127.0.0.1"
                   for host, _port, _kind in candidates.gather(42420))

    def test_link_local_and_multicast_are_never_candidates(self):
        assert not candidates.is_reachable_address("169.254.3.4")
        assert not candidates.is_reachable_address("fe80::1")
        assert not candidates.is_reachable_address("239.255.255.250")
        assert not candidates.is_reachable_address("0.0.0.0")

    def test_a_local_address_carries_the_port_being_punched(self):
        gathered = candidates.gather(45678)
        assert gathered, "no local interface address was gathered"
        for host, port, kind in gathered:
            assert kind == UPGRADE_KIND_LAN
            assert port == 45678
            assert candidates.is_reachable_address(host)

    def test_a_mapped_and_an_observed_address_carry_their_own_ports(self):
        gathered = candidates.gather(45678, mapped=("203.0.113.7", 51820),
                                     observed=[("198.51.100.4", 33445)])
        assert gathered[0] == ("203.0.113.7", 51820, UPGRADE_KIND_MAPPED)
        assert gathered[1] == ("198.51.100.4", 33445, UPGRADE_KIND_OBSERVED)

    def test_the_list_never_exceeds_the_protocol_cap(self):
        observed = [(f"198.51.100.{n}", 30000 + n) for n in range(1, 20)]
        gathered = candidates.gather(45678, observed=observed)
        assert len(gathered) == MAX_UPGRADE_CANDIDATES

    def test_a_truncated_list_keeps_what_crosses_a_nat(self):
        observed = [(f"198.51.100.{n}", 30000 + n)
                    for n in range(1, MAX_UPGRADE_CANDIDATES + 4)]
        gathered = candidates.gather(45678, mapped=("203.0.113.7", 51820),
                                     observed=observed)
        assert gathered[0][2] == UPGRADE_KIND_MAPPED
        assert all(kind != UPGRADE_KIND_LAN for _host, _port, kind in gathered)

    def test_the_same_address_is_never_offered_twice(self):
        local = candidates.local_addresses()
        if not local:
            return
        gathered = candidates.gather(45678, observed=[(local[0], 45678)])
        assert len(gathered) == len({(h, p) for h, p, _k in gathered})

    def test_what_is_gathered_is_what_the_wire_accepts(self):
        gathered = candidates.gather(45678, mapped=("203.0.113.7", 51820))
        wire = [[host, port, kind] for host, port, kind in gathered]
        assert upgrade_candidates(wire) == gathered


class TestProbeDatagrams:
    """What a probe carries, and what it takes for one to count."""

    def test_a_probe_is_a_magic_and_the_nonce_and_nothing_else(self):
        nonce = b"\x11" * UPGRADE_NONCE_BYTES
        datagram = punch.probe_datagram(nonce)
        assert len(datagram) == punch.DATAGRAM_BYTES
        assert datagram.endswith(nonce)
        assert punch.read_datagram(datagram, nonce) == punch.KIND_PROBE

    def test_an_acknowledgement_is_told_apart_from_a_probe(self):
        nonce = b"\x11" * UPGRADE_NONCE_BYTES
        assert punch.read_datagram(punch.ack_datagram(nonce), nonce) == punch.KIND_ACK

    def test_another_attempts_nonce_is_not_this_attempts_probe(self):
        assert punch.read_datagram(punch.probe_datagram(b"\x22" * 16),
                                   b"\x11" * 16) is None

    def test_anything_that_is_not_a_probe_is_nothing(self):
        nonce = b"\x11" * UPGRADE_NONCE_BYTES
        assert punch.read_datagram(b"", nonce) is None
        assert punch.read_datagram(b"GET / HTTP/1.1", nonce) is None
        assert punch.read_datagram(punch.probe_datagram(nonce) + b"x", nonce) is None
        assert punch.read_datagram(b"junk" + nonce, nonce) is None


class TestPunchExchange:
    """Two sockets on loopback, probing each other the way two peers do."""

    def test_two_sockets_find_each_other_and_name_the_pair(self):
        nonce = b"\x33" * UPGRADE_NONCE_BYTES
        left = punch.bind_socket("127.0.0.1", 0)
        right = punch.bind_socket("127.0.0.1", 0)
        try:
            left_addr = left.getsockname()
            right_addr = right.getsockname()
            results = {}

            def _run(name, sock, targets):
                results[name] = punch.punch(sock, targets, nonce, seconds=4.0)

            threads = [
                threading.Thread(target=_run, args=("left", left, [right_addr])),
                threading.Thread(target=_run, args=("right", right, [left_addr])),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10.0)

            assert results["left"].punched and results["right"].punched
            assert results["left"].remote == right_addr
            assert results["right"].remote == left_addr
            assert results["left"].probes_sent >= 1
        finally:
            left.close()
            right.close()

    def test_an_unreachable_candidate_does_not_upset_the_punch(self):
        nonce = b"\x34" * UPGRADE_NONCE_BYTES
        left = punch.bind_socket("127.0.0.1", 0)
        right = punch.bind_socket("127.0.0.1", 0)
        try:
            right_addr = right.getsockname()
            unreachable = ("192.0.2.123", 9)
            results = {}

            def _run(name, sock, targets):
                results[name] = punch.punch(sock, targets, nonce, seconds=4.0)

            threads = [
                threading.Thread(target=_run,
                                 args=("left", left, [unreachable, right_addr])),
                threading.Thread(target=_run,
                                 args=("right", right, [left.getsockname()])),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10.0)
            assert results["left"].remote == right_addr
        finally:
            left.close()
            right.close()

    def test_a_probe_carrying_the_wrong_nonce_is_ignored(self):
        ours = b"\x35" * UPGRADE_NONCE_BYTES
        theirs = b"\x36" * UPGRADE_NONCE_BYTES
        listener = punch.bind_socket("127.0.0.1", 0)
        stranger = punch.bind_socket("127.0.0.1", 0)
        try:
            listener_addr = listener.getsockname()

            def _shout():
                deadline = time.time() + 2.5
                while time.time() < deadline:
                    stranger.sendto(punch.probe_datagram(theirs), listener_addr)
                    stranger.sendto(punch.ack_datagram(theirs), listener_addr)
                    time.sleep(0.1)

            noise = threading.Thread(target=_shout, daemon=True)
            noise.start()
            result = punch.punch(listener, [stranger.getsockname()], ours,
                                 seconds=2.0)
            noise.join(timeout=5.0)
            assert not result.punched
            assert result.probes_from == []
        finally:
            listener.close()
            stranger.close()

    def test_a_probe_arriving_on_another_socket_is_answered_there(self):
        nonce = b"\x37" * UPGRADE_NONCE_BYTES
        listening = punch.bind_socket("127.0.0.1", 0)
        caller = punch.bind_socket("127.0.0.1", 0)
        try:
            sent = []
            taken = punch.answer_probe(punch.probe_datagram(nonce),
                                       caller.getsockname(),
                                       lambda data, addr: sent.append((data, addr)),
                                       nonce)
            assert taken
            assert [data for data, _addr in sent] == [punch.ack_datagram(nonce),
                                                      punch.probe_datagram(nonce)]
            assert not punch.answer_probe(b"a quic packet, more or less",
                                          caller.getsockname(),
                                          lambda *_a: None, nonce)
        finally:
            listening.close()
            caller.close()

    def test_an_attempt_holds_until_the_time_it_named(self):
        nonce = b"\x38" * UPGRADE_NONCE_BYTES
        sock = punch.bind_socket("127.0.0.1", 0)
        try:
            started = time.monotonic()
            result = punch.punch(sock, [("127.0.0.1", 9)], nonce, seconds=0.4,
                                 start_at=time.time() + 0.5)
            assert not result.punched
            assert time.monotonic() - started >= 0.5
        finally:
            sock.close()


# ---------------------------------------------------------------------------
# The manager, over two peers that can see each other on the mesh
# ---------------------------------------------------------------------------

def _mirror_channel(owner, member, ch_hash, perms):
    """Give a member the same channel record and roster the owner published."""
    member.storage.upsert_channel(ch_hash, "upgrade-room", "",
                                  owner.identity.hash_hex, perms, time.time())
    member.storage.subscribe(ch_hash)
    member.storage.upsert_member(ch_hash, member.identity.hash_hex,
                                 member.name.capitalize(), role=ROLE_MEMBER)
    member.storage.upsert_member(ch_hash, owner.identity.hash_hex,
                                 owner.name.capitalize(), role=ROLE_OWNER)
    member.storage.set_channel_permissions(ch_hash, perms)


def _shared_invite_channel(owner, member) -> str:
    """One invite-only channel both peers are current members of."""
    perms = dict(PRESET_PRIVATE)
    ch_hash = owner.channel_mgr.create_channel("upgrade-room", "",
                                               permissions=perms)
    owner.invite_mgr.publish_member_list(ch_hash,
                                         add_members=[member.identity.hash])
    assert wait_for(lambda: owner.storage.is_member(ch_hash,
                                                    member.identity.hash_hex),
                    msg="the member list to name the member")
    _mirror_channel(owner, member, ch_hash, perms)
    return ch_hash


class UpgradePeer:
    """A peer with a direct transport, no session on it, and a manager over it.

    The transport is wired into the peer's Router the way backend_core wires
    it, so a session that comes up is a session the Router routes over.
    """

    def __init__(self, peer):
        self.peer = peer
        self.transport = peer.ip_transport
        self.transport.set_authorize(
            lambda peer_hex: is_eligible(peer.storage, peer.identity.hash_hex,
                                         peer_hex))
        self.manager = UpgradeManager(peer.identity, peer.storage, peer.router,
                                      peer.presence_mgr, peer.config,
                                      transport=self.transport)
        self.channel_hash = ""
        peer._teardown_callbacks.insert(0, self.manager.stop)

    @property
    def hash_hex(self) -> str:
        """This peer's identity hash."""
        return self.peer.identity.hash_hex

    def has_session_with(self, other: "UpgradePeer") -> bool:
        """Whether a direct session with the other peer is up."""
        return self.transport.can_reach(other.hash_hex)


@pytest.fixture
def upgrade_pair(peer_factory):
    """Alice and Bob, members of one invite-only channel, each able to upgrade."""
    alice = peer_factory("alice", direct=True, open_sessions=False)
    bob = peer_factory("bob", direct=True, open_sessions=False)
    channel_hash = _shared_invite_channel(alice, bob)
    pair = (UpgradePeer(alice), UpgradePeer(bob))
    for node in pair:
        node.channel_hash = channel_hash
    return pair


def _smaller_first(pair):
    """The pair ordered by identity hash, which is what decides who offers."""
    return tuple(sorted(pair, key=lambda node: node.hash_hex))


class TestTheWholeHandshake:
    """Two peers, one offer, one answer, real probes and a real session."""

    def test_a_sighting_brings_a_direct_session_up_both_ways(self, upgrade_pair):
        smaller, larger = _smaller_first(upgrade_pair)
        smaller.manager.on_peer_appeared(larger.hash_hex)

        assert wait_for(lambda: smaller.has_session_with(larger), timeout=30.0,
                        msg="the session the offer opened")
        assert wait_for(lambda: larger.has_session_with(smaller), timeout=10.0,
                        msg="the far side of the session")
        assert smaller.peer.router.path_for(larger.hash_hex) == PATH_DIRECT
        assert larger.peer.router.path_for(smaller.hash_hex) == PATH_DIRECT
        assert smaller.manager.failures() == {}

    def test_a_message_then_travels_over_the_session(self, upgrade_pair):
        smaller, larger = _smaller_first(upgrade_pair)
        smaller.manager.on_peer_appeared(larger.hash_hex)
        assert wait_for(lambda: smaller.has_session_with(larger), timeout=30.0,
                        msg="the session")

        smaller.peer.messaging.send_message(
            channel_hash_hex=smaller.channel_hash, content="over the direct path",
            subscriber_hashes=[larger.hash_hex],
        )
        message_id = smaller.peer.storage.get_latest_message_id(
            smaller.channel_hash)
        assert wait_for(lambda: larger.peer.storage.message_exists(message_id),
                        msg="the message over the session")

    def test_the_larger_hash_does_not_offer_until_the_fallback_passes(
            self, upgrade_pair):
        smaller, larger = _smaller_first(upgrade_pair)
        now = time.time()
        larger.manager.on_peer_appeared(smaller.hash_hex)

        assert larger.manager.consider(smaller.hash_hex, now) == REASON_BACKOFF
        assert larger.manager.attempt_count() == 0
        assert larger.manager.consider(
            smaller.hash_hex, now + FALLBACK_OFFER_SECS + 1) is None

    def test_the_smaller_hash_offers_on_the_first_sighting(self, upgrade_pair):
        smaller, larger = _smaller_first(upgrade_pair)
        assert smaller.manager.consider(larger.hash_hex) is None

    def test_the_fallback_offer_upgrades_a_pair_the_smaller_never_offered(
            self, upgrade_pair):
        smaller, larger = _smaller_first(upgrade_pair)
        larger.manager.on_peer_appeared(smaller.hash_hex)
        larger.peer.presence_mgr.record_seen(smaller.hash_hex)
        larger.manager.tick(time.time() + FALLBACK_OFFER_SECS + 1)

        assert wait_for(lambda: larger.has_session_with(smaller), timeout=30.0,
                        msg="the session the fallback offer opened")


class TestTheGate:
    """What the manager refuses, at the layer above the transport's own."""

    def test_a_peer_sharing_no_invite_only_channel_is_never_offered_one(
            self, peer_factory):
        alice = peer_factory("alice", direct=True, open_sessions=False)
        mallory = peer_factory("mallory", direct=False)
        node = UpgradePeer(alice)

        assert node.manager.consider(mallory.identity.hash_hex) == REASON_INELIGIBLE
        assert node.manager.offer(mallory.identity.hash_hex) == REASON_INELIGIBLE
        assert node.manager.attempt_count() == 0
        assert node.manager.failures()[mallory.identity.hash_hex]["reason"] == \
            REASON_INELIGIBLE

    def test_a_node_with_direct_sessions_off_neither_offers_nor_answers(
            self, upgrade_pair):
        smaller, larger = _smaller_first(upgrade_pair)
        larger.peer.config.upgrade_enabled = False

        assert larger.manager.consider(smaller.hash_hex) == REASON_DISABLED
        smaller.manager.on_peer_appeared(larger.hash_hex)
        assert not wait_for(lambda: smaller.has_session_with(larger),
                            timeout=5.0), "an offer was answered with sessions off"
        smaller.manager.tick(time.time() + OFFER_TIMEOUT_SECS + 1)
        assert smaller.manager.failures()[larger.hash_hex]["reason"] == \
            REASON_NO_ANSWER

    def test_try_now_re_applies_the_gate_for_an_ineligible_peer(self,
                                                                peer_factory):
        alice = peer_factory("alice", direct=True, open_sessions=False)
        mallory = peer_factory("mallory", direct=False)
        node = UpgradePeer(alice)

        result = actions.offer_upgrade(alice.storage, node.manager,
                                       alice.identity.hash_hex,
                                       mallory.identity.hash_hex)
        assert result == {"ok": False, "reason": REASON_INELIGIBLE}
        assert node.manager.attempt_count() == 0

    def test_try_now_starts_an_attempt_a_backoff_would_have_held(
            self, upgrade_pair):
        smaller, larger = _smaller_first(upgrade_pair)
        smaller.manager._record_failure(larger.hash_hex, REASON_PUNCH_FAILED)
        assert smaller.manager.consider(larger.hash_hex) == REASON_BACKOFF

        result = actions.offer_upgrade(smaller.peer.storage, smaller.manager,
                                       smaller.hash_hex, larger.hash_hex)
        assert result == {"ok": True, "reason": None}
        assert wait_for(lambda: smaller.has_session_with(larger), timeout=30.0,
                        msg="the session Try now opened")


class TestBackoff:
    """A pair that failed waits, and waits longer each time."""

    def test_it_doubles_from_thirty_seconds_to_a_day(self, upgrade_pair):
        smaller, larger = _smaller_first(upgrade_pair)
        manager = smaller.manager
        peer_hex = larger.hash_hex

        waits = []
        now = time.time()
        for _ in range(20):
            manager._record_failure(peer_hex, REASON_PUNCH_FAILED, now=now)
            entry = manager.failures()[peer_hex]
            waits.append(round(entry["next_attempt"] - entry["at"]))
            now = entry["next_attempt"] + 1
        assert waits[0] == BACKOFF_START_SECS
        assert waits[1] == BACKOFF_START_SECS * 2
        assert waits[2] == BACKOFF_START_SECS * 4
        assert waits[-1] == BACKOFF_MAX_SECS
        assert max(waits) == BACKOFF_MAX_SECS

    def test_the_same_reason_inside_the_wait_does_not_push_it_out(self,
                                                                  upgrade_pair):
        smaller, larger = _smaller_first(upgrade_pair)
        manager = smaller.manager
        peer_hex = larger.hash_hex
        now = time.time()

        manager._record_failure(peer_hex, REASON_PUNCH_FAILED, now=now)
        first = manager.failures()[peer_hex]["next_attempt"]
        for _ in range(5):
            manager._record_failure(peer_hex, REASON_PUNCH_FAILED, now=now + 1)
        assert manager.failures()[peer_hex]["next_attempt"] == first

    def test_a_peer_refused_as_ineligible_is_re_checked_on_the_next_sighting(
            self, upgrade_pair):
        smaller, larger = _smaller_first(upgrade_pair)
        manager = smaller.manager
        peer_hex = larger.hash_hex

        manager._record_failure(peer_hex, REASON_INELIGIBLE)
        assert manager.failures()[peer_hex]["next_attempt"] > time.time()
        assert manager.consider(peer_hex) is None
        assert manager.failures() == {}

    def test_a_failure_names_when_the_next_attempt_is(self, upgrade_pair):
        smaller, larger = _smaller_first(upgrade_pair)
        smaller.manager._record_failure(larger.hash_hex, REASON_HANDSHAKE_FAILED)
        entry = smaller.manager.failures()[larger.hash_hex]
        assert entry["reason"] == REASON_HANDSHAKE_FAILED
        assert entry["next_attempt"] > time.time()

    def test_a_changed_candidate_set_clears_the_wait(self, upgrade_pair):
        smaller, larger = _smaller_first(upgrade_pair)
        manager = smaller.manager
        peer_hex = larger.hash_hex

        manager._note_candidate_set(peer_hex, [("10.0.0.1", 4000, "lan")])
        manager._record_failure(peer_hex, REASON_PUNCH_FAILED)
        assert manager.consider(peer_hex) == REASON_BACKOFF

        manager._note_candidate_set(peer_hex, [("10.0.0.2", 4000, "lan")])
        assert manager.failures() == {}
        assert manager.consider(peer_hex) is None

    def test_the_same_candidate_set_does_not_clear_it(self, upgrade_pair):
        smaller, larger = _smaller_first(upgrade_pair)
        manager = smaller.manager
        peer_hex = larger.hash_hex

        manager._note_candidate_set(peer_hex, [("10.0.0.1", 4000, "lan")])
        manager._record_failure(peer_hex, REASON_PUNCH_FAILED)
        manager._note_candidate_set(peer_hex, [("10.0.0.1", 4000, "lan")])
        assert manager.consider(peer_hex) == REASON_BACKOFF


class TestObservedAddresses:
    """What a peer learns about its own address, and where it is kept."""

    def test_an_observed_address_is_remembered_across_a_restart(self,
                                                               upgrade_pair):
        smaller, larger = _smaller_first(upgrade_pair)
        smaller.manager._remember_self_address(
            larger.hash_hex, {F_UPGRADE_OBSERVED: ["203.0.113.7", 33445]})
        assert smaller.peer.storage.get_upgrade_addresses("self") == [
            ("203.0.113.7", 33445)]

        reopened = Storage(db_path=smaller.peer.data_dir / "storage.db")
        try:
            assert reopened.get_upgrade_addresses("self") == [
                ("203.0.113.7", 33445)]
        finally:
            reopened.close()

    def test_an_offer_carries_where_the_peer_was_last_seen(self, upgrade_pair):
        smaller, larger = _smaller_first(upgrade_pair)
        smaller.peer.storage.record_upgrade_address(
            larger.hash_hex, "peer", "198.51.100.9", 41000)
        fields = {}
        smaller.manager._add_observed(fields, larger.hash_hex)
        assert fields[F_UPGRADE_OBSERVED] == ["198.51.100.9", 41000]

    def test_a_nonsense_observation_is_not_stored(self, upgrade_pair):
        smaller, larger = _smaller_first(upgrade_pair)
        smaller.manager._remember_self_address(
            larger.hash_hex, {F_UPGRADE_OBSERVED: ["nowhere", 33445]})
        assert smaller.peer.storage.get_upgrade_addresses("self") == []

    def test_what_a_session_taught_us_is_offered_as_a_candidate(self,
                                                                upgrade_pair):
        """A pair that came up once leaves the dialer an address to offer.

        A node behind a NAT has no other way to learn its translated address:
        nothing inside its own network can see it, and asking a service for it
        would be a center. Both the probe exchange and the session's hello say
        so, and what either learns is a candidate for the next attempt.
        """
        smaller, larger = _smaller_first(upgrade_pair)
        smaller.manager.on_peer_appeared(larger.hash_hex)
        assert wait_for(lambda: smaller.has_session_with(larger), timeout=30.0,
                        msg="the session")

        assert wait_for(
            lambda: smaller.peer.storage.get_upgrade_addresses("self"),
            timeout=10.0, msg="the address the far side saw")
        learned = smaller.peer.storage.get_upgrade_addresses("self")
        host, port = learned[0]
        assert candidates.is_reachable_address(host)
        assert 1 <= port <= 65535
        offered = candidates.gather(45678, observed=learned)
        assert (host, port, UPGRADE_KIND_OBSERVED) in offered, \
            "an observed address never reached the candidate list"


class TestEligibilitySweep:
    """The once-a-second re-check, which is what a kick reaches."""

    def test_a_kicked_members_session_is_closed_within_a_second(self,
                                                               upgrade_pair):
        smaller, larger = _smaller_first(upgrade_pair)
        smaller.manager.on_peer_appeared(larger.hash_hex)
        assert wait_for(lambda: smaller.has_session_with(larger), timeout=30.0,
                        msg="the session")

        smaller.peer.storage.remove_member(smaller.channel_hash,
                                           larger.hash_hex)
        smaller.manager.tick()

        assert wait_for(lambda: not smaller.has_session_with(larger),
                        timeout=2.0, msg="the session to be torn down")
        assert smaller.manager.failures()[larger.hash_hex]["reason"] == \
            REASON_INELIGIBLE

    def test_a_still_eligible_peer_keeps_its_session(self, upgrade_pair):
        smaller, larger = _smaller_first(upgrade_pair)
        smaller.manager.on_peer_appeared(larger.hash_hex)
        assert wait_for(lambda: smaller.has_session_with(larger), timeout=30.0,
                        msg="the session")

        for _ in range(3):
            smaller.manager.tick()
            time.sleep(0.2)
        assert smaller.has_session_with(larger)


class TestNoAddressToName:
    """A node that can name no address of its own says nothing rather than
    offering a peer an empty list to refuse."""

    def test_an_offer_is_not_sent_without_a_candidate(self, upgrade_pair,
                                                      monkeypatch):
        smaller, larger = _smaller_first(upgrade_pair)
        monkeypatch.setattr(candidates, "local_addresses", lambda: [])

        smaller.manager.on_peer_appeared(larger.hash_hex)
        assert wait_for(
            lambda: smaller.manager.failures().get(larger.hash_hex, {})
            .get("reason") == REASON_PUNCH_FAILED,
            msg="the attempt to end for want of an address")
        assert not larger.has_session_with(smaller)


class TestInterfaceEnumeration:
    """The addresses a route probe cannot find, on the platform that will say."""

    def test_an_interface_address_is_found_without_a_route_towards_it(self):
        if sys.platform.startswith("linux"):
            found = candidates._interface_addresses()
            assert found, "no interface answered the address ioctl"
            assert "127.0.0.1" in found, "loopback was not enumerated"
        else:
            assert candidates._interface_addresses() == []

    def test_what_it_finds_still_goes_through_the_reachability_filter(self):
        assert "127.0.0.1" not in candidates.local_addresses()


class TestPunchFromOneSideOnly:
    """The case the namespace harness found: only one side can name the other.

    A peer behind a NAT has an address its own candidate list cannot carry, so
    the side that can be reached first has to probe back at wherever the probe
    came from. Modelled here by giving one side no candidate at all.
    """

    def test_a_probe_teaches_the_receiver_where_to_probe_back(self):
        nonce = b"\x39" * UPGRADE_NONCE_BYTES
        knows = punch.bind_socket("127.0.0.1", 0)
        unknown = punch.bind_socket("127.0.0.1", 0)
        try:
            results = {}

            def _run(name, sock, targets):
                results[name] = punch.punch(sock, targets, nonce, seconds=4.0)

            threads = [
                threading.Thread(target=_run,
                                 args=("knows", knows, [unknown.getsockname()])),
                threading.Thread(target=_run, args=("blind", unknown, [])),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10.0)

            assert results["knows"].remote == unknown.getsockname()
            assert results["blind"].remote == knows.getsockname()
        finally:
            knows.close()
            unknown.close()


class TestTheClientGate:
    """The switch a user turns off, and what it is a switch about."""

    def test_turning_it_off_closes_the_sessions_this_node_holds(self,
                                                               upgrade_pair):
        smaller, larger = _smaller_first(upgrade_pair)
        smaller.manager.on_peer_appeared(larger.hash_hex)
        assert wait_for(lambda: smaller.has_session_with(larger), timeout=30.0,
                        msg="the session")

        assert smaller.manager.set_enabled(False) is False

        assert wait_for(lambda: not smaller.has_session_with(larger),
                        timeout=10.0, msg="the session to close")
        assert smaller.manager.consider(larger.hash_hex) == REASON_DISABLED

    def test_an_offer_arriving_while_it_is_off_is_ignored(self, upgrade_pair):
        smaller, larger = _smaller_first(upgrade_pair)
        larger.manager.set_enabled(False)

        smaller.manager.on_peer_appeared(larger.hash_hex)

        assert not wait_for(lambda: smaller.has_session_with(larger),
                            timeout=8.0), \
            "a node with direct connections off answered an offer"
        assert larger.peer.config.upgrade_enabled is False

    def test_turning_it_back_on_lets_a_session_come_up(self, upgrade_pair):
        smaller, larger = _smaller_first(upgrade_pair)
        smaller.manager.set_enabled(False)
        assert smaller.manager.set_enabled(True) is True

        smaller.manager.on_peer_appeared(larger.hash_hex)

        assert wait_for(lambda: smaller.has_session_with(larger), timeout=30.0,
                        msg="the session after the switch came back")
