"""
A STUN server small enough to read: it answers one question and holds nothing.

The public address echo has to be tested against something, and pointing a test
or the namespace harness at a real public server would make the run depend on
the internet and on somebody else's uptime. This answers a binding request with
XOR-MAPPED-ADDRESS naming the source it read the request from, which is the
whole of what the client uses, and drops everything else.

Run it in the root namespace for the harness, or in-process for a test:

    python stun_responder.py 198.51.100.254 3478
"""

import socket
import struct
import sys
import threading
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from trenchchat.network.ip import stun  # noqa: E402

# A binding request is twenty bytes; one carrying attributes this does not read
# is still far under this, and anything larger is not a request.
MAX_DATAGRAM_BYTES = 1500

# How often the serving thread looks at whether it has been stopped.
POLL_SECS = 0.2

MAPPED_PREFIX = "::ffff:"


def answer(data: bytes, source: tuple[str, int]) -> bytes | None:
    """The response to one datagram, or None when it is not a binding request."""
    transaction_id = stun.transaction_of(data)
    if transaction_id is None:
        return None
    if struct.unpack_from("!H", data, 0)[0] != stun.BINDING_REQUEST:
        return None
    host = source[0]
    if host.startswith(MAPPED_PREFIX):
        host = host[len(MAPPED_PREFIX):]
    return stun.build_response(transaction_id, host, source[1])


class StunResponder:
    """One socket answering binding requests, on a thread of its own."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        self._socket = socket.socket(family, socket.SOCK_DGRAM)
        self._socket.bind((host, port))
        self._socket.settimeout(POLL_SECS)
        self._stop = threading.Event()
        self._requests = 0
        self._datagrams = 0
        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name="stun-responder")

    @property
    def address(self) -> tuple[str, int]:
        """Where this responder listens, as a client is told about it."""
        name = self._socket.getsockname()
        return (name[0], name[1])

    @property
    def server(self) -> str:
        """This responder as a config entry, bracketed if it is IPv6."""
        host, port = self.address
        return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"

    @property
    def requests(self) -> int:
        """How many binding requests have been answered."""
        return self._requests

    @property
    def datagrams(self) -> int:
        """How many datagrams of any kind arrived.

        What proves a node asked nothing: a client that is switched off must
        send nothing at all here, not merely nothing well formed.
        """
        return self._datagrams

    def start(self) -> "StunResponder":
        """Start serving, and hand back self so a test can open one in a line."""
        self._thread.start()
        return self

    def wait(self) -> None:
        """Block until the serving thread ends, which is how a script runs."""
        while self._thread.is_alive():
            self._thread.join(1.0)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                data, source = self._socket.recvfrom(MAX_DATAGRAM_BYTES)
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                return
            self._datagrams += 1
            response = answer(data, source)
            if response is None:
                continue
            self._requests += 1
            try:
                self._socket.sendto(response, source)
            except OSError:
                return

    def stop(self) -> None:
        """Stop serving and give the socket back."""
        self._stop.set()
        self._thread.join(timeout=2.0)
        self._socket.close()


def main(argv: list[str]) -> int:
    """Serve until killed, which is how the namespace harness runs it."""
    host = argv[1] if len(argv) > 1 else "0.0.0.0"
    port = int(argv[2]) if len(argv) > 2 else stun.DEFAULT_PORT
    responder = StunResponder(host, port).start()
    print(f"stun_responder: listening on {responder.server}", flush=True)
    try:
        responder.wait()
    except KeyboardInterrupt:
        responder.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
