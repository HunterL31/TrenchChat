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
import os
import threading
import time
from pathlib import Path

import RNS

from trenchchat.config import DATA_DIR, Config
from trenchchat.core import lockbox
from trenchchat.core.bandwidth import SAMPLE_INTERVAL_SECS, BandwidthMonitor
from trenchchat.core.connectivity import LinkWatcher
from trenchchat.core.sync import SYNC_RETRY_SECS
from trenchchat.core.identity import Identity
from trenchchat.core.interfaces_config import default_rns_config_path, seed_initial_config
from trenchchat.core.storage import Storage
from trenchchat.core.channel import ChannelManager
from trenchchat.core.server import ServerManager
from trenchchat.core.messaging import Messaging
from trenchchat.core.subscription import SubscriptionManager
from trenchchat.core.invite import InviteManager
from trenchchat.core.sync import SyncManager
from trenchchat.core.presence import (
    PresenceBeacon, PresenceManager, resolve_display_name,
)
from trenchchat.core.protocol import F_DISPLAY_NAME, F_MSG_TYPE
from trenchchat.core.user_directory import UserDirectory
from trenchchat.core.avatar import AvatarManager
from trenchchat.core.direct import DirectMessageManager
from trenchchat.core.friends import FriendsManager
from trenchchat.core.propagation import PropagationCollector, PropagationNodes
from trenchchat.core.reaction import ReactionManager
from trenchchat.core.voice import VoiceManager
from trenchchat.core.audio.engine import make_tone_pipeline
from trenchchat.core.node_browser import NodeBrowserManager
from trenchchat.network.router import Router
from trenchchat.network.node_transport import RNSNodeTransport
from trenchchat.network.voice_transport import RNSVoiceTransport
from trenchchat.network.announce import (
    FirstContactAnnouncer, NodeAnnounceHandler, PathResponseHandler,
    PeerAnnounceHandler, PropagationAnnounceHandler, UserAnnounceHandler,
)
from trenchchat.version import record_launch

_LINK_INTERFACE_NAME = "TesterLink"

# TCPInterface.HW_MTU. A configured bitrate at or below 62500 makes RNS's
# optimise_mtu() set HW_MTU to None, which its own HDLC read loop then adds an
# int to -- every inbound frame raises, so the tester can send but never
# receive. Pinning the MTU turns optimise_mtu() into a no-op and avoids it.
_TCP_HW_MTU_BYTES = 262144

# Shortened presence intervals so a hand test can observe the beacon
# surviving the hub in minutes instead of the production 300s/180s.
_PRESENCE_TIMEOUT_SECS = 60.0
_PRESENCE_BEACON_AFTER_SECS = 30.0

# Shortened voice intervals, same rationale.
_VOICE_STATE_REFRESH_SECS = 10.0
_VOICE_ROSTER_TTL_SECS = 30.0

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


def _rns_loglevel() -> int:
    """RNS verbosity for a tester, from TC_TESTENV_LOGLEVEL.

    Defaults to LOG_NOTICE. Raise it to LOG_DEBUG (7) when a scenario needs to
    see why a peer stayed silent -- refusals in sync.py are logged at debug
    precisely because they are silent on the wire.
    """
    raw = os.environ.get("TC_TESTENV_LOGLEVEL")
    if not raw:
        return RNS.LOG_NOTICE
    try:
        return max(0, min(int(raw), 7))
    except ValueError:
        return RNS.LOG_NOTICE


