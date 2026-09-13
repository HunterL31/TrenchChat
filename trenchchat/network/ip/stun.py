"""
Asking a public server one question: where this node appears to come from.

Two peers both behind a NAT have nothing to aim at. A member that is already
reachable answers this for the whole channel through a session's hello, and
that is the answer this design prefers, because it asks nothing of anybody
outside the channel. What is left is a channel with no reachable member, and
for that pair the only remaining answer is a server outside both networks that
echoes the address it sees. RFC 5389's binding request is the smallest such
question: one datagram out, one back, no session, no account, no chat data.

It is off until a user turns it on, and what turning it on discloses is written
down in docs/security-improvements.md. Nothing here asks anything of the
network on its own: every function takes a channel the transport opened, and
the transport only opens one when the setting is on.

The request goes out of, and the answer comes back on, the socket this node
listens on. That is the whole point: the address a server echoes is the
translation of the socket it heard from, and an address for any other socket is
one a peer's probes cannot use. The endpoint routes a response here by its
transaction id, the way it routes a probe by its nonce; no QUIC packet can be
read as either, because both start with two zero bits where QUIC always has at
least one set.
"""

import ipaddress
import os
import socket
import struct
import threading
import time
from dataclasses import dataclass

import RNS

MAGIC_COOKIE = 0x2112A442
COOKIE_BYTES = struct.pack("!I", MAGIC_COOKIE)
TRANSACTION_BYTES = 12
HEADER_BYTES = 20

BINDING_REQUEST = 0x0001
BINDING_SUCCESS = 0x0101

ATTR_MAPPED_ADDRESS = 0x0001
ATTR_XOR_MAPPED_ADDRESS = 0x0020

FAMILY_IPV4 = 0x01
FAMILY_IPV6 = 0x02
ADDRESS_BYTES = {FAMILY_IPV4: 4, FAMILY_IPV6: 16}

# The port RFC 5389 assigns, used for a server written with no port of its own.
DEFAULT_PORT = 3478

# A server name can resolve to several addresses; one of each family is enough
# to learn what this node looks like in each.
MAX_SERVER_ADDRESSES = 2

# RFC 5389's retransmission schedule: a request, then another after RTO, with
# the wait doubling each time. The RFC's own total is 39.5 seconds, which is
# longer than this is worth: an attempt that waits that long has already
# failed and gone to backoff. Four requests inside eight seconds is the whole
# budget, and a server that has not answered by then is the next server's turn.
RTO_SECS = 0.5
MAX_REQUESTS = 4
TOTAL_WAIT_SECS = 8.0


def new_transaction_id() -> bytes:
    """Ninety-six random bits, which is all that ties a response to a request."""
    return os.urandom(TRANSACTION_BYTES)


def build_request(transaction_id: bytes) -> bytes:
    """One binding request: a header, the magic cookie, and no attributes."""
    if len(transaction_id) != TRANSACTION_BYTES:
        raise ValueError(f"a transaction id is {TRANSACTION_BYTES} bytes")
    return struct.pack("!HH", BINDING_REQUEST, 0) + COOKIE_BYTES + transaction_id


def transaction_of(data: bytes) -> bytes | None:
    """The transaction a datagram belongs to, or None when it is not one.

    What the endpoint asks of every datagram before QUIC sees it. The two
    leading zero bits and the magic cookie are what RFC 5389 gives for telling
    STUN apart from whatever else arrives on a multiplexed socket.
    """
    if len(data) < HEADER_BYTES or data[0] & 0xC0:
        return None
    if data[4:8] != COOKIE_BYTES:
        return None
    length = struct.unpack_from("!H", data, 2)[0]
    if length % 4 or HEADER_BYTES + length != len(data):
        return None
    return data[8:HEADER_BYTES]


def parse_response(data: bytes, transaction_id: bytes) -> tuple[str, int] | None:
    """The address a binding response reports, or None when it reports none.

    XOR-MAPPED-ADDRESS is what a server answers today and the only one a NAT
    rewriting payloads cannot quietly corrupt on the way back; MAPPED-ADDRESS
    is read as a fallback for a server that predates RFC 5389. A response whose
    transaction id is not this one is nothing at all.
    """
    if transaction_of(data) != transaction_id:
        return None
    if struct.unpack_from("!H", data, 0)[0] != BINDING_SUCCESS:
        return None
    mapped = None
    offset = HEADER_BYTES
    while offset + 4 <= len(data):
        attr_type, attr_length = struct.unpack_from("!HH", data, offset)
        offset += 4
        if offset + attr_length > len(data):
            return None
        value = data[offset:offset + attr_length]
        offset += attr_length + (-attr_length % 4)
        if attr_type == ATTR_XOR_MAPPED_ADDRESS:
            found = _read_address(value, transaction_id, xor=True)
            if found is not None:
                return found
        elif attr_type == ATTR_MAPPED_ADDRESS and mapped is None:
            mapped = _read_address(value, transaction_id, xor=False)
    return mapped


def build_response(transaction_id: bytes, host: str, port: int) -> bytes:
    """One binding response carrying XOR-MAPPED-ADDRESS, for a responder."""
    address = ipaddress.ip_address(host)
    family = FAMILY_IPV4 if address.version == 4 else FAMILY_IPV6
    mask = COOKIE_BYTES + transaction_id
    packed = bytes(b ^ m for b, m in zip(address.packed, mask))
    value = (struct.pack("!BBH", 0, family, port ^ (MAGIC_COOKIE >> 16))
             + packed)
    attribute = struct.pack("!HH", ATTR_XOR_MAPPED_ADDRESS, len(value)) + value
    return (struct.pack("!HH", BINDING_SUCCESS, len(attribute))
            + COOKIE_BYTES + transaction_id + attribute)


