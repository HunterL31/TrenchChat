"""
Legacy Qt entry point. The active client is the Flutter app -- launch it
with main_flutter.py; this stays until the migration finishes.

Startup order:
  1. Load config and record this build's version against the profile
  2. Start Qt application (required before showing any dialogs)
  3. PIN gate — if a lock is set, show UnlockDialog and derive the key
  4. Initialise Reticulum
  5. Build Identity (uses Reticulum keystore, optionally encrypted)
  6. Open SQLite storage (optionally encrypted via SQLCipher)
  7. Build Router (LXMFRouter + propagation filter)
  8. Build core managers (channel, messaging, subscription, invite)
  9. Restore owned channel destinations
 10. Announce presence
 11. Show main window and enter PyQt6 event loop
"""

import sys
import signal
import argparse

import RNS
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from trenchchat.config import Config
from trenchchat.core import lockbox
from trenchchat.core.avatar import AvatarManager
from trenchchat.core.friends import FriendsManager
from trenchchat.core.identity import Identity
from trenchchat.core.interfaces_config import default_rns_config_path, seed_initial_config
from trenchchat.core.reaction import ReactionManager
from trenchchat.core.storage import Storage
from trenchchat.core.channel import ChannelManager
from trenchchat.core.server import ServerManager
from trenchchat.core.messaging import Messaging
from trenchchat.core.presence import PresenceBeacon, PresenceManager
from trenchchat.core.subscription import SubscriptionManager
from trenchchat.core.invite import InviteManager
from trenchchat.core.user_directory import UserDirectory
from trenchchat.core.voice import VoiceManager
from trenchchat.network.router import REANNOUNCE_INTERVAL_SECS, Router
from trenchchat.network.voice_transport import RNSVoiceTransport
from trenchchat.network.announce import UserAnnounceHandler
from trenchchat.version import record_launch
from trenchchat.gui.main_window import MainWindow
from trenchchat.gui.pin_dialog import UnlockDialog

_REANNOUNCE_INTERVAL_MS = int(REANNOUNCE_INTERVAL_SECS * 1000)
_VOICE_TICK_INTERVAL_MS = 1_000
_INTERFACE_POLL_INTERVAL_MS = 500
_INTERFACE_POLL_TIMEOUT_MS = 30_000
_SIGNAL_POLL_INTERVAL_MS = 200


