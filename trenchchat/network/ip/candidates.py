"""
Where a peer should try to reach this node, gathered with the standard library.

A candidate is a literal address and a port, tagged with how this node came by
it. Three kinds, in the order a truncated list keeps them:

    mapped    a router gave this node an inbound port (UPnP-IGD or NAT-PMP)
    observed  a peer saw this node's probes arrive from here
    lan       an address of a local interface, with the port being punched

The lan kind is why this design needs no overlay integration: a Tailscale or
WireGuard interface has an ordinary address on this machine, so it is gathered
like any other and the punch over it always succeeds.

Both families are gathered and both are offered. An IPv6 address usually needs
no punch at all, only a firewall that lets the reply back in, which is why a
pair with IPv6 on both ends is the case that works with nothing observed and
nobody helping. A host holds several IPv6 addresses at once, so two are
offered: the one the kernel would send from, which is the temporary address
wherever privacy extensions are on, and one other global address. Link-local
is never offered, because it needs a zone this node cannot name for the peer;
unique-local is, because two peers on one site can reach each other by it and
nobody else can.

Nothing here asks the network anything. There is no service to query for "my
address", because one would be a center; what this node knows about its own
public address, it learned from a peer that already had a reason to talk to it.
"""

import ipaddress
import socket
import struct
import sys

from trenchchat.core.protocol import (
    MAX_UPGRADE_CANDIDATES, UPGRADE_KIND_LAN, UPGRADE_KIND_MAPPED,
    UPGRADE_KIND_OBSERVED,
)

# Addresses the kernel would route towards, asked one at a time so every
# interface with a route answers for itself: the default route, the Tailscale
# range, and each private range in turn. A UDP socket that connects sends
# nothing, so this costs no packet and reaches no host.
ROUTE_PROBES = (
    (socket.AF_INET, "8.8.8.8"),
    (socket.AF_INET, "100.100.100.100"),
    (socket.AF_INET, "10.0.0.1"),
    (socket.AF_INET, "172.16.0.1"),
    (socket.AF_INET, "192.168.0.1"),
    (socket.AF_INET6, "2001:4860:4860::8888"),
    (socket.AF_INET6, "fd00::1"),
)

PROBE_PORT = 9

# IPv6 addresses offered per attempt. A host with privacy extensions on holds a
# permanent address and a rotating temporary one per prefix, and a candidate
# list has eight places in all.
MAX_IPV6_ADDRESSES = 2

# Linux writes every IPv6 address here, which is the only place the temporary
# and the permanent one are both visible. The flags are the kernel's:
# an address that is still proving itself, or already deprecated, is not one a
# peer should be told to dial.
_IF_INET6 = "/proc/net/if_inet6"
_IFA_F_DADFAILED = 0x08
_IFA_F_DEPRECATED = 0x20
_IFA_F_TENTATIVE = 0x40
_UNUSABLE_FLAGS = _IFA_F_DADFAILED | _IFA_F_DEPRECATED | _IFA_F_TENTATIVE


def is_reachable_address(host: str) -> bool:
    """Whether a peer elsewhere could ever reach this node at this address.

    Loopback is this machine talking to itself, link-local needs a zone this
    node cannot name for the peer, and the rest are not host addresses at all.
    IPv6 is judged on its own terms: a v4-mapped address is this node's own
    socket talking about itself rather than an address anybody can dial, and
    site-local was deprecated in favour of unique-local, which is kept.
    """
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if (address.is_loopback or address.is_link_local or address.is_multicast
            or address.is_unspecified or address.is_reserved):
        return False
    if address.version == 6:
        return not (address.ipv4_mapped is not None or address.is_site_local)
    return True


def family_of(host: str) -> int | None:
    """Which IP version an address is, or None when it is not one."""
    try:
        return ipaddress.ip_address(host).version
    except ValueError:
        return None


def newest_per_family(addresses) -> list[tuple[str, int]]:
    """The first address of each family, keeping the order given.

    Every peer that observes this node is remembered, so what it has been told
    about itself holds more than one address of a family; a candidate list has
    room for one of each.
    """
    kept: list[tuple[str, int]] = []
    families: set[int] = set()
    for host, port in addresses:
        version = family_of(host)
        if version is None or version in families:
            continue
        families.add(version)
        kept.append((host, port))
    return kept


def _route_address(family: int, target: str) -> str | None:
    """The local address the kernel would use towards a target, or None."""
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as probe:
            probe.connect((target, PROBE_PORT))
            return probe.getsockname()[0]
    except OSError:
        return None


