"""
Headless TrenchChat backend, for running one "tester" identity as its own
OS process during local multi-user testing.

Mirrors the object wiring in main.py (Identity, Storage, Router, core
managers) minus the Qt GUI. Each tester gets a fully isolated data
directory and its own standalone Reticulum instance (share_instance=No,
so it never silently attaches to another Reticulum instance already
running on the machine) connected to the other tester via a single
point-to-point TCP interface -- not AutoInterface, which relies on UDP
multicast that has been observed to fail on this machine/network.
"""

import json
import threading
import time
from pathlib import Path

import RNS

from trenchchat.config import Config
from trenchchat.core.identity import Identity
from trenchchat.core.storage import Storage
from trenchchat.core.channel import ChannelManager
from trenchchat.core.messaging import Messaging
from trenchchat.core.subscription import SubscriptionManager
from trenchchat.core.invite import InviteManager
from trenchchat.core.sync import SyncManager
from trenchchat.core.presence import PresenceManager
from trenchchat.core.user_directory import UserDirectory
from trenchchat.core.avatar import AvatarManager
from trenchchat.core.reaction import ReactionManager
from trenchchat.network.router import Router
from trenchchat.network.announce import PeerAnnounceHandler, UserAnnounceHandler

RETICULUM_CONFIG_TEMPLATE = """\
[reticulum]
enable_transport = {enable_transport}
share_instance = No
instance_name = {instance_name}

[logging]
loglevel = 3

[interfaces]
  [[TesterLink]]
    type = {iface_type}
    interface_enabled = true
{iface_body}
"""


def _write_reticulum_config(rns_dir: Path, instance_name: str, role: str,
                            listen_port: int, peer_host: str, peer_port: int,
                            enable_transport: bool = False) -> None:
    """
    role="server": bind a TCPServerInterface on 127.0.0.1:listen_port.
    role="client": dial a TCPClientInterface at peer_host:peer_port.
    Exactly one interface either way -- a deterministic point-to-point
    link between the two testers, independent of LAN multicast/firewalls.

    enable_transport: set True so this instance will relay traffic between
    other peers connected to it (e.g. routing between a 3rd party client
    plugged into its TCPServerInterface and the other tester) -- off by
    default since a plain 2-tester link never needs to route through
    either side.
    """
    if role == "server":
        iface_type = "TCPServerInterface"
        iface_body = f"    listen_ip = 127.0.0.1\n    listen_port = {listen_port}"
    elif role == "client":
        iface_type = "TCPClientInterface"
        iface_body = f"    target_host = {peer_host}\n    target_port = {peer_port}"
    else:
        raise ValueError(f"unknown role: {role}")

    rns_dir.mkdir(parents=True, exist_ok=True)
    config_text = RETICULUM_CONFIG_TEMPLATE.format(
        enable_transport="True" if enable_transport else "False",
        instance_name=instance_name,
        iface_type=iface_type,
        iface_body=iface_body,
    )
    (rns_dir / "config").write_text(config_text)


