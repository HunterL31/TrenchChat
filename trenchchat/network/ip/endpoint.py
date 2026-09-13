"""
One UDP endpoint for every direct session this node has, dialled or accepted.

A punched pair only holds when each side sends from, and carries the session
on, the socket its candidates named: a router forwards the mapping that socket
opened and nothing else. So this node has one endpoint. It is where sessions
are accepted, where they are dialled from, and where an attempt's probes go
out, and inbound datagrams are told apart by QUIC's connection id, which every
packet after the first carries and which is unique per connection whichever
side opened it. A probe is told apart before that, by its magic and a nonce an
attempt is waiting on.

Both families run under the same endpoint. Where the platform lets one socket
carry them (Linux binds :: with IPV6_V6ONLY off and reads IPv4 as
::ffff:a.b.c.d) there is one; where it does not, or where the host has no IPv6
at all, there is one socket per family and the address being sent to picks
between them. Everything above this file works in ordinary addresses: a
v4-mapped address is unmapped on the way in and mapped again on the way out,
so a peer is never told about an address it could not dial.

aioquic's own server and connect() each bind a socket of their own, which is
the one thing this design cannot have; the demultiplexing here is the same
routing its server does, over sockets this node owns.
"""

import asyncio
import ipaddress
import socket

import RNS
from aioquic.buffer import Buffer
from aioquic.quic.configuration import SMALLEST_MAX_DATAGRAM_SIZE, QuicConfiguration
from aioquic.quic.connection import QuicConnection
from aioquic.quic.packet import (
    QuicPacketType, encode_quic_version_negotiation, pull_quic_header,
)

DUAL_STACK_HOST = "::"
IPV4_ANY_HOST = "0.0.0.0"
MAPPED_PREFIX = "::ffff:"

# Tries at binding both families on one port before settling for the one that
# is bound. Only the kernel-assigned case can collide, and only rarely.
PAIRED_BIND_ATTEMPTS = 4


def unmap_host(host: str) -> str:
    """An address as everything above this file names it.

    A dual-stack socket reads an IPv4 peer as ::ffff:a.b.c.d, which is this
    node's own socket talking about itself; a peer offered that address could
    not dial it from an IPv4-only host.
    """
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    mapped = getattr(address, "ipv4_mapped", None)
    return str(mapped) if mapped is not None else host


def socket_address(host: str, port: int, family: int) -> tuple | None:
    """The tuple a socket of this family sends to, or None if it cannot.

    An IPv4 address goes out of a dual-stack socket in its mapped form; an
    IPv6 address has no form an IPv4 socket can send to at all.
    """
    try:
        address = ipaddress.ip_address(unmap_host(host))
    except ValueError:
        return None
    if family == socket.AF_INET6:
        return ((MAPPED_PREFIX + str(address), port, 0, 0) if address.version == 4
                else (str(address), port, 0, 0))
    if address.version != 4:
        return None
    return (str(address), port)


def bind_datagram_socket(host: str, port: int) -> socket.socket:
    """A UDP socket bound where the caller asked, in the family it resolves to.

    The port is claimed exclusively, and SO_REUSEADDR is deliberately not set:
    on a UDP socket it lets a second socket bind the same port, after which the
    kernel decides which of them an arriving datagram reaches, and a second node
    on the host silently takes this one's sessions. UDP has no TIME_WAIT, so a
    port is free to bind again the moment it is closed either way.
    """
    info = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)[0]
    family, _type, _proto, _canonical, address = info
    sock = socket.socket(family, socket.SOCK_DGRAM)
    sock.bind(address)
    return sock


def _carries_ipv4(sock: socket.socket) -> bool:
    """Whether this socket can send to an IPv4 address in its mapped form."""
    if sock.family == socket.AF_INET:
        return True
    try:
        return sock.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY) == 0
    except OSError:
        return False


def _dual_stack_socket(port: int) -> socket.socket | None:
    """One socket for both families, or None where the platform refuses."""
    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    except OSError:
        return None
    try:
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        if sock.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY) != 0:
            sock.close()
            return None
        sock.bind((DUAL_STACK_HOST, port))
    except OSError:
        sock.close()
        return None
    return sock


