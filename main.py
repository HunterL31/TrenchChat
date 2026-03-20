"""
TrenchChat entry point.

Startup order:
  1. Load config
  2. Initialise Reticulum
  3. Build Identity (uses Reticulum keystore)
  4. Open SQLite storage
  5. Build Router (LXMFRouter + propagation filter)
  6. Build core managers (channel, messaging, subscription, invite)
  7. Restore owned channel destinations
  8. Announce presence
  9. Start PyQt6 event loop
"""

import sys
import signal
import argparse

import RNS
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from trenchchat.config import Config
from trenchchat.core.identity import Identity
from trenchchat.core.storage import Storage
from trenchchat.core.channel import ChannelManager
from trenchchat.core.messaging import Messaging
from trenchchat.core.subscription import SubscriptionManager
from trenchchat.core.invite import InviteManager
from trenchchat.network.router import Router
from trenchchat.gui.main_window import MainWindow

_REANNOUNCE_INTERVAL_MS = 60_000
_STARTUP_ANNOUNCE_DELAY_MS = 10_000


def main():
    parser = argparse.ArgumentParser(description="TrenchChat")
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable verbose Reticulum + TrenchChat debug logging"
    )
    args = parser.parse_args()

    # --- config ---
    config = Config()

    # --- Reticulum ---
    rns = RNS.Reticulum(loglevel=RNS.LOG_DEBUG if args.verbose else RNS.LOG_NOTICE)

    # --- identity ---
    identity = Identity(config)

    # --- storage ---
    storage = Storage()

    # --- network router ---
    router = Router(config, identity)

    # --- core managers ---
    channel_mgr = ChannelManager(identity, storage)
    messaging = Messaging(identity, storage, router)
    subscription_mgr = SubscriptionManager(identity, storage, router)
    invite_mgr = InviteManager(identity, storage, router)

    # Restore RNS destinations for channels we own
    channel_mgr.restore_owned_channels()

    # Announce our delivery destination and all owned channels
    router.announce()
    channel_mgr.announce_all_owned()

    # Sync from propagation node on startup if configured
    router.sync_from_propagation_node()

    # --- Qt app ---
    app = QApplication(sys.argv)
    app.setApplicationName("TrenchChat")

    # Allow Ctrl+C to quit cleanly
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    # Re-announce every minute so newly connected peers can discover us.
    # Also fires a second announce shortly after startup in case the TCP
    # interface to the hub wasn't ready when the first announce fired.
    def _reannounce():
        router.announce()
        channel_mgr.announce_all_owned()
        RNS.log("TrenchChat: re-announced delivery destination and channels", RNS.LOG_DEBUG)

    reannounce_timer = QTimer()
    reannounce_timer.timeout.connect(_reannounce)
    reannounce_timer.start(_REANNOUNCE_INTERVAL_MS)

    # Extra early announce to catch interfaces that come up late
    QTimer.singleShot(_STARTUP_ANNOUNCE_DELAY_MS, _reannounce)

    window = MainWindow(
        config=config,
        identity=identity,
        storage=storage,
        router=router,
        channel_mgr=channel_mgr,
        messaging=messaging,
        subscription_mgr=subscription_mgr,
        invite_mgr=invite_mgr,
    )
    window.show()

    exit_code = app.exec()
    storage.close()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