def _write_reticulum_config(rns_dir: Path, instance_name: str, role: str,
                            listen_port: int, peer_host: str, peer_port: int,
                            enable_transport: bool = False,
                            link_bitrate: int = 0) -> None:
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

    link_bitrate: when the orchestrator is shaping this tester's link, the
    shaped rate in bps, so RNS's announce pacing and MTU match what the
    wire is actually doing. Omitted at 0, which leaves RNS's own guess.
    """
    if role == "server":
        iface_type = "TCPServerInterface"
        iface_body = f"    listen_ip = 127.0.0.1\n    listen_port = {listen_port}"
    elif role == "client":
        iface_type = "TCPClientInterface"
        iface_body = f"    target_host = {peer_host}\n    target_port = {peer_port}"
    else:
        raise ValueError(f"unknown role: {role}")

    if link_bitrate > 0:
        iface_body += (f"\n    bitrate = {link_bitrate}"
                       f"\n    fixed_mtu = {_TCP_HW_MTU_BYTES}")

    rns_dir.mkdir(parents=True, exist_ok=True)
    config_text = RETICULUM_CONFIG_TEMPLATE.format(
        enable_transport="True" if enable_transport else "False",
        instance_name=instance_name,
        iface_type=iface_type,
        iface_body=iface_body,
    )
    (rns_dir / "config").write_text(config_text)


# Checked more often than SYNC_RETRY_SECS so a request that ages out is
# re-asked promptly rather than up to a full interval late.
SYNC_TICK_SECS = SYNC_RETRY_SECS / 3


class Backend:
    """All the wired-up managers for one tester, plus lifecycle helpers."""

    def __init__(self, data_dir: Path, display_name: str, role: str,
                listen_port: int, peer_host: str, peer_port: int,
                instance_name: str, enable_transport: bool = False,
                link_bitrate: int = 0):
        self.data_dir = data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self._link_callbacks: list = []

        rns_dir = data_dir / "reticulum"
        _write_reticulum_config(rns_dir, instance_name, role,
                                listen_port, peer_host, peer_port,
                                enable_transport=enable_transport,
                                link_bitrate=link_bitrate)
        self.rns_config_path = str(rns_dir / "config")

        self.config = Config(data_dir=data_dir)
        self.config.display_name = display_name
        self.version_state = record_launch(data_dir)

        self.rns = RNS.Reticulum(configdir=str(rns_dir), loglevel=_rns_loglevel())

        self.identity = Identity(self.config, identity_path=data_dir / "identity")
        self.storage = Storage(db_path=data_dir / "storage.db")
        self.router = Router(self.config, self.identity,
                             storagepath=str(data_dir / "messagestore"))
        self._wire_managers(
            presence_timeout_secs=_PRESENCE_TIMEOUT_SECS,
            presence_beacon_after_secs=_PRESENCE_BEACON_AFTER_SECS,
            voice_state_refresh_secs=_VOICE_STATE_REFRESH_SECS,
            voice_roster_ttl_secs=_VOICE_ROSTER_TTL_SECS,
        )

    @classmethod
    def for_real_profile(cls, rns_configdir: str | None = None) -> "Backend":
        """Backend over the machine's real profile: ~/.trenchchat plus the
        default Reticulum config (real interfaces, real mesh), constructed
        exactly like main.py's wiring. Must not run alongside the desktop
        client -- both would announce the same identity and contend for the
        same database.

        Raises RuntimeError for a PIN-locked profile: there is no headless
        unlock path yet (see the migration board's unlock design question).
        """
        if lockbox.is_locked():
            raise RuntimeError(
                "This profile is PIN-locked and the headless backend has no "
                "unlock path yet. Remove the PIN in the desktop client's "
                "Settings to use it here."
            )
        self = cls.__new__(cls)
        self.data_dir = DATA_DIR
        self._link_callbacks = []
        self.config = Config()
        self.version_state = record_launch(self.data_dir)
        # A machine with no Reticulum config at all gets the bootstrap seeds
        # and interface discovery before RNS reads the file, so a fresh
        # install connects without a restart.
        seed_initial_config(default_rns_config_path(rns_configdir))
        self.rns = RNS.Reticulum(configdir=rns_configdir, loglevel=RNS.LOG_NOTICE)
        self.rns_config_path = str(Path(RNS.Reticulum.configdir) / "config")
        self.identity = Identity(self.config)
        self.storage = Storage()
        self.router = Router(self.config, self.identity)
        self._wire_managers(use_tone_audio=False)
        return self

    def _wire_managers(self, presence_timeout_secs: float | None = None,
                       presence_beacon_after_secs: float | None = None,
                       voice_state_refresh_secs: float | None = None,
                       voice_roster_ttl_secs: float | None = None,
                       use_tone_audio: bool = True) -> None:
        """Managers and announce handlers shared by both constructors,
        mirroring main.py. The presence overrides shorten the testenv's
        observation windows; None keeps the production defaults.

        use_tone_audio drives the tone pipeline for headless testers (no sound
        devices); a real profile passes False so VoiceManager builds the same
        real AudioPipeline main.py does, degrading to receive-only when the
        machine has no audio libraries or devices."""
        self.channel_mgr = ChannelManager(self.identity, self.storage)
        self.server_mgr = ServerManager(self.identity, self.storage)
        self.messaging = Messaging(self.identity, self.storage, self.router)
        self.subscription_mgr = SubscriptionManager(self.identity, self.storage, self.router)
        self.invite_mgr = InviteManager(self.identity, self.storage, self.router)
        self.reaction_mgr = ReactionManager(self.identity, self.storage, self.router)
        self.sync_mgr = SyncManager(self.identity, self.storage, self.router,
                                    self.messaging, self.subscription_mgr, self.invite_mgr,
                                    reaction_mgr=self.reaction_mgr)
        presence_kwargs = {}
        if presence_timeout_secs is not None:
            presence_kwargs["timeout_secs"] = presence_timeout_secs
        self.presence_mgr = PresenceManager(self.identity.hash_hex, self.config,
                                            **presence_kwargs)
        beacon_kwargs = {}
        if presence_beacon_after_secs is not None:
            beacon_kwargs["beacon_after_secs"] = presence_beacon_after_secs
        self.presence_beacon = PresenceBeacon(
            self.identity, self.storage, self.router, self.subscription_mgr,
            self.presence_mgr, **beacon_kwargs,
        )
        self.router.add_outbound_callback(self.presence_beacon.record_sent)
        self.user_directory = UserDirectory(self.identity.hash_hex)
        self.avatar_mgr = AvatarManager(self.identity, self.config, self.storage, self.router)
        self.friends_mgr = FriendsManager(self.storage, self.identity.hash_hex,
                                          self.presence_mgr, identity=self.identity,
                                          router=self.router)
        self.presence_mgr.add_seen_callback(self.friends_mgr.record_seen)
        self.presence_mgr.add_presence_callback(self.friends_mgr.record_presence)
        self.direct_mgr = DirectMessageManager(self.identity, self.storage,
                                               self.friends_mgr, self.presence_mgr)
        self.messaging.set_direct_manager(self.direct_mgr)
        self.messaging.set_presence_manager(self.presence_mgr)
        self.reaction_mgr.set_direct_manager(self.direct_mgr)
        # Answers a peer the first time we hear them: our own re-announce is
        # 15 minutes apart, and until they have heard us they cannot verify
        # anything we send -- it is quarantined at their end and dropped.
        self.first_contact = FirstContactAnnouncer(
            self.router, self.channel_mgr, self.identity.hash_hex,
        )
        self.propagation_nodes = PropagationNodes(self.config, self.router)
        self.propagation_collector = PropagationCollector(
            self.router, self.identity, self.propagation_nodes,
        )
        # Held mail is pulled, so a node being chosen is the first moment
        # there is anywhere to ask. A node restored from the last run is
        # already selected by the time this is registered, which is why the
        # collector also starts in its settling window rather than relying on
        # this alone.
        self.propagation_nodes.add_selection_callback(
            lambda _node: self.propagation_collector.collect_now()
        )
        RNS.Transport.register_announce_handler(
            PropagationAnnounceHandler(self.propagation_nodes.record_node)
        )
        # Headless testers have no sound devices; the tone pipeline feeds the
        # real encode/transmit path with a generated signal instead. A real
        # profile uses no factory, so VoiceManager builds the real
        # AudioPipeline (mic capture + playback), exactly as main.py does.
        voice_kwargs = {}
        if voice_state_refresh_secs is not None:
            voice_kwargs["state_refresh_secs"] = voice_state_refresh_secs
        if voice_roster_ttl_secs is not None:
            voice_kwargs["roster_ttl_secs"] = voice_roster_ttl_secs
        if use_tone_audio:
            voice_kwargs["audio_factory"] = make_tone_pipeline
        self.voice_transport = RNSVoiceTransport(self.identity)
        self.voice_mgr = VoiceManager(
            self.identity, self.storage, self.router, self.subscription_mgr,
            self.config, transport=self.voice_transport, **voice_kwargs,
        )

        self.node_transport = RNSNodeTransport(self.identity)
        self.node_browser = NodeBrowserManager(
            self.identity, self.storage, self.config,
            transport=self.node_transport,
        )

        def _on_node_discovered(node_hex: str, display_name: str, iface) -> None:
            self.node_browser.record_node_announce(node_hex, display_name, iface)

        RNS.Transport.register_announce_handler(
            NodeAnnounceHandler(_on_node_discovered)
        )

        # Mirrors main.py's _on_user_announced: a trenchchat.user announce is
        # the strongest signal a peer is a TrenchChat client (not just any
        # LXMF client), so it feeds both the directory and presence.
        def _on_user_announced(peer_hex: str, display_name: str, iface) -> None:
            self.user_directory.record_user(peer_hex, display_name)
            self.presence_mgr.record_seen(peer_hex)
            self.first_contact.note_peer(peer_hex, iface)

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
            self._seed_user_directory(peer_hex)
            self.avatar_mgr.flush_avatar(peer_hex)
            self.reaction_mgr.flush_pending_emoji(peer_hex)
            self.subscription_mgr.flush_pending(peer_hex)
            self.invite_mgr.flush_pending(peer_hex)
            self.friends_mgr.flush_pending(peer_hex)
            self.first_contact.note_peer(peer_hex, iface)

        RNS.Transport.register_announce_handler(
            PeerAnnounceHandler(_on_peer_appeared)
        )

        # A peer's identity can also arrive as a path response, which is how a
        # first message from someone we have never heard becomes verifiable.
        # Releasing the quarantine is what actually delivers it.
        def _on_identity_resolved(peer_hex: str) -> None:
            self.router.release_quarantined(peer_hex)
            self.presence_mgr.record_seen(peer_hex)

        RNS.Transport.register_announce_handler(
            PathResponseHandler(_on_identity_resolved)
        )

        # Also update presence and the user directory from any inbound LXMF
        # message, covering peers reached via a backchannel link without a
        # prior announce, peers signing off, and a peer's display name
        # travelling on a chat message (same rationale as main_window.py's
        # _on_inbound_message).
        self.router.add_delivery_callback(self._on_inbound_message)

        self.bandwidth = BandwidthMonitor()

        # Nothing else notices *our own* link returning: every catch-up path
        # is driven by hearing from a remote peer.
        self.link_watcher = LinkWatcher(self._on_link_restored)
        self.link_watcher.start()

        self.channel_mgr.restore_owned_channels()
        self.server_mgr.restore_owned_servers()

    def _on_link_restored(self) -> None:
        """Catch up after our own link returns.

        Channel history comes from peers; a direct message left with a
        propagation node while we were away has to be collected, or it stays
        there.
        """
        self.sync_mgr.request_sync_all()
        self.collect_propagated()

    def collect_propagated(self) -> bool:
        """Ask the selected propagation node for anything held for us."""
        return self.propagation_collector.collect_now()

    def _on_inbound_message(self, message) -> None:
        """Record presence and directory state from an inbound LXMF message.

        A chat message carries the sender's display name in F_DISPLAY_NAME, so
        record it straight into the directory; a control message only seeds the
        directory for an already-confirmed TrenchChat peer.
        """
        sender_hex = self.presence_mgr.record_inbound(message)
        if not sender_hex:
            return
        fields = message.fields or {}
        if F_MSG_TYPE in fields:
            self._seed_user_directory(sender_hex)
            return
        name = fields.get(F_DISPLAY_NAME, "")
        if isinstance(name, bytes):
            name = name.decode(errors="replace")
        if name:
            self.user_directory.record_user(sender_hex, name)
        else:
            self._seed_user_directory(sender_hex)

    def _seed_user_directory(self, peer_hex: str) -> None:
        """Refresh a confirmed TrenchChat peer's directory entry.

        Mirrors main_window._seed_user_directory: a peer is confirmed if they
        are already in the directory (from a prior trenchchat.user announce) or
        appear in any channel's members table. Resolves the best available name.
        """
        if (self.user_directory.contains(peer_hex)
                or peer_hex in self.storage.get_trenchchat_peer_identities()):
            name = resolve_display_name(
                peer_hex, self.identity.hash_hex, self.storage, self.config
            )
            self.user_directory.record_user(peer_hex, name)

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

    def announce(self, attached_interface=None):
        self.router.announce(attached_interface=attached_interface)
        self.router.announce_user(attached_interface=attached_interface)
        self.channel_mgr.announce_all_owned(attached_interface=attached_interface)

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

    def start_voice_ticker(self, interval: float = 1.0) -> None:
        """Drive VoiceManager.tick, mirroring main.py's voice tick QTimer.

        Also drives SyncManager.tick on its own slower cadence: an unanswered
        sync request has nothing else to re-trigger it once the announce burst
        that prompted it has passed, and PropagationCollector.tick, which
        owns its own cadence -- held direct messages are pulled, and a node
        with a path that was not up at startup is asked again here.

        Runs as a daemon thread so it never blocks process exit."""
        def _loop():
            last_sync_tick = 0.0
            while True:
                time.sleep(interval)
                try:
                    self.voice_mgr.tick()
                except Exception as e:
                    RNS.log(f"TesterBackend: voice tick failed: {e}", RNS.LOG_WARNING)
                try:
                    self.node_browser.tick()
                except Exception as e:
                    RNS.log(f"TesterBackend: node tick failed: {e}", RNS.LOG_WARNING)
                now = time.time()
                if now - last_sync_tick >= SYNC_TICK_SECS:
                    last_sync_tick = now
                    try:
                        self.sync_mgr.tick()
                    except Exception as e:
                        RNS.log(f"TesterBackend: sync tick failed: {e}", RNS.LOG_WARNING)
                try:
                    self.propagation_collector.tick(now)
                except Exception as e:
                    RNS.log(f"TesterBackend: propagation collect failed: {e}",
                            RNS.LOG_WARNING)
                try:
                    self.first_contact.tick(now)
                except Exception as e:
                    RNS.log(f"TesterBackend: first-contact announce failed: {e}",
                            RNS.LOG_WARNING)

        t = threading.Thread(target=_loop, daemon=True, name="voice-ticker")
        t.start()

    def start_bandwidth_sampler(self, interval: float = SAMPLE_INTERVAL_SECS) -> None:
        """Periodically sample the interface byte counters so /bandwidth can
        answer windowed rates. Runs as a daemon thread so it never blocks
        process exit."""
        def _loop():
            while True:
                time.sleep(interval)
                try:
                    self.bandwidth.sample()
                except Exception as e:
                    RNS.log(f"TesterBackend: bandwidth sample failed: {e}", RNS.LOG_WARNING)

        t = threading.Thread(target=_loop, daemon=True, name="bandwidth-sampler")
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
                    self.sync_mgr.status.prune()
                    self.presence_beacon.tick()
                    self.reaction_mgr.retry_pending_emoji()
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

    def announce_offline(self) -> int:
        """Same notice main.py sends from aboutToQuit -- tell channel peers we
        are shutting down so they drop us to offline now."""
        return self.presence_beacon.announce_offline()

    def close(self):
        self.link_watcher.stop()
        self.router.stop()
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
        first action in the GUI, during which the periodic reannounce
        timer would normally have had time to fire; here we deliberately
        shorten that interval so the smoke test doesn't have to wait it out.

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

    def link_interface(self):
        """Return this backend's own TesterLink interface object, or None."""
        for iface in RNS.Transport.interfaces:
            if _LINK_INTERFACE_NAME in str(iface):
                return iface
        return None

    def link_online(self) -> bool:
        """Whether the TesterLink interface is currently connected."""
        iface = self.link_interface()
        return iface is not None and iface.online and not iface.detached

    def add_link_callback(self, cb) -> None:
        """Register a callback invoked with (is_online: bool) on link state change."""
        self._link_callbacks.append(cb)

    def _fire_link_callbacks(self, is_online: bool) -> None:
        for cb in self._link_callbacks:
            try:
                cb(is_online)
            except Exception as e:
                RNS.log(f"TrenchChat [link]: callback error: {e}", RNS.LOG_ERROR)

    def go_offline(self) -> bool:
        """Detach the TesterLink interface, simulating this tester dropping off the
        network. Returns False if there's no interface, or it isn't ours to control
        (non-initiator, e.g. the listening side of a TCPServerInterface)."""
        iface = self.link_interface()
        if iface is None or not getattr(iface, "initiator", False):
            return False
        iface.detach()
        self._fire_link_callbacks(False)
        return True

    def go_online(self) -> bool:
        """Reconnect a previously detached TesterLink interface.

        detach() leaves the interface with detached=True and IN/OUT False (set by
        the read loop's teardown()); reconnect() never resets any of the three, so
        without clearing them by hand the interface stays blackholed even after the
        socket reconnects.
        """
        iface = self.link_interface()
        if iface is None or not getattr(iface, "initiator", False):
            return False

        iface.detached = False
        iface.IN = True
        iface.OUT = True

        def _reconnect():
            iface.reconnect()
            self.warm_up(timeout=10.0, interval=1.0)
            self._fire_link_callbacks(self.link_online())

        threading.Thread(target=_reconnect, daemon=True, name="link-reconnect").start()
        return True


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
