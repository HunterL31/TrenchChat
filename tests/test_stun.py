"""
The public address echo: its messages, and what it refuses to believe.

A binding request asks one question and a binding response answers it, and
everything here is about not believing an answer that was not to this question:
a response for another transaction, one from a server nobody asked, one whose
attribute runs off the end of the datagram. The client itself never asks
anything unless a user turned the setting on; that gate is tested against the
manager, in test_upgrade.py and test_adversarial.py.
"""

import contextlib
import socket
import struct
import threading

import pytest

from tests.fake_stun import StunResponder, answer
from trenchchat.network.ip import punch, stun
from trenchchat.network.ip.endpoint import bind_datagram_socket

TRANSACTION = b"\x11" * stun.TRANSACTION_BYTES
OTHER_TRANSACTION = b"\x22" * stun.TRANSACTION_BYTES


def _attribute(attr_type: int, value: bytes) -> bytes:
    """One attribute, padded to four bytes the way RFC 5389 writes them."""
    return (struct.pack("!HH", attr_type, len(value)) + value
            + b"\x00" * (-len(value) % 4))


def _message(msg_type: int, transaction_id: bytes, body: bytes = b"") -> bytes:
    return (struct.pack("!HH", msg_type, len(body)) + stun.COOKIE_BYTES
            + transaction_id + body)


def _mapped_value(host: str, port: int) -> bytes:
    """A plain MAPPED-ADDRESS value, which no masking is applied to."""
    packed = socket.inet_pton(
        socket.AF_INET6 if ":" in host else socket.AF_INET, host)
    family = stun.FAMILY_IPV6 if ":" in host else stun.FAMILY_IPV4
    return struct.pack("!BBH", 0, family, port) + packed


class TestTheRequest:
    def test_it_is_a_header_the_cookie_and_the_transaction(self):
        request = stun.build_request(TRANSACTION)
        assert len(request) == stun.HEADER_BYTES
        assert request[4:8] == stun.COOKIE_BYTES
        assert request[8:] == TRANSACTION
        assert struct.unpack_from("!H", request, 0)[0] == stun.BINDING_REQUEST

    def test_a_transaction_id_of_the_wrong_length_is_refused(self):
        with pytest.raises(ValueError):
            stun.build_request(b"\x11" * 11)

    def test_a_new_transaction_id_is_random_and_the_right_length(self):
        first, second = stun.new_transaction_id(), stun.new_transaction_id()
        assert len(first) == stun.TRANSACTION_BYTES
        assert first != second


class TestTellingStunApart:
    """What the endpoint asks of every datagram before QUIC sees it."""

    def test_a_request_names_its_own_transaction(self):
        assert stun.transaction_of(stun.build_request(TRANSACTION)) == TRANSACTION

    def test_a_response_names_it_too(self):
        response = stun.build_response(TRANSACTION, "203.0.113.7", 33445)
        assert stun.transaction_of(response) == TRANSACTION

    def test_anything_without_the_cookie_is_not_stun(self):
        header = struct.pack("!HH", stun.BINDING_REQUEST, 0) + b"\x00" * 16
        assert stun.transaction_of(header) is None

    def test_a_quic_packet_can_never_be_read_as_one(self):
        """Every QUIC packet sets a bit STUN leaves clear, long header or short."""
        for first in (0x80, 0xC0, 0x40, 0x7F):
            quic = bytes([first]) + b"\x00" * 3 + stun.COOKIE_BYTES + TRANSACTION
            assert stun.transaction_of(quic) is None

    def test_a_truncated_or_mis_sized_datagram_is_not_stun(self):
        assert stun.transaction_of(b"") is None
        assert stun.transaction_of(stun.build_request(TRANSACTION)[:-1]) is None
        assert stun.transaction_of(stun.build_request(TRANSACTION) + b"x") is None

    def test_a_probe_datagram_is_not_stun(self):
        assert stun.transaction_of(punch.probe_datagram(b"\x01" * 16)) is None