def _read_address(value: bytes, transaction_id: bytes,
                  *, xor: bool) -> tuple[str, int] | None:
    """One address attribute, unmasked where the attribute is the XOR one."""
    if len(value) < 4:
        return None
    size = ADDRESS_BYTES.get(value[1])
    if size is None or len(value) != 4 + size:
        return None
    port = struct.unpack_from("!H", value, 2)[0]
    packed = value[4:]
    if xor:
        port ^= MAGIC_COOKIE >> 16
        mask = COOKIE_BYTES + transaction_id
        packed = bytes(b ^ m for b, m in zip(packed, mask))
    try:
        return str(ipaddress.ip_address(packed)), port
    except ValueError:
        return None


def parse_server(text: str) -> tuple[str, int] | None:
    """A configured "host:port" server, or None when it is not one.

    The port is optional and RFC 5389's 3478 stands in for it. An IPv6 literal
    is bracketed the way a URL writes one, because a bare one is all colons and
    the last of them cannot be told from a port separator.
    """
    text = (text or "").strip()
    if not text or len(text) > 260:
        return None
    if text.startswith("["):
        host, closed, rest = text[1:].partition("]")
        if not closed:
            return None
        port_text = rest[1:] if rest.startswith(":") else ""
        if rest and not rest.startswith(":"):
            return None
    else:
        host, _found, port_text = text.rpartition(":")
        if not host or ":" in host:
            host, port_text = text, ""
    if not host or any(character.isspace() for character in host):
        return None
    if not port_text:
        return host, DEFAULT_PORT
    try:
        port = int(port_text)
    except ValueError:
        return None
    return (host, port) if 1 <= port <= 65535 else None


def resolve(server: str,
            limit: int = MAX_SERVER_ADDRESSES) -> list[tuple[str, int]]:
    """Every literal address a configured server names, one per family.

    Blocks on the resolver, so callers run it on a worker rather than on a
    callback thread. A name that resolves to nothing gives an empty list, which
    is the next server's turn rather than an error.
    """
    parsed = parse_server(server)
    if parsed is None:
        return []
    host, port = parsed
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
    except OSError as e:
        RNS.log(f"TrenchChat [stun]: could not resolve {server}: {e}",
                RNS.LOG_WARNING)
        return []
    found: list[tuple[str, int]] = []
    families: set[int] = set()
    for info in infos:
        address = info[4]
        try:
            version = ipaddress.ip_address(address[0]).version
        except ValueError:
            continue
        if version in families or len(found) >= limit:
            continue
        families.add(version)
        found.append((address[0], address[1]))
    return found


class BindingChannel:
    """One binding transaction's datagrams on the socket this node listens on.

    The endpoint hands it every datagram carrying this transaction id, on its
    loop; request() drives the retransmission from a worker thread, because
    waiting belongs on neither the loop nor an RNS callback thread. A response
    arriving from anywhere but the server this transaction asked is dropped: an
    address echoed by somebody who was not asked is an address somebody else
    chose for this node.
    """

    def __init__(self, transaction_id: bytes, send, server: tuple[str, int]):
        """
        transaction_id: the twelve bytes this transaction's datagrams carry
        send(data, (host, port)) -> bool: the endpoint's own sender
        server: the literal address asked, which is the only one answered from
        """
        self._transaction_id = transaction_id
        self._send = send
        self._server = (server[0], server[1])
        self._lock = threading.Lock()
        self._answered = threading.Event()
        self._address: tuple[str, int] | None = None
        self._closed = False

    @property
    def server(self) -> tuple[str, int]:
        """The server this transaction asked."""
        return self._server

    def send_request(self) -> bool:
        """Ask once. False when nothing here could send it."""
        with self._lock:
            if self._closed:
                return False
        return bool(self._send(build_request(self._transaction_id), self._server))

    def deliver(self, data: bytes, addr) -> bool:
        """Take one datagram and read it. Runs on the transport's loop."""
        if (addr[0], addr[1]) != self._server:
            RNS.log(f"TrenchChat [stun]: ignoring a response from "
                    f"{addr[0]}:{addr[1]}, which is not the server asked",
                    RNS.LOG_WARNING)
            return True
        found = parse_response(data, self._transaction_id)
        if found is None:
            return True
        with self._lock:
            if self._closed:
                return True
            self._address = found
        self._answered.set()
        return True

    def wait(self, timeout: float) -> bool:
        """Wait for an answer, or for this round to be over."""
        return self._answered.wait(timeout)

    def address(self) -> tuple[str, int] | None:
        """The address the server echoed, or None while none has arrived."""
        with self._lock:
            return self._address

    def close(self) -> None:
        """Stop reading: the transaction this channel belonged to is over."""
        with self._lock:
            self._closed = True
        self._answered.set()


@dataclass
class BindingResult:
    """What one transaction cost, and what it learned."""

    address: tuple[str, int] | None = None
    requests_sent: int = 0
    seconds: float = 0.0

    @property
    def answered(self) -> bool:
        """Whether the server echoed an address."""
        return self.address is not None


def request(channel: BindingChannel, *, timeout: float = TOTAL_WAIT_SECS,
            rto: float = RTO_SECS, requests: int = MAX_REQUESTS) -> BindingResult:
    """Ask one server until it answers or the budget is spent.

    RFC 5389's schedule, bounded: the wait after each request doubles, and the
    whole transaction is over at *timeout* whatever the schedule would say.
    """
    started = time.monotonic()
    deadline = started + timeout
    result = BindingResult()
    wait = rto
    for _attempt in range(requests):
        if not channel.send_request():
            break
        result.requests_sent += 1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if channel.wait(min(wait, remaining)):
            break
        wait *= 2
    result.address = channel.address()
    result.seconds = round(time.monotonic() - started, 3)
    return result
