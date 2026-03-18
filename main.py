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

import RNS
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


def main():
    # --- config ---
    config = Config()

    # --- Reticulum ---
    rns = RNS.Reticulum()

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
