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

Probes go out of the socket this node listens on and arrive back on it, because
that is the socket its candidates named and the only one a router forwards to.
The endpoint owns that socket and hands every datagram carrying an attempt's
nonce to its ProbeChannel; punch() drives the retransmission from a worker
thread, since everything here blocks and none of it belongs on an RNS callback
thread or on the transport's loop.

Phase 0 found the ordering that matters: a probe reaching a NAT before that NAT
has made its own outbound mapping can take the very tuple the mapping wanted,
after which neither side's candidate is right. So the answering side probes
first, from the moment it answers, and the offering side waits for that answer
and for the punch time it named.
"""

import threading
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

# Addresses one attempt will answer probes from. An attempt aims at eight
# candidates and learns a handful more from the probes that arrive; anything
# past this is somebody spraying nonces they should not have.
MAX_PROBE_SOURCES = 32


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


def nonce_of(data: bytes) -> bytes | None:
    """The nonce a probe datagram carries, or None when it is not one.

    What the endpoint asks of every datagram before QUIC sees it: no valid QUIC
    packet is this short, and the nonce still has to name a live attempt.
    """
    if len(data) != DATAGRAM_BYTES or data[:MAGIC_BYTES] not in (PROBE_MAGIC,
                                                                ACK_MAGIC):
        return None
    return data[MAGIC_BYTES:]


class ProbeChannel:
    """One attempt's probes on the socket this node listens on.

    The endpoint delivers every datagram carrying this attempt's nonce here, on
    its loop, and the answer goes back on the spot: an acknowledgement, so the
    far side knows its probe arrived, and a probe of this node's own at the
    address it came from, which is the address a NAT chose and no candidate
    list could have named. punch() keeps probing from a thread of its own,
    because one datagram each way is not a path until both sides have seen one.
    """

    def __init__(self, nonce: bytes, send):
        """
        nonce: the sixteen bytes this attempt's datagrams carry
        send(data, (host, port)) -> bool: the endpoint's own sender
        """
        self._nonce = nonce
        self._send = send
        self._lock = threading.Lock()
        self._arrived = threading.Event()
        self._probes_from: set = set()
        self._acks_from: set = set()
        self._fresh: list = []
        self._closed = False

    def send_probe(self, addr) -> bool:
        """Probe one address. False when nothing here could send it."""
        with self._lock:
            if self._closed:
                return False
        return bool(self._send(probe_datagram(self._nonce), addr))

    def deliver(self, data: bytes, addr) -> bool:
        """Take one datagram and answer it. Runs on the transport's loop."""
        kind = read_datagram(data, self._nonce)
        if kind is None:
            return False
        with self._lock:
            if self._closed:
                return True
            if kind == KIND_ACK:
                self._acks_from.add(addr)
                self._arrived.set()
                return True
            fresh = addr not in self._probes_from
            if fresh and len(self._probes_from) >= MAX_PROBE_SOURCES:
                return True
            self._probes_from.add(addr)
            if fresh:
                self._fresh.append(addr)
            self._arrived.set()
        self._send(ack_datagram(self._nonce), addr)
        if fresh:
            self._send(probe_datagram(self._nonce), addr)
        return True

    def arm(self) -> None:
        """Forget what has arrived, so the next wait is about the next round."""
        self._arrived.clear()

    def wait(self, timeout: float) -> bool:
        """Wait for anything to arrive, or for the round to be over."""
        return self._arrived.wait(timeout)

    def take_fresh(self) -> list:
        """The addresses probes newly arrived from, which are new targets."""
        with self._lock:
            fresh, self._fresh = self._fresh, []
        return fresh

    def matched(self) -> tuple | None:
        """The first address seen both ways, which is the punched pair."""
        with self._lock:
            both_ways = self._probes_from & self._acks_from
        return sorted(both_ways)[0] if both_ways else None

    def seen(self) -> tuple[list, list]:
        """Every address a probe and an acknowledgement arrived from."""
        with self._lock:
            return sorted(self._probes_from), sorted(self._acks_from)

    def close(self) -> None:
        """Stop answering: the attempt this channel belonged to is over."""
        with self._lock:
            self._closed = True
        self._arrived.set()


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


def punch(channel: ProbeChannel, peer_candidates, *,
          seconds: float = PUNCH_TIMEOUT_SECS,
          interval: float = PROBE_INTERVAL_SECS,
          start_at: float | None = None, on_probe=None) -> PunchResult:
    """Probe every candidate until one answers both ways or the time is up.

    *peer_candidates* are (host, port) pairs, whatever kind they were offered
    as, and an address a probe arrives from joins them: a NAT names an address
    the node behind it cannot know, so the pair that works is often one neither
    side could name. *start_at* holds the first probe until the time the peer
    was told, so the side that answered has already opened its own mapping.
    *on_probe* is called with each address a probe arrived from, which is what
    a peer is later told about itself.
    """
    started = time.monotonic()
    if start_at is not None:
        wait = start_at - time.time()
        if 0 < wait <= MAX_UPGRADE_PUNCH_AHEAD_SECS:
            time.sleep(wait)
    deadline = time.monotonic() + seconds
    result = PunchResult()
    targets = [(host, port) for host, port, *_rest in peer_candidates]

    while time.monotonic() < deadline:
        channel.arm()
        for target in targets:
            if channel.send_probe(target):
                result.probes_sent += 1
        channel.wait(min(interval, max(deadline - time.monotonic(), 0.0)))
        for source in channel.take_fresh():
            if source not in targets:
                targets.append(source)
            if on_probe is not None:
                on_probe(source)
        remote = channel.matched()
        if remote is not None:
            result.remote = remote
            break

    result.probes_from, result.acks_from = channel.seen()
    result.seconds = round(time.monotonic() - started, 3)
    if result.punched:
        RNS.log(f"TrenchChat [ip]: punched a path to "
                f"{result.remote[0]}:{result.remote[1]} in "
                f"{result.seconds:.1f}s", RNS.LOG_NOTICE)
    return result