class Backend:
    """All the wired-up managers for one tester, plus lifecycle helpers."""

    def __init__(self, data_dir: Path, display_name: str, role: str,
                listen_port: int, peer_host: str, peer_port: int,
                instance_name: str, enable_transport: bool = False):
        self.data_dir = data_dir
        data_dir.mkdir(parents=True, exist_ok=True)

        rns_dir = data_dir / "reticulum"
        _write_reticulum_config(rns_dir, instance_name, role,
                                listen_port, peer_host, peer_port,
                                enable_transport=enable_transport)
        self.rns_config_path = str(rns_dir / "config")

        self.config = Config(data_dir=data_dir)
        self.config.display_name = display_name

        self.rns = RNS.Reticulum(configdir=str(rns_dir), loglevel=RNS.LOG_NOTICE)

        self.identity = Identity(self.config, identity_path=data_dir / "identity")
        self.storage = Storage(db_path=data_dir / "storage.db")
        self.router = Router(self.config, self.identity,
                             storagepath=str(data_dir / "messagestore"))

        self.channel_mgr = ChannelManager(self.identity, self.storage)
        self.messaging = Messaging(self.identity, self.storage, self.router)
        self.subscription_mgr = SubscriptionManager(self.identity, self.storage, self.router)
        self.invite_mgr = InviteManager(self.identity, self.storage, self.router)
        self.sync_mgr = SyncManager(self.identity, self.storage, self.router,
                                    self.messaging, self.subscription_mgr, self.invite_mgr)
        self.presence_mgr = PresenceManager(self.identity.hash_hex, self.config)
        self.user_directory = UserDirectory(self.identity.hash_hex)
        self.avatar_mgr = AvatarManager(self.identity, self.config, self.storage, self.router)
        self.reaction_mgr = ReactionManager(self.identity, self.storage, self.router)

        # Mirrors main.py's _on_user_announced: a trenchchat.user announce is
        # the strongest signal a peer is a TrenchChat client (not just any
        # LXMF client), so it feeds both the directory and presence.
        def _on_user_announced(peer_hex: str, display_name: str, iface) -> None:
            self.user_directory.record_user(peer_hex, display_name)
            self.presence_mgr.record_seen(peer_hex)

        RNS.Transport.register_announce_handler(
            UserAnnounceHandler(_on_user_announced)
        )

        # Mirrors main_window.py's combined _on_peer_appeared handler: one
        # PeerAnnounceHandler registration drives both the sync manager's
        # gap-fill request and presence tracking, so a peer's LXMF delivery
        # announce is the single trigger for both. Without this, SyncManager
        # .on_peer_appeared() is never called at all in this harness.
        def _on_peer_appeared(peer_hex: str, iface) -> None:
            self.sync_mgr.on_peer_appeared(peer_hex)
            self.presence_mgr.record_seen(peer_hex)
            self.avatar_mgr.flush_avatar(peer_hex)

        RNS.Transport.register_announce_handler(
            PeerAnnounceHandler(_on_peer_appeared)
        )

        # Also mark a peer as seen on any inbound LXMF message, covering
        # peers reached via a backchannel link without a prior announce
        # (same rationale as main_window.py's _on_inbound_message).
        def _on_inbound_message(message) -> None:
            if not message.source_hash:
                return
            sender_identity = RNS.Identity.recall(message.source_hash)
            if sender_identity is not None:
                self.presence_mgr.record_seen(sender_identity.hash.hex())

        self.router.add_delivery_callback(_on_inbound_message)

        self.channel_mgr.restore_owned_channels()

    def accept_invite(self, channel_hash_hex: str, token: bytes, expiry: float,
                      admin_hex: str) -> None:
        """Same call main_window.py's _on_accept_invite makes. invite.py's
        _send_raw has no retry queue (unlike chat messages), so it silently
        drops the join_request if the admin's path isn't known yet -- a real
        human clicking "Accept" usually has enough natural delay for that to
        have resolved already; bridge the gap here for callers (like a
        scripted test) that might not."""
        if not self.path_known(admin_hex):
            self.warm_up(admin_hex, timeout=15.0, interval=1.0)
        self.invite_mgr.send_join_request(channel_hash_hex, token, expiry, admin_hex)

    def announce(self):
        self.router.announce()
        self.router.announce_user()
        self.channel_mgr.announce_all_owned()

    def start_heartbeat(self, interval: float = 1.5) -> None:
        """Re-announce on a timer for the life of the process, mirroring the
        real app's periodic reannounce QTimer. Runs as a daemon thread so it
        never blocks process exit."""
        def _loop():
            while True:
                try:
                    self.announce()
                except Exception as e:
                    RNS.log(f"TesterBackend: heartbeat announce failed: {e}", RNS.LOG_WARNING)
                time.sleep(interval)

        t = threading.Thread(target=_loop, daemon=True, name="heartbeat")
        t.start()

    def start_presence_pruner(self, interval: float = 15.0) -> None:
        """Periodically prune stale presence and user directory entries,
        mirroring main_window.py's _on_presence_tick. Runs as a daemon
        thread so it never blocks process exit."""
        def _loop():
            while True:
                time.sleep(interval)
                try:
                    self.presence_mgr.prune()
                    self.user_directory.prune()
                except Exception as e:
                    RNS.log(f"TesterBackend: presence prune failed: {e}", RNS.LOG_WARNING)

        t = threading.Thread(target=_loop, daemon=True, name="presence-pruner")
        t.start()

    @property
    def hash_hex(self) -> str:
        return self.identity.hash_hex

    def write_identity_file(self):
        """Publish this tester's identity hash to disk so the peer/orchestrator
        can discover it without a manual key exchange."""
        (self.data_dir / "identity_hash.json").write_text(
            json.dumps({"hash_hex": self.hash_hex, "display_name": self.config.display_name})
        )

    def close(self):
        self.storage.close()

    def path_known(self, peer_hash_hex: str) -> bool:
        """Whether we can currently resolve peer_hash_hex's LXMF delivery
        identity -- i.e. whether a send to them would go through instead
        of being dropped (invite.py's _send_raw has no retry queue, unlike
        chat messages, so callers must confirm the path first)."""
        delivery_dest_hash = RNS.Destination.hash(
            bytes.fromhex(peer_hash_hex), "lxmf", "delivery"
        )
        return RNS.Identity.recall(delivery_dest_hash) is not None

    def warm_up(self, peer_hash_hex: str | None = None, timeout: float = 20.0,
               interval: float = 1.0) -> bool:
        """
        Re-announce periodically until the link to the peer is confirmed
        (or timeout). Mirrors the natural delay a human has before their
        first action in the GUI, during which the periodic 60s reannounce
        timer would normally have had time to fire at least once; here we
        deliberately shorten that interval since we don't want the smoke
        test to take a full minute.

        Returns True once peer_hash_hex's path is known (or immediately if
        no peer_hash_hex is given -- just re-announces on a timer).
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.announce()
            if peer_hash_hex is not None and self.path_known(peer_hash_hex):
                return True
            time.sleep(interval)
        return peer_hash_hex is None or self.path_known(peer_hash_hex)


def wait_for_identity_file(data_dir: Path, timeout: float = 15.0) -> dict:
    """Block until the given tester's identity_hash.json appears, then return it."""
    path = data_dir / "identity_hash.json"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            try:
                return json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        time.sleep(0.1)
    raise TimeoutError(f"identity file never appeared at {path}")
