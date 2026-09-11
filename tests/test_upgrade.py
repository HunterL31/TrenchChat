"""
The upgrade handshake: its wire, its candidates, its punch and its manager.

Two eligible peers trade one offer and one answer over Reticulum, punch a UDP
path between the candidates they name, and open a direct session over it.
Everything in the offer is asserted by a peer, so the first class here is
about what is refused on the way in; the last drives the whole flow between
two peers on loopback, with real probes and a real session at the end.
"""

import threading
import time

from trenchchat.core.protocol import (
    MAX_UPGRADE_CANDIDATES, MAX_UPGRADE_CERT_BYTES, MAX_UPGRADE_HOST_CHARS,
    UPGRADE_KIND_LAN, UPGRADE_KIND_MAPPED, UPGRADE_KIND_OBSERVED,
    UPGRADE_NONCE_BYTES, upgrade_address, upgrade_candidates,
    upgrade_certificate, upgrade_nonce, upgrade_punch_at,
)
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
