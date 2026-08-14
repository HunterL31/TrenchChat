"""
Standalone Reticulum transport hub for the dev test environment.

Testers currently link directly to each other over one point-to-point
TCPClientInterface/TCPServerInterface pair. That works for exactly two
peers, but it also means neither side can be taken offline independently
without tearing down the only interface both peers share.

This hub is a third, headless RNS.Reticulum instance with no TrenchChat
identity, storage, or managers. Every tester connects to it as a
TCPClientInterface -- the only RNS interface type that can be detached
and reconnected at runtime (see Backend.go_offline/go_online in
backend_core.py). Routing through a shared hub, instead of tester-to-
tester direct links, is what makes per-peer online/offline toggling
possible in later phases.
"""

import sys
import time
from pathlib import Path

import RNS

_HUB_CONFIG_TEMPLATE = """\
[reticulum]
enable_transport = True
share_instance = No
instance_name = {instance_name}

[logging]
loglevel = 3

[interfaces]
  [[Hub]]
    type = TCPServerInterface
    interface_enabled = true
    listen_ip = 127.0.0.1
    listen_port = {listen_port}
"""


def _write_hub_config(rns_dir: Path, listen_port: int, instance_name: str) -> None:
    """Write a minimal transport-only Reticulum config for the hub."""
    rns_dir.mkdir(parents=True, exist_ok=True)
    config_text = _HUB_CONFIG_TEMPLATE.format(
        instance_name=instance_name, listen_port=listen_port,
    )
    (rns_dir / "config").write_text(config_text)


def run(data_dir: Path, listen_port: int, instance_name: str) -> None:
    """Start the hub's Reticulum instance and block forever."""
    rns_dir = data_dir / "reticulum"
    _write_hub_config(rns_dir, listen_port, instance_name)
    RNS.Reticulum(configdir=str(rns_dir), loglevel=RNS.LOG_NOTICE)
    RNS.log(f"TrenchChat [hub]: listening on 127.0.0.1:{listen_port}", RNS.LOG_NOTICE)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    _, data_dir, listen_port, instance_name = sys.argv
    run(Path(data_dir), int(listen_port), instance_name)