def main():
    parser = argparse.ArgumentParser(description="TrenchChat")
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable TrenchChat debug logging (RNS stays at NOTICE level)",
    )
    parser.add_argument(
        "--rns-debug", action="store_true",
        help="Enable full Reticulum debug logging (very verbose — includes backbone/transport internals)",
    )
    args = parser.parse_args()

    # --rns-debug enables the full RNS firehose; -v alone keeps RNS at NOTICE
    # so backbone/transport chatter doesn't drown TrenchChat's own messages.
    if args.rns_debug:
        rns_loglevel = RNS.LOG_DEBUG
    elif args.verbose:
        rns_loglevel = RNS.LOG_INFO
    else:
        rns_loglevel = RNS.LOG_NOTICE

    # --- config ---
    config = Config()

    # --- version record ---
    record_launch()

    # --- Qt app (must exist before any QDialog is shown) ---
    app = QApplication(sys.argv)
    app.setApplicationName("TrenchChat")

    # --- PIN gate ---
    encryption_key: bytes | None = None
    if lockbox.is_locked():
        dlg = UnlockDialog()
        if dlg.exec() != UnlockDialog.DialogCode.Accepted:
            sys.exit(0)
        encryption_key = dlg.raw_key

    # --- Reticulum ---
    # A machine with no Reticulum config at all gets the bootstrap seeds and
    # interface discovery before RNS reads the file.
    seed_initial_config(default_rns_config_path())
    rns = RNS.Reticulum(loglevel=rns_loglevel)

    # --- identity ---
    identity = Identity(config, encryption_key=encryption_key)

    # --- storage ---
    storage = Storage(encryption_key=encryption_key)

    # --- network router ---
    router = Router(config, identity)

    # --- core managers ---
    channel_mgr = ChannelManager(identity, storage)
    server_mgr = ServerManager(identity, storage)
    messaging = Messaging(identity, storage, router)
    subscription_mgr = SubscriptionManager(identity, storage, router)
    invite_mgr = InviteManager(identity, storage, router)
    presence_mgr = PresenceManager(identity.hash_hex, config)
    presence_beacon = PresenceBeacon(
        identity, storage, router, subscription_mgr, presence_mgr
    )
    router.add_outbound_callback(presence_beacon.record_sent)
    user_directory = UserDirectory(identity.hash_hex)
    avatar_mgr = AvatarManager(identity, config, storage, router)
    reaction_mgr = ReactionManager(identity, storage, router)
    friends_mgr = FriendsManager(storage, identity.hash_hex, presence_mgr)
    presence_mgr.add_seen_callback(friends_mgr.record_seen)
    presence_mgr.add_presence_callback(friends_mgr.record_presence)
    voice_transport = RNSVoiceTransport(identity)
    voice_mgr = VoiceManager(identity, storage, router, subscription_mgr,
                             config, transport=voice_transport)

    # Register the user announce handler before any announces go out so we
    # never miss a trenchchat.user announce from a peer that is already online.
    def _on_user_announced(peer_hex: str, display_name: str, iface) -> None:
        user_directory.record_user(peer_hex, display_name)
        presence_mgr.record_seen(peer_hex)

    RNS.Transport.register_announce_handler(UserAnnounceHandler(_on_user_announced))

    # Restore RNS destinations for channels and servers we own
    channel_mgr.restore_owned_channels()
    server_mgr.restore_owned_servers()

    # Announce our delivery destination, trenchchat.user, and all owned channels
    router.announce()
    router.announce_user()
    channel_mgr.announce_all_owned()

    # Re-announce every minute so newly connected peers can discover us.
    # Also fires a second announce shortly after startup in case the TCP
    # interface to the hub wasn't ready when the first announce fired.
    def _reannounce(attached_interface=None):
        """Announce on all interfaces (periodic) or a specific one (triggered)."""
        router.announce(attached_interface=attached_interface)
        router.announce_user(attached_interface=attached_interface)
        channel_mgr.announce_all_owned(attached_interface=attached_interface)
        if attached_interface is not None:
            RNS.log(
                f"TrenchChat: re-announced on {attached_interface}",
                RNS.LOG_DEBUG,
            )
        else:
            RNS.log("TrenchChat: re-announced on all interfaces", RNS.LOG_DEBUG)

    reannounce_timer = QTimer()
    reannounce_timer.timeout.connect(_reannounce)
    reannounce_timer.start(_REANNOUNCE_INTERVAL_MS)

    voice_tick_timer = QTimer()
    voice_tick_timer.timeout.connect(voice_mgr.tick)
    voice_tick_timer.start(_VOICE_TICK_INTERVAL_MS)

    # Poll for the first interface to come online, then re-announce on it
    # immediately.  This replaces blind startup timers: we announce as soon as
    # the network is actually ready rather than guessing at a fixed delay.
    # The poller stops itself once an online interface is found or after a
    # timeout, at which point it falls back to a broadcast announce.
    _interface_poll_elapsed = [0]
    _seen_interfaces: set = set()

    def _poll_for_interface():
        _interface_poll_elapsed[0] += _INTERFACE_POLL_INTERVAL_MS
        for iface in RNS.Transport.interfaces:
            if getattr(iface, "online", False) and iface not in _seen_interfaces:
                _seen_interfaces.add(iface)
                RNS.log(
                    f"TrenchChat: interface {iface} online, announcing on it",
                    RNS.LOG_DEBUG,
                )
                _reannounce(attached_interface=iface)

        if _seen_interfaces:
            _interface_poll_timer.stop()
        elif _interface_poll_elapsed[0] >= _INTERFACE_POLL_TIMEOUT_MS:
            RNS.log(
                "TrenchChat: interface poll timed out, announcing on all interfaces",
                RNS.LOG_WARNING,
            )
            _interface_poll_timer.stop()
            _reannounce()

    _interface_poll_timer = QTimer()
    _interface_poll_timer.timeout.connect(_poll_for_interface)
    _interface_poll_timer.start(_INTERFACE_POLL_INTERVAL_MS)

    window = MainWindow(
        config=config,
        identity=identity,
        storage=storage,
        rns=rns,
        router=router,
        channel_mgr=channel_mgr,
        server_mgr=server_mgr,
        messaging=messaging,
        subscription_mgr=subscription_mgr,
        invite_mgr=invite_mgr,
        presence_mgr=presence_mgr,
        user_directory=user_directory,
        avatar_mgr=avatar_mgr,
        reaction_mgr=reaction_mgr,
        presence_beacon=presence_beacon,
        voice_mgr=voice_mgr,
    )
    window.show()

    # --- shutdown ---
    # aboutToQuit fires once the quit is irreversible, so a close the user backs
    # out of never tells anyone we left.
    app.aboutToQuit.connect(presence_beacon.announce_offline)

    # Route Ctrl+C and SIGTERM through Qt so that notice still runs, rather than
    # the process exiting out from under it. Two constraints on where this can
    # live: RNS.Reticulum and LXMF.LXMRouter both install their own
    # SIGINT/SIGTERM handlers in their constructors, so registering any earlier
    # is silently overwritten; and Python cannot run a signal handler while the
    # Qt event loop is blocked inside C++, so the idle timer exists purely to
    # hand control back to the interpreter periodically.
    def _quit_on_signal(signum, frame):
        app.quit()

    signal.signal(signal.SIGINT, _quit_on_signal)
    signal.signal(signal.SIGTERM, _quit_on_signal)
    signal_timer = QTimer()
    signal_timer.timeout.connect(lambda: None)
    signal_timer.start(_SIGNAL_POLL_INTERVAL_MS)

    exit_code = app.exec()
    # Both libraries only register their teardown as atexit hooks, and those
    # never run: RNS ends the process with os._exit. Now that the signals come
    # through Qt, this is the only path that persists their state.
    router.stop()
    storage.close()
    RNS.Reticulum.exit_handler()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
