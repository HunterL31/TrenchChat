"""
TrenchChat entry point.

Startup order:
  1. Load config
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
from trenchchat.core.identity import Identity
from trenchchat.core.storage import Storage
from trenchchat.core.channel import ChannelManager
from trenchchat.core.messaging import Messaging
from trenchchat.core.presence import PresenceManager
from trenchchat.core.subscription import SubscriptionManager
from trenchchat.core.invite import InviteManager
from trenchchat.core.user_directory import UserDirectory
from trenchchat.network.router import Router
from trenchchat.network.announce import UserAnnounceHandler
from trenchchat.gui.main_window import MainWindow
from trenchchat.gui.pin_dialog import UnlockDialog

_REANNOUNCE_INTERVAL_MS = 60_000
_STARTUP_ANNOUNCE_DELAY_MS_FAST = 3_000
_STARTUP_ANNOUNCE_DELAY_MS_SLOW = 10_000


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
    else:
        rns_loglevel = RNS.LOG_NOTICE

    # --- config ---
    config = Config()

    # --- Qt app (must exist before any QDialog is shown) ---
    app = QApplication(sys.argv)
    app.setApplicationName("TrenchChat")

    # Allow Ctrl+C to quit cleanly
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    # --- PIN gate ---
    encryption_key: bytes | None = None
    if lockbox.is_locked():
        dlg = UnlockDialog()
        if dlg.exec() != UnlockDialog.DialogCode.Accepted:
            sys.exit(0)
        encryption_key = dlg.raw_key

    # --- Reticulum ---
    rns = RNS.Reticulum(loglevel=rns_loglevel)

    # --- identity ---
    identity = Identity(config, encryption_key=encryption_key)

    # --- storage ---
    storage = Storage(encryption_key=encryption_key)

    # --- network router ---
    router = Router(config, identity)

    # --- core managers ---
    channel_mgr = ChannelManager(identity, storage)
    messaging = Messaging(identity, storage, router)
    subscription_mgr = SubscriptionManager(identity, storage, router)
    invite_mgr = InviteManager(identity, storage, router)
    presence_mgr = PresenceManager(identity.hash_hex, config)
    user_directory = UserDirectory(identity.hash_hex)

    # Register the user announce handler before any announces go out so we
    # never miss a trenchchat.user announce from a peer that is already online.
    def _on_user_announced(peer_hex: str, display_name: str) -> None:
        user_directory.record_user(peer_hex, display_name)
        presence_mgr.record_seen(peer_hex)

    RNS.Transport.register_announce_handler(UserAnnounceHandler(_on_user_announced))

    # Restore RNS destinations for channels we own
    channel_mgr.restore_owned_channels()

    # Announce our delivery destination, trenchchat.user, and all owned channels
    router.announce()
    router.announce_user()
    channel_mgr.announce_all_owned()

    # Sync from propagation node on startup if configured
    router.sync_from_propagation_node()

    # Re-announce every minute so newly connected peers can discover us.
    # Also fires a second announce shortly after startup in case the TCP
    # interface to the hub wasn't ready when the first announce fired.
    def _reannounce():
        router.announce()
        router.announce_user()
        channel_mgr.announce_all_owned()
        RNS.log("TrenchChat: re-announced delivery destination and channels", RNS.LOG_DEBUG)

    reannounce_timer = QTimer()
    reannounce_timer.timeout.connect(_reannounce)
    reannounce_timer.start(_REANNOUNCE_INTERVAL_MS)

    # Two deferred re-announces to catch interfaces that come up late.
    # The fast one (3s) covers most TCP interfaces; the slow one (10s) covers
    # slower or congested links that take longer to establish.
    QTimer.singleShot(_STARTUP_ANNOUNCE_DELAY_MS_FAST, _reannounce)
    QTimer.singleShot(_STARTUP_ANNOUNCE_DELAY_MS_SLOW, _reannounce)

    window = MainWindow(
        config=config,
        identity=identity,
        storage=storage,
        rns=rns,
        router=router,
        channel_mgr=channel_mgr,
        messaging=messaging,
        subscription_mgr=subscription_mgr,
        invite_mgr=invite_mgr,
        presence_mgr=presence_mgr,
        user_directory=user_directory,
    )
    window.show()

    exit_code = app.exec()
    storage.close()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
