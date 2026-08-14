"""
Production wiring for a headless TrenchChat backend.

Mirrors main.py's object graph (Identity, Storage, Router, core managers,
announce handlers) rather than devtools/testenv/backend_core.Backend's,
which is deliberately shaped for two testers on one machine (a generated
point-to-point Reticulum config, shortened presence timeouts, no
propagation-node sync). ServiceBackend exposes the same attribute surface
devtools/testenv/api.py's create_app() expects, so the same FastAPI routes
run unmodified against either backend.
"""

import os
import threading
import time

import RNS

from trenchchat.config import Config
from trenchchat.core import lockbox
from trenchchat.core.avatar import AvatarManager
from trenchchat.core.channel import ChannelManager
from trenchchat.core.identity import Identity
from trenchchat.core.invite import InviteManager
from trenchchat.core.messaging import Messaging
from trenchchat.core.presence import PresenceBeacon, PresenceManager, resolve_display_name
from trenchchat.core.reaction import ReactionManager
from trenchchat.core.server import ServerManager
from trenchchat.core.storage import Storage
from trenchchat.core.subscription import SubscriptionManager
from trenchchat.core.sync import SyncManager
from trenchchat.core.user_directory import UserDirectory
from trenchchat.network.announce import ChannelAnnounceHandler, PeerAnnounceHandler, \
    UserAnnounceHandler
from trenchchat.network.router import Router

# Env var carrying the PIN, for callers that can't pass encryption_key
# directly (e.g. a process launched by a supervisor rather than embedded).
# The value is a PIN, not a raw key -- it goes through lockbox.unlock() the
# same way a human's UnlockDialog entry would.
PIN_ENV_VAR = "TRENCHCHAT_PIN"

_REANNOUNCE_INTERVAL_SECS = 60.0
_INTERFACE_POLL_INTERVAL_SECS = 0.5
_INTERFACE_POLL_TIMEOUT_SECS = 30.0


def _resolve_encryption_key(encryption_key: bytes | None) -> bytes | None:
    """Resolve the storage/identity encryption key for this process.

    Precedence: an explicit argument, then TRENCHCHAT_PIN via lockbox.unlock().
    Raises lockbox.WrongPinError if a PIN is supplied but doesn't match.

    There is no unlock HTTP endpoint yet -- a locked install with no key
    available this way cannot be served headlessly. See the design plan
    for that gap; main.py's interactive UnlockDialog remains the only path
    for setting or changing a PIN.
    """
    if encryption_key is not None:
        return encryption_key
    if not lockbox.is_locked():
        return None
    pin = os.environ.get(PIN_ENV_VAR)
    if not pin:
        raise RuntimeError(
            f"Storage is PIN-locked but no key was supplied and {PIN_ENV_VAR} is unset."
        )
    return lockbox.unlock(pin)


