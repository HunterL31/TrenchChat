"""Phase 0 spike: the UDP hole punch, as one process per peer.

Each peer binds one UDP socket, sends probe datagrams carrying its own nonce to every
candidate it was given, and answers a peer probe with an acknowledgement that echoes the
nonce back. A candidate pair counts as punched when it is seen both ways: a probe has
arrived from that address, and an acknowledgement carrying this peer's own nonce has
arrived from the same address, which proves the peer received something this node sent.

Run it by hand on loopback:

    python punch.py --bind 127.0.0.1:5000 --nonce aa --peer-nonce bb \\
                    --candidate 127.0.0.1:5001 --seconds 5
    python punch.py --bind 127.0.0.1:5001 --nonce bb --peer-nonce aa \\
                    --candidate 127.0.0.1:5000 --seconds 5

netns_nat.sh drives it between two namespaces behind separate NATs.
"""

import argparse
import json
import socket
import sys
import time

PROBE = b"PUNCH"
ACK = b"PACK"
PROBE_INTERVAL_SECS = 0.2
RECV_BYTES = 512
SEPARATOR = b" "


def parse_endpoint(text: str) -> tuple[str, int]:
    """Split a `host:port` argument."""
    host, _, port = text.rpartition(":")
    return host, int(port)


def build_probe(nonce: bytes, sequence: int) -> bytes:
    """A probe names the sender's nonce so the receiver can tell it from stray traffic."""
    return SEPARATOR.join([PROBE, nonce.hex().encode(), str(sequence).encode()])


def build_ack(own_nonce: bytes, peer_nonce: bytes) -> bytes:
    """An acknowledgement echoes the peer's nonce, which is the proof the probe arrived."""
    return SEPARATOR.join([ACK, own_nonce.hex().encode(), peer_nonce.hex().encode()])


def parse_datagram(data: bytes) -> tuple[bytes, bytes, bytes] | None:
    """Split a probe or acknowledgement into kind, sender nonce and echoed nonce."""
    parts = data.split(SEPARATOR)
    if len(parts) < 3 or parts[0] not in (PROBE, ACK):
        return None
    try:
        sender = bytes.fromhex(parts[1].decode())
        echoed = bytes.fromhex(parts[2].decode()) if parts[0] == ACK else b""
    except ValueError:
        return None
    return parts[0], sender, echoed


def punch(bind: tuple[str, int], nonce: bytes, peer_nonce: bytes,
          candidates: list[tuple[str, int]], seconds: float) -> dict:
    """Probe every candidate until one is seen both ways or the deadline passes."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(bind)
    sock.settimeout(PROBE_INTERVAL_SECS)
    started = time.monotonic()
    deadline = started + seconds
    probes_from: set[tuple[str, int]] = set()
    acks_from: set[tuple[str, int]] = set()
    sequence = 0
    result: dict = {"bound": f"{bind[0]}:{bind[1]}",
                    "candidates": [f"{host}:{port}" for host, port in candidates]}
    try:
        while time.monotonic() < deadline:
            for candidate in candidates:
                try:
                    sock.sendto(build_probe(nonce, sequence), candidate)
                except OSError:
                    pass
            sequence += 1
            window = time.monotonic() + PROBE_INTERVAL_SECS
            while time.monotonic() < window:
                try:
                    data, source = sock.recvfrom(RECV_BYTES)
                except socket.timeout:
                    break
                except OSError:
                    continue
                parsed = parse_datagram(data)
                if parsed is None or parsed[1] != peer_nonce:
                    continue
                kind, _, echoed = parsed
                if kind == PROBE:
                    probes_from.add(source)
                    sock.sendto(build_ack(nonce, peer_nonce), source)
                elif echoed == nonce:
                    acks_from.add(source)
                both_ways = probes_from & acks_from
                if both_ways:
                    remote = sorted(both_ways)[0]
                    result["result"] = "punched"
                    result["remote"] = f"{remote[0]}:{remote[1]}"
                    result["seconds"] = round(time.monotonic() - started, 3)
                    result["probes_sent"] = sequence * len(candidates)
                    return result
    finally:
        sock.close()
    result["result"] = "failed"
    result["seconds"] = round(time.monotonic() - started, 3)
    result["probes_sent"] = sequence * len(candidates)
    result["probes_received_from"] = [f"{host}:{port}" for host, port in sorted(probes_from)]
    result["acks_received_from"] = [f"{host}:{port}" for host, port in sorted(acks_from)]
    return result


def main(argv: list[str] | None = None) -> int:
    """Parse the command line and run one side of a hole punch."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bind", required=True, help="local host:port to punch from")
    parser.add_argument("--nonce", required=True, help="this peer's nonce, in hex")
    parser.add_argument("--peer-nonce", required=True, help="the other peer's nonce, in hex")
    parser.add_argument("--candidate", action="append", default=[], required=True,
                        help="a host:port to probe, repeatable")
    parser.add_argument("--seconds", type=float, default=8.0)
    parser.add_argument("--out", help="write the result document here as well as to stdout")
    args = parser.parse_args(argv)

    result = punch(
        parse_endpoint(args.bind),
        bytes.fromhex(args.nonce),
        bytes.fromhex(args.peer_nonce),
        [parse_endpoint(value) for value in args.candidate],
        args.seconds,
    )
    if args.out:
        with open(args.out, "w") as handle:
            json.dump(result, handle, indent=2)
    print(json.dumps(result), flush=True)
    return 0 if result["result"] == "punched" else 1


if __name__ == "__main__":
    sys.exit(main())