def _hostname_addresses() -> list[str]:
    """Whatever this machine's own name resolves to, which is often several."""
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None,
                                   type=socket.SOCK_DGRAM)
    except OSError:
        return []
    return [info[4][0] for info in infos]


# Linux's ioctl for "the IPv4 address of this interface", and the buffer it
# writes back into. Nothing else answers this from the standard library.
_SIOCGIFADDR = 0x8915
_IFNAME_BYTES = 16
_IFREQ_BYTES = 256


def _interface_addresses6() -> list[str]:
    """Every usable IPv6 address the kernel holds, where it will say.

    A host has more than one: the permanent address of each prefix and the
    temporary one privacy extensions rotate in front of it. A route probe
    answers with whichever the kernel would send from and never names the
    other, and either may be the one a peer can reach.
    """
    try:
        with open(_IF_INET6, "r") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return []
    found: list[str] = []
    for line in lines:
        parts = line.split()
        if len(parts) < 5 or len(parts[0]) != 32:
            continue
        try:
            flags = int(parts[4], 16)
        except ValueError:
            continue
        if flags & _UNUSABLE_FLAGS:
            continue
        packed = bytes.fromhex(parts[0])
        address = str(ipaddress.IPv6Address(packed))
        if address not in found:
            found.append(address)
    return found


def _interface_addresses() -> list[str]:
    """Every interface's IPv4 address, where the platform will say.

    The route probes miss an interface with no route towards any of them, and a
    node on a segment like that has an address peers can reach and no way for a
    connect() to point at it. Linux answers this through an ioctl; every other
    platform gets the probes alone, which is what they had.
    """
    if not sys.platform.startswith("linux"):
        return []
    try:
        import fcntl
    except ImportError:
        return []
    found: list[str] = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for _index, name in socket.if_nameindex():
            request = struct.pack(f"{_IFREQ_BYTES}s",
                                  name[:_IFNAME_BYTES - 1].encode())
            try:
                answer = fcntl.ioctl(sock.fileno(), _SIOCGIFADDR, request)
            except OSError:
                continue
            found.append(socket.inet_ntoa(answer[20:24]))
    return found


def local_addresses() -> list[str]:
    """Every local interface address a peer could reach this node at.

    Three sources, because no one of them is enough: the routing table answers
    for every interface a probe target routes through, the interface list
    answers for the rest where the platform allows it, and this machine's own
    name answers for whatever a resolver knows. An address none of the three
    finds is not gathered, and a peer can still learn it as an observed
    address.
    """
    found: list[str] = []
    for family, target in ROUTE_PROBES:
        address = _route_address(family, target)
        if address is not None and address not in found:
            found.append(address)
    for source in (_interface_addresses(), _interface_addresses6(),
                   _hostname_addresses()):
        for address in source:
            if address not in found:
                found.append(address)
    return _bounded(address for address in found
                    if is_reachable_address(address))


def _bounded(addresses) -> list[str]:
    """Every IPv4 address found, and at most two IPv6 ones, in order."""
    kept: list[str] = []
    sixes = 0
    for address in addresses:
        if family_of(address) == 6:
            if sixes >= MAX_IPV6_ADDRESSES:
                continue
            sixes += 1
        kept.append(address)
    return kept


def gather(port: int, *, mapped: tuple[str, int] | None = None,
           observed=(), limit: int = MAX_UPGRADE_CANDIDATES
           ) -> list[tuple[str, int, str]]:
    """This node's candidates for one attempt, at most *limit* of them.

    *port* is the port being punched, as bound rather than as configured: a
    kernel-assigned port is the only one a test or a second instance on one
    machine ever has. *mapped* and *observed* carry their own ports, because
    both name a port some other party chose.

    A truncated list keeps the mapped and observed entries: those are the ones
    that cross a NAT, and a peer that cannot use them has usually already
    failed on the lan entries too.

    Both families are offered together and probed together, since which of
    them a pair has in common is not a thing either side knows in advance.
    """
    gathered: list[tuple[str, int, str]] = []
    seen: set[tuple[str, int]] = set()

    def _add(host: str, host_port: int, kind: str) -> None:
        if not is_reachable_address(host) or not 1 <= host_port <= 65535:
            return
        if (host, host_port) in seen or len(gathered) >= limit:
            return
        seen.add((host, host_port))
        gathered.append((host, host_port, kind))

    if mapped is not None:
        _add(mapped[0], mapped[1], UPGRADE_KIND_MAPPED)
    for entry in observed:
        _add(entry[0], entry[1], UPGRADE_KIND_OBSERVED)
    for address in local_addresses():
        _add(address, port, UPGRADE_KIND_LAN)
    return gathered