class ServiceBackend:
    """All the wired-up managers for one production TrenchChat identity."""

    def __init__(self, encryption_key: bytes | None = None,
                rns_loglevel: int = RNS.LOG_NOTICE):
        key = _resolve_encryption_key(encryption_key)
        self._link_callbacks: list = []

        self.config = Config()

        # No configdir override -- attaches to the user's real Reticulum
        # config (~/.reticulum), or a running shared instance, exactly like
        # main.py. The dev harness generates its own point-to-point config
        # per tester; production has no such per-run config to generate.
        self.rns = RNS.Reticulum(loglevel=rns_loglevel)
        self.rns_config_path = RNS.Reticulum.configpath

        self.identity = Identity(self.config, encryption_key=key)
        self.storage = Storage(encryption_key=key)
        self.router = Router(self.config, self.identity)

        self.channel_mgr = ChannelManager(self.identity, self.storage)
        self.server_mgr = ServerManager(self.identity, self.storage)
        self.messaging = Messaging(self.identity, self.storage, self.router)
        self.subscription_mgr = SubscriptionManager(self.identity, self.storage, self.router)
        self.invite_mgr = InviteManager(self.identity, self.storage, self.router)
        self.sync_mgr = SyncManager(self.identity, self.storage, self.router,
                                    self.messaging, self.subscription_mgr, self.invite_mgr)
        # No timeout_secs/beacon_after_secs override: the defaults in
        # presence.py (300s / 180s) are the production values -- the dev
        # harness is the one that overrides them, down to 60s/30s.
        self.presence_mgr = PresenceManager(self.identity.hash_hex, self.config)
        self.presence_beacon = PresenceBeacon(
            self.identity, self.storage, self.router, self.subscription_mgr,
            self.presence_mgr,
        )
        self.router.add_outbound_callback(self.presence_beacon.record_sent)
        self.user_directory = UserDirectory(self.identity.hash_hex)
        self.avatar_mgr = AvatarManager(self.identity, self.config, self.storage, self.router)
        self.reaction_mgr = ReactionManager(self.identity, self.storage, self.router)

        self._register_announce_handlers()

        self.channel_mgr.restore_owned_channels()
        self.server_mgr.restore_owned_servers()

        self.router.add_delivery_callback(self._on_inbound_message)

    # --- announce handlers ---

    def _register_announce_handlers(self) -> None:
        def _on_user_announced(peer_hex: str, display_name: str, iface) -> None:
            self.user_directory.record_user(peer_hex, display_name)
            self.presence_mgr.record_seen(peer_hex)

        RNS.Transport.register_announce_handler(UserAnnounceHandler(_on_user_announced))

        def _on_peer_appeared(peer_hex: str, iface) -> None:
            self.sync_mgr.on_peer_appeared(peer_hex)
            self.presence_mgr.record_seen(peer_hex)
            self.avatar_mgr.flush_avatar(peer_hex)

        RNS.Transport.register_announce_handler(PeerAnnounceHandler(_on_peer_appeared))

        # main_window.py registers this to seed presence and the user
        # directory from channel announces, without waiting for a separate
        # trenchchat.user announce; backend_core.Backend never picked it up.
        def _on_channel_announce(destination_hash: bytes, announced_identity, metadata: dict,
                                 iface) -> None:
            if announced_identity is None:
                return
            peer_hex = announced_identity.hash.hex()
            self.presence_mgr.record_seen(peer_hex)
            display_name = resolve_display_name(
                peer_hex, self.identity.hash_hex, self.storage, self.config
            )
            self.user_directory.record_user(peer_hex, display_name)

        RNS.Transport.register_announce_handler(ChannelAnnounceHandler(_on_channel_announce))

    def _on_inbound_message(self, message) -> None:
        if not message.source_hash:
            return
        sender_identity = RNS.Identity.recall(message.source_hash)
        if sender_identity is not None:
            self.presence_mgr.record_seen(sender_identity.hash.hex())

    # --- startup / announce loop ---

    def start(self) -> None:
        """Announce, sync from the propagation node, and start the
        background announce loops. Call once after construction."""
        self._reannounce()
        self.router.sync_from_propagation_node()
        threading.Thread(target=self._run_interface_poller, daemon=True,
                         name="interface-poller").start()
        threading.Thread(target=self._run_reannounce_loop, daemon=True,
                         name="reannounce-loop").start()

    def _reannounce(self, attached_interface=None) -> None:
        self.router.announce(attached_interface=attached_interface)
        self.router.announce_user(attached_interface=attached_interface)
        self.channel_mgr.announce_all_owned(attached_interface=attached_interface)

    def _run_reannounce_loop(self) -> None:
        """Periodic full reannounce so newly-connected peers can discover us.
        Daemon-thread equivalent of main.py's 60s QTimer."""
        while True:
            time.sleep(_REANNOUNCE_INTERVAL_SECS)
            try:
                self._reannounce()
            except Exception as e:
                RNS.log(f"TrenchChat [service]: periodic reannounce failed: {e}",
                        RNS.LOG_WARNING)

    def _run_interface_poller(self) -> None:
        """Poll for the first interface to come online, then reannounce on
        it immediately, instead of guessing at a fixed startup delay.
        Daemon-thread equivalent of main.py's _poll_for_interface QTimer."""
        seen: set = set()
        elapsed = 0.0
        while elapsed < _INTERFACE_POLL_TIMEOUT_SECS:
            time.sleep(_INTERFACE_POLL_INTERVAL_SECS)
            elapsed += _INTERFACE_POLL_INTERVAL_SECS
            for iface in RNS.Transport.interfaces:
                if getattr(iface, "online", False) and iface not in seen:
                    seen.add(iface)
                    RNS.log(f"TrenchChat [service]: interface {iface} online, announcing on it",
                            RNS.LOG_DEBUG)
                    self._reannounce(attached_interface=iface)
            if seen:
                return
        RNS.log("TrenchChat [service]: interface poll timed out, announcing on all interfaces",
                RNS.LOG_WARNING)
        self._reannounce()

    # --- invites ---

    def accept_invite(self, channel_hash_hex: str, token: bytes, expiry: float,
                      admin_hex: str) -> None:
        """Send the join-request for a previously received invite.

        invite.py's _send_raw has no retry queue, so this requests the
        admin's path first if it isn't already known -- fire-and-forget, not
        a blocking wait (unlike Backend.warm_up in the dev harness, which
        exists only to make a scripted two-process test deterministic).
        """
        delivery_dest_hash = RNS.Destination.hash(bytes.fromhex(admin_hex), "lxmf", "delivery")
        if RNS.Identity.recall(delivery_dest_hash) is None:
            RNS.Transport.request_path(delivery_dest_hash)
        self.invite_mgr.send_join_request(channel_hash_hex, token, expiry, admin_hex)

    # --- link control ---
    #
    # /net/* and add_link_callback model the dev harness's single named
    # point-to-point TesterLink interface, a concept production has no
    # equivalent of (there may be zero, one, or many real interfaces, none
    # of them individually "the" link). These are honest no-ops rather than
    # a fake single-interface model.

    def add_link_callback(self, cb) -> None:
        self._link_callbacks.append(cb)

    def link_interface(self):
        return None

    def link_online(self) -> bool:
        return any(getattr(iface, "online", False) for iface in RNS.Transport.interfaces)

    def go_offline(self) -> bool:
        return False

    def go_online(self) -> bool:
        return False

    def close(self) -> None:
        self.storage.close()