class TestTheResponse:
    def test_an_ipv4_address_round_trips_through_the_masking(self):
        response = stun.build_response(TRANSACTION, "203.0.113.7", 33445)
        assert stun.parse_response(response, TRANSACTION) == ("203.0.113.7", 33445)

    def test_an_ipv6_address_round_trips_too(self):
        response = stun.build_response(TRANSACTION, "2001:db8:ff::5", 42420)
        assert stun.parse_response(response, TRANSACTION) == \
            ("2001:db8:ff::5", 42420)

    def test_the_masking_is_real_and_not_a_pair_of_identities(self):
        """XOR-MAPPED-ADDRESS exists because a NAT rewriting payloads would
        otherwise find its own address in one and helpfully translate it."""
        response = stun.build_response(TRANSACTION, "203.0.113.7", 33445)
        assert socket.inet_aton("203.0.113.7") not in response

    def test_a_plain_mapped_address_is_the_fallback(self):
        response = _message(stun.BINDING_SUCCESS, TRANSACTION,
                            _attribute(stun.ATTR_MAPPED_ADDRESS,
                                       _mapped_value("198.51.100.9", 41000)))
        assert stun.parse_response(response, TRANSACTION) == ("198.51.100.9", 41000)

    def test_the_xor_form_is_preferred_when_both_are_there(self):
        xor = stun.build_response(TRANSACTION, "203.0.113.7", 33445)
        both = _message(
            stun.BINDING_SUCCESS, TRANSACTION,
            _attribute(stun.ATTR_MAPPED_ADDRESS,
                       _mapped_value("198.51.100.9", 41000))
            + xor[stun.HEADER_BYTES:])
        assert stun.parse_response(both, TRANSACTION) == ("203.0.113.7", 33445)

    def test_a_response_to_another_transaction_is_nothing(self):
        response = stun.build_response(OTHER_TRANSACTION, "203.0.113.7", 33445)
        assert stun.parse_response(response, TRANSACTION) is None

    def test_a_message_that_is_not_a_success_is_nothing(self):
        body = _attribute(stun.ATTR_XOR_MAPPED_ADDRESS,
                          _mapped_value("203.0.113.7", 33445))
        assert stun.parse_response(_message(0x0111, TRANSACTION, body),
                                   TRANSACTION) is None

    def test_an_attribute_running_off_the_end_is_refused(self):
        body = struct.pack("!HH", stun.ATTR_XOR_MAPPED_ADDRESS, 64) + b"\x00" * 4
        assert stun.parse_response(_message(stun.BINDING_SUCCESS, TRANSACTION,
                                            body), TRANSACTION) is None

    def test_an_unknown_family_is_not_an_address(self):
        value = struct.pack("!BBH", 0, 0x09, 1234) + b"\x00" * 4
        body = _attribute(stun.ATTR_XOR_MAPPED_ADDRESS, value)
        assert stun.parse_response(_message(stun.BINDING_SUCCESS, TRANSACTION,
                                            body), TRANSACTION) is None

    def test_a_response_with_no_address_at_all_reports_none(self):
        assert stun.parse_response(_message(stun.BINDING_SUCCESS, TRANSACTION),
                                   TRANSACTION) is None


class TestServerStrings:
    """What a user may put in the config, and what it resolves to."""

    def test_a_host_and_port_round_trips(self):
        assert stun.parse_server("stun.example.com:3478") == \
            ("stun.example.com", 3478)

    def test_a_host_with_no_port_gets_the_assigned_one(self):
        assert stun.parse_server("stun.example.com") == \
            ("stun.example.com", stun.DEFAULT_PORT)

    def test_a_bracketed_ipv6_literal_is_read_with_its_port(self):
        assert stun.parse_server("[2001:db8::1]:3478") == ("2001:db8::1", 3478)

    def test_a_bare_ipv6_literal_is_all_host(self):
        assert stun.parse_server("2001:db8::1") == ("2001:db8::1",
                                                    stun.DEFAULT_PORT)

    def test_nonsense_is_refused_rather_than_stored(self):
        assert stun.parse_server("") is None
        assert stun.parse_server("   ") is None
        assert stun.parse_server("stun.example.com:not-a-port") is None
        assert stun.parse_server("stun.example.com:0") is None
        assert stun.parse_server("stun.example.com:70000") is None
        assert stun.parse_server("two words:3478") is None
        assert stun.parse_server("[2001:db8::1") is None
        assert stun.parse_server("x" * 300) is None

    def test_a_literal_resolves_to_itself(self):
        assert stun.resolve("127.0.0.1:3478") == [("127.0.0.1", 3478)]

    def test_a_name_that_resolves_to_nothing_is_no_server(self):
        assert stun.resolve("not-a-host.invalid:3478") == []
        assert stun.resolve("nonsense") == []