def _paired_sockets(port: int) -> list[socket.socket]:
    """One socket per family on the same port, for a host that splits them."""
    for _attempt in range(PAIRED_BIND_ATTEMPTS):
        try:
            sixth = bind_datagram_socket(DUAL_STACK_HOST, port)
        except OSError:
            return [bind_datagram_socket(IPV4_ANY_HOST, port)]
        bound = sixth.getsockname()[1]
        try:
            return [sixth, bind_datagram_socket(IPV4_ANY_HOST, bound)]
        except OSError:
            sixth.close()
            if port:
                return [bind_datagram_socket(IPV4_ANY_HOST, port)]
    return [bind_datagram_socket(IPV4_ANY_HOST, port)]


def bind_listen_sockets(host: str, port: int) -> list[socket.socket]:
    """The sockets this node listens on, dual-stack where the platform allows.

    A host named explicitly gets exactly that one socket, which is what a test
    and a machine with one address to offer both want. The default asks for
    both families: one socket where IPV6_V6ONLY can be cleared, a socket each
    on the same port where it cannot, and an IPv4 socket alone on a host with
    no IPv6.
    """
    if host != DUAL_STACK_HOST:
        return [bind_datagram_socket(host, port)]
    sock = _dual_stack_socket(port)
    return [sock] if sock is not None else _paired_sockets(port)


class _Binding(asyncio.DatagramProtocol):
    """One socket under the endpoint, and what arrives on it."""

    def __init__(self, endpoint: "DatagramEndpoint", family: int, dual: bool):
        self.family = family
        self.dual = dual
        self.transport = None
        self._endpoint = endpoint

    def connection_made(self, transport) -> None:
        """Keep the transport, which is how anything is written back out."""
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        """Hand one datagram to the endpoint, which decides whose it is."""
        self._endpoint.receive(self, data, addr)

    def error_received(self, exc) -> None:
        """An ICMP refusal for a datagram already sent, which is not an error.

        A probe at a candidate nothing listens on earns one of these, and the
        attempt is meant to carry on probing the rest.
        """
        RNS.log(f"TrenchChat [ip]: datagram error: {exc}", RNS.LOG_DEBUG)

    def sendto(self, data: bytes, addr) -> None:
        """Write one datagram out of this socket."""
        if self.transport is not None:
            self.transport.sendto(data, addr)

    def close(self) -> None:
        """Drop the socket."""
        if self.transport is not None:
            self.transport.close()
            self.transport = None


