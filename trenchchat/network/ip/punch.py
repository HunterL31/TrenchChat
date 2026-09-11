"""
The UDP hole punch: probes that prove knowledge of one nonce.

Two peers that have traded an offer and an answer each hold the same sixteen
random bytes, which travelled encrypted inside an LXMF message and so are known
to nobody else. A probe carries a four-byte magic and that nonce and nothing
else: knowing it is the whole of what a probe proves, and what authenticates
the pair is the QUIC handshake and the HELLO that follow on the punched path.

A pair counts as punched when it is seen both ways from one address: a probe
arrived from it, and an acknowledgement echoing this node's nonce arrived from
it, which is the peer saying it received something this node sent.

The socket is the caller's, bound before the punch and handed to the session
after it, because the QUIC session has to run on the socket whose mapping the
punch opened. Everything here blocks, so it belongs on a worker thread and
never on an RNS callback thread.

Phase 0 found the ordering that matters: a probe reaching a NAT before that NAT
has made its own outbound mapping can take the very tuple the mapping wanted,
after which neither side's candidate is right. So the answering side probes
first, from the moment it answers, and the offering side waits for that answer
and for the punch time it named.
"""

import socket
import time
from dataclasses import dataclass, field

import RNS

from trenchchat.core.protocol import (
    MAX_UPGRADE_PUNCH_AHEAD_SECS, UPGRADE_NONCE_BYTES,
)

PROBE_MAGIC = b"TCu\x01"
ACK_MAGIC = b"TCa\x01"
MAGIC_BYTES = len(PROBE_MAGIC)
DATAGRAM_BYTES = MAGIC_BYTES + UPGRADE_NONCE_BYTES

KIND_PROBE = "probe"
KIND_ACK = "ack"

# How often every candidate is probed, and for how long. A few seconds is the
# whole budget: a pair that has not punched by then is on the mesh, and the
# next attempt is a backoff away.
PROBE_INTERVAL_SECS = 0.2
PUNCH_TIMEOUT_SECS = 5.0

# Nothing legitimate is longer than a probe, so the read is the exact size.
RECV_BYTES = DATAGRAM_BYTES


def probe_datagram(nonce: bytes) -> bytes:
    """A probe: the magic and the attempt's nonce, twenty bytes in all."""
    return PROBE_MAGIC + nonce


def ack_datagram(nonce: bytes) -> bytes:
    """An acknowledgement, which is a probe's magic swapped for the answer's."""
    return ACK_MAGIC + nonce


def read_datagram(data: bytes, nonce: bytes) -> str | None:
    """Which kind of probe datagram this is, or None for anything else.

    Stray traffic and a probe for another attempt both land here, and both are
    nothing: only the nonce for this attempt is read as either kind.
    """
    if len(data) != DATAGRAM_BYTES or data[MAGIC_BYTES:] != nonce:
        return None
    magic = data[:MAGIC_BYTES]
    if magic == PROBE_MAGIC:
        return KIND_PROBE
    if magic == ACK_MAGIC:
        return KIND_ACK
    return None


def bind_socket(host: str = "0.0.0.0", port: int = 0) -> socket.socket:
    """One UDP socket for one attempt, bound where the caller asked.

    Its own socket every time: the session runs on it afterwards, and a socket
    another transport owns cannot be handed over.
    """
    info = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)[0]
    family, _type, _proto, _canonical, address = info
    sock = socket.socket(family, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(address)
    return sock


@dataclass
class PunchResult:
    """What one attempt saw, whether or not it found a path."""

    remote: tuple[str, int] | None = None
    probes_from: list = field(default_factory=list)
    acks_from: list = field(default_factory=list)
    probes_sent: int = 0
    seconds: float = 0.0

    @property
    def punched(self) -> bool:
        """Whether a candidate pair was seen both ways."""
        return self.remote is not None


def punch(sock: socket.socket, peer_candidates, nonce: bytes, *,
          seconds: float = PUNCH_TIMEOUT_SECS,
          interval: float = PROBE_INTERVAL_SECS,
          start_at: float | None = None, on_probe=None) -> PunchResult:
    """Probe every candidate until one answers both ways or the time is up.

    *peer_candidates* are (host, port) pairs, whatever kind they were offered
    as. *start_at* holds the first probe until the time the peer was told, so
    the side that answered has already opened its own mapping. *on_probe* is
    called with each address a probe arrived from, which is what a peer is
    later told about itself.
    """
    started = time.monotonic()
    if start_at is not None:
        wait = start_at - time.time()
        if 0 < wait <= MAX_UPGRADE_PUNCH_AHEAD_SECS:
            time.sleep(wait)
    deadline = time.monotonic() + seconds
    probe = probe_datagram(nonce)
    result = PunchResult()
    probes_from: set = set()
    acks_from: set = set()
    targets = [(host, port) for host, port, *_rest in peer_candidates]

    sock.settimeout(interval)
    try:
        while time.monotonic() < deadline:
            for target in targets:
                try:
                    sock.sendto(probe, target)
                    result.probes_sent += 1
                except OSError:
                    pass
            window = time.monotonic() + interval
            while time.monotonic() < window:
                try:
                    data, source = sock.recvfrom(RECV_BYTES)
                except (socket.timeout, TimeoutError):
                    break
                except OSError:
                    continue
                kind = read_datagram(data, nonce)
                if kind is None:
                    continue
                if kind == KIND_PROBE:
                    if source not in probes_from:
                        probes_from.add(source)
                        if on_probe is not None:
                            on_probe(source)
                    try:
                        sock.sendto(ack_datagram(nonce), source)
                    except OSError:
                        pass
                else:
                    acks_from.add(source)
                both_ways = probes_from & acks_from
                if both_ways:
                    result.remote = sorted(both_ways)[0]
                    break
            if result.remote is not None:
                break
    finally:
        sock.settimeout(None)
    result.probes_from = sorted(probes_from)
    result.acks_from = sorted(acks_from)
    result.seconds = round(time.monotonic() - started, 3)
    if result.punched:
        RNS.log(f"TrenchChat [ip]: punched a path to "
                f"{result.remote[0]}:{result.remote[1]} in "
                f"{result.seconds:.1f}s", RNS.LOG_NOTICE)
    return result


def answer_probe(data: bytes, source, send, nonce: bytes) -> bool:
    """Answer one probe that arrived somewhere other than an attempt's socket.

    The listening socket sees probes aimed at a mapped or observed candidate,
    which name the port a router forwards rather than the port being punched.
    Answering with an acknowledgement and a probe of this node's own makes that
    address a candidate pair like any other. Returns whether the datagram was a
    probe at all; anything else belongs to whoever was going to read it next.
    """
    if read_datagram(data, nonce) != KIND_PROBE:
        return False
    send(ack_datagram(nonce), source)
    send(probe_datagram(nonce), source)
    return True
