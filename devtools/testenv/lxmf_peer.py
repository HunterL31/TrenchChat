"""
A bare LXMF client, for proving TrenchChat's direct messages interoperate.

Deliberately imports nothing from TrenchChat. It is RNS and LXMF and nothing
else -- the same footing Sideband or NomadNet stand on -- so that a scenario
using it proves interoperability rather than proving TrenchChat agrees with
itself. Every message it sends is a plain LXMessage: text in the content, no
fields at all.

Three modes, each printing one JSON object on stdout:

    identity --data-dir D
        Create or load an identity and print its hash. Touches no network, so
        a scenario can learn the hash before deciding to trust it.

    send --data-dir D --to IDENTITY_HASH --content TEXT
        Join the hub, wait for a path to the recipient, send, and report.

    listen --data-dir D --seconds N
        Join the hub and report every message received in that window.
"""

import argparse
import json
import sys
import time
from pathlib import Path

import LXMF
import RNS

_CONFIG = """\
[reticulum]
enable_transport = False
share_instance = No
instance_name = trenchchat_lxmf_peer

[logging]
loglevel = 3

[interfaces]
  [[HubLink]]
    type = TCPClientInterface
    interface_enabled = true
    target_host = 127.0.0.1
    target_port = {hub_port}
"""

# A path has to reach us through the hub before anything can be addressed.
PATH_TIMEOUT_SECS = 60.0
ANNOUNCE_SETTLE_SECS = 2.0


def _identity(data_dir: Path) -> RNS.Identity:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "identity"
    if path.exists():
        return RNS.Identity.from_file(str(path))
    identity = RNS.Identity()
    identity.to_file(str(path))
    return identity


def _start(data_dir: Path, hub_port: int):
    rns_dir = data_dir / "reticulum"
    rns_dir.mkdir(parents=True, exist_ok=True)
    (rns_dir / "config").write_text(_CONFIG.format(hub_port=hub_port))
    RNS.Reticulum(configdir=str(rns_dir), loglevel=RNS.LOG_NOTICE)

    identity = _identity(data_dir)
    router = LXMF.LXMRouter(storagepath=str(data_dir / "messagestore"),
                            identity=identity)
    delivery = router.register_delivery_identity(identity, display_name="Bare LXMF")
    router.announce(delivery.hash)
    time.sleep(ANNOUNCE_SETTLE_SECS)
    return identity, router, delivery


def _await_path(destination_hash: bytes) -> bool:
    if not RNS.Transport.has_path(destination_hash):
        RNS.Transport.request_path(destination_hash)
    deadline = time.time() + PATH_TIMEOUT_SECS
    while time.time() < deadline:
        if RNS.Transport.has_path(destination_hash) and \
                RNS.Identity.recall(destination_hash) is not None:
            return True
        time.sleep(0.5)
    return False


def do_identity(args) -> dict:
    return {"hash": _identity(Path(args.data_dir)).hash.hex()}


def do_send(args) -> dict:
    identity, router, delivery = _start(Path(args.data_dir), args.hub_port)
    target = RNS.Destination.hash(bytes.fromhex(args.to), "lxmf", "delivery")
    if not _await_path(target):
        return {"sent": False, "error": "no path to the recipient"}

    destination = RNS.Destination(
        RNS.Identity.recall(target), RNS.Destination.OUT,
        RNS.Destination.SINGLE, "lxmf", "delivery",
    )
    # A plain message: content and nothing else, exactly as any LXMF client
    # that has never heard of TrenchChat would send it.
    message = LXMF.LXMessage(destination, delivery, args.content,
                             desired_method=LXMF.LXMessage.DIRECT)
    router.handle_outbound(message)

    deadline = time.time() + PATH_TIMEOUT_SECS
    while time.time() < deadline:
        if message.state == LXMF.LXMessage.DELIVERED:
            return {"sent": True, "state": "delivered",
                    "source": identity.hash.hex()}
        if message.state == LXMF.LXMessage.FAILED:
            return {"sent": False, "error": "delivery failed"}
        time.sleep(0.5)
    return {"sent": message.state == LXMF.LXMessage.SENT, "state": "sent",
            "source": identity.hash.hex()}


def do_listen(args) -> dict:
    _identity, router, _delivery = _start(Path(args.data_dir), args.hub_port)
    received = []

    def on_delivery(message):
        content = message.content
        if isinstance(content, bytes):
            content = content.decode(errors="replace")
        source = RNS.Identity.recall(message.source_hash)
        received.append({
            "content": content,
            "source": source.hash.hex() if source else None,
            "fields": sorted(int(k) for k in (message.fields or {})),
        })

    router.register_delivery_callback(on_delivery)
    deadline = time.time() + args.seconds
    while time.time() < deadline:
        time.sleep(0.5)
    return {"received": received}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hub-port", type=int, default=41001)
    parser.add_argument("--data-dir", required=True)
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("identity")
    send = sub.add_parser("send")
    send.add_argument("--to", required=True)
    send.add_argument("--content", required=True)
    listen = sub.add_parser("listen")
    listen.add_argument("--seconds", type=float, default=30.0)
    args = parser.parse_args()

    result = {"identity": do_identity, "send": do_send,
              "listen": do_listen}[args.mode](args)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