class DatagramEndpoint:
    """Every direct session on one node, over the sockets it listens on."""

    def __init__(self, configuration: QuicConfiguration, create_protocol,
                 *, probe_router=None):
        """
        configuration: the listening side's, used for sessions accepted here
        create_protocol(connection) -> the session wrapping that connection
        probe_router(data, host_port) -> bool: sees every datagram before QUIC
        does and says whether it took it, which only a punch probe ever is
        """
        self._configuration = configuration
        self._create_protocol = create_protocol
        self._probe_router = probe_router
        self._bindings: list[_Binding] = []
        self._protocols: dict[bytes, object] = {}
        self._closed = False

    # --- lifecycle ---

    async def bind(self, sockets: list[socket.socket]) -> None:
        """Take ownership of the sockets and start reading them."""
        loop = asyncio.get_running_loop()
        taken = 0
        try:
            for sock in sockets:
                binding = _Binding(self, sock.family, _carries_ipv4(sock))
                await loop.create_datagram_endpoint(lambda b=binding: b, sock=sock)
                self._bindings.append(binding)
                taken += 1
        except Exception:
            for sock in sockets[taken:]:
                sock.close()
            self.close()
            raise

    @property
    def port(self) -> int:
        """The port every socket here is bound to, or 0 when there is none."""
        for binding in self._bindings:
            if binding.transport is not None:
                return binding.transport.get_extra_info("sockname")[1]
        return 0

    def close(self) -> None:
        """Close every session on this endpoint and give the sockets back."""
        self._closed = True
        for protocol in set(self._protocols.values()):
            try:
                protocol.close()
            except Exception as e:
                RNS.log(f"TrenchChat [ip]: could not close a session: {e}",
                        RNS.LOG_DEBUG)
        self._protocols.clear()
        for binding in self._bindings:
            binding.close()
        self._bindings.clear()

    # --- sending ---

    def _binding_for(self, host: str) -> _Binding | None:
        """The socket that can reach this address, preferring its own family."""
        try:
            version = ipaddress.ip_address(unmap_host(host)).version
        except ValueError:
            return None
        wanted = socket.AF_INET6 if version == 6 else socket.AF_INET
        fallback = None
        for binding in self._bindings:
            if binding.transport is None:
                continue
            if binding.family == wanted:
                return binding
            if version == 4 and binding.dual:
                fallback = binding
        return fallback

    def send_to(self, data: bytes, addr) -> bool:
        """Send one datagram to an ordinary address. False when none can."""
        host, port = addr[0], addr[1]
        binding = self._binding_for(host)
        if binding is None:
            return False
        target = socket_address(host, port, binding.family)
        if target is None:
            return False
        binding.sendto(data, target)
        return True

    # --- sessions ---

    async def dial(self, host: str, port: int, configuration: QuicConfiguration,
                   create_protocol):
        """Open one outbound connection from the socket this node listens on."""
        binding = self._binding_for(host)
        if binding is None or self._closed:
            raise OSError(f"no socket here can reach {host}")
        address = socket_address(host, port, binding.family)
        if address is None:
            raise OSError(f"{host} is not an address this node can dial")
        connection = QuicConnection(configuration=configuration)
        session = create_protocol(connection)
        self._adopt(session, connection.host_cid)
        session.connection_made(binding.transport)
        await session.dial(address)
        return session

    def forget(self, protocol) -> None:
        """Drop every connection id one session was reachable at."""
        for cid, held in list(self._protocols.items()):
            if held is protocol:
                del self._protocols[cid]

    def _adopt(self, protocol, *cids: bytes) -> None:
        """Route these connection ids here, and follow the ones QUIC issues.

        The three handlers are aioquic's own seam for exactly this, set the
        same way its server sets them; a connection that issues or retires an
        id has to be findable under it before the peer starts using it.
        """
        for cid in cids:
            self._protocols[cid] = protocol
        protocol._connection_id_issued_handler = (
            lambda cid, held=protocol: self._protocols.__setitem__(cid, held))
        protocol._connection_id_retired_handler = (
            lambda cid: self._protocols.pop(cid, None))
        protocol._connection_terminated_handler = (
            lambda held=protocol: self.forget(held))

    # --- receiving ---

    def receive(self, binding: _Binding, data: bytes, addr) -> None:
        """One datagram: a probe, a packet for a session here, or a new session."""
        if self._probe_router is not None:
            try:
                if self._probe_router(data, (unmap_host(addr[0]), addr[1])):
                    return
            except Exception as e:
                RNS.log(f"TrenchChat [ip]: probe router error: {e}", RNS.LOG_ERROR)
        try:
            header = pull_quic_header(
                Buffer(data=data),
                host_cid_length=self._configuration.connection_id_length)
        except ValueError:
            return
        protocol = self._protocols.get(header.destination_cid)
        if protocol is None:
            protocol = self._open(binding, header, data, addr)
            if protocol is None:
                return
        protocol.datagram_received(data, addr)

    def _open(self, binding: _Binding, header, data: bytes, addr):
        """Take a connection nobody here knows, if it is one worth taking."""
        if self._closed:
            return None
        if (header.version is not None
                and header.version not in self._configuration.supported_versions):
            binding.sendto(encode_quic_version_negotiation(
                source_cid=header.destination_cid,
                destination_cid=header.source_cid,
                supported_versions=self._configuration.supported_versions), addr)
            return None
        if (header.packet_type != QuicPacketType.INITIAL
                or len(data) < SMALLEST_MAX_DATAGRAM_SIZE):
            return None
        connection = QuicConnection(
            configuration=self._configuration,
            original_destination_connection_id=header.destination_cid)
        protocol = self._create_protocol(connection)
        self._adopt(protocol, header.destination_cid, connection.host_cid)
        protocol.connection_made(binding.transport)
        return protocol