@contextlib.contextmanager
def socket_channel(transaction_id: bytes, server: tuple[str, int]):
    """A BindingChannel over a socket of the test's own.

    The transport feeds a channel from the loop its endpoint runs on; a test
    that is only about the transaction feeds one from a thread, so what is
    under test is the exchange and not the transport around it.
    """
    sock = bind_datagram_socket("127.0.0.1", 0)

    def _send(data: bytes, addr) -> bool:
        try:
            sock.sendto(data, addr)
        except OSError:
            return False
        return True

    channel = stun.BindingChannel(transaction_id, _send, server)
    stop = threading.Event()

    def _read() -> None:
        sock.settimeout(0.1)
        while not stop.is_set():
            try:
                data, source = sock.recvfrom(1500)
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                return
            channel.deliver(data, source)

    reader = threading.Thread(target=_read, daemon=True)
    reader.start()
    try:
        yield channel, sock.getsockname()
    finally:
        stop.set()
        channel.close()
        reader.join(timeout=2.0)
        sock.close()


@pytest.fixture
def responder():
    """A STUN server of this test's own, on loopback."""
    server = StunResponder("127.0.0.1", 0).start()
    try:
        yield server
    finally:
        server.stop()


class TestTheTransaction:
    """One request, one answer, and the socket it was asked from."""

    def test_the_address_echoed_is_the_socket_that_asked(self, responder):
        with socket_channel(TRANSACTION, responder.address) as (channel, mine):
            result = stun.request(channel, timeout=3.0, rto=0.3)

        assert result.answered, "the responder never answered"
        assert result.address == mine
        assert result.requests_sent == 1
        assert responder.requests == 1

    def test_a_server_that_never_answers_costs_the_budget_and_no_more(self):
        dead = ("127.0.0.1", 9)
        with socket_channel(TRANSACTION, dead) as (channel, _mine):
            result = stun.request(channel, timeout=1.0, rto=0.2)

        assert not result.answered
        assert result.requests_sent > 1, "nothing was retransmitted"
        assert result.seconds < 2.0

    def test_an_answer_from_anybody_but_the_server_asked_is_dropped(self,
                                                                    responder):
        """An address echoed by somebody nobody asked is an address somebody
        else chose for this node."""
        elsewhere = ("127.0.0.1", responder.address[1] + 1)
        with socket_channel(TRANSACTION, elsewhere) as (channel, _mine):
            forged = stun.build_response(TRANSACTION, "198.51.100.9", 41000)
            assert channel.deliver(forged, ("203.0.113.250", 3478)) is True
            assert channel.address() is None

    def test_a_closed_channel_reads_nothing_more(self, responder):
        with socket_channel(TRANSACTION, responder.address) as (channel, _mine):
            channel.close()
            assert channel.send_request() is False
            response = stun.build_response(TRANSACTION, "203.0.113.7", 33445)
            channel.deliver(response, responder.address)
            assert channel.address() is None

    def test_the_responder_ignores_what_is_not_a_request(self, responder):
        """It answers one question and nothing else, which is the whole of it."""
        response = stun.build_response(TRANSACTION, "203.0.113.7", 33445)
        assert answer(response, ("127.0.0.1", 1234)) is None
        assert answer(b"GET / HTTP/1.1", ("127.0.0.1", 1234)) is None
