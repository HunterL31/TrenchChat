"""
Test fixtures for TrenchChat integration tests.

Each test peer gets its own isolated data directory, SQLite database,
Reticulum identity, and LXMF router. All peers share a single
RNS.Reticulum instance (singleton).

Network delivery between same-process peers uses a TestTransport shim:
since all peers share the same RNS instance, LXMF's `has_path` check
always returns False for locally-registered destinations (they're not
in the routing table). The TestTransport intercepts router.send() calls
and directly invokes the recipient's delivery callbacks, allowing full
end-to-end testing of message formatting, field parsing, storage, and
business logic without requiring actual network transport.
"""

import time
import threading
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

import pytest
import RNS
import LXMF

from trenchchat.config import Config
from trenchchat.core.identity import Identity
from trenchchat.core.storage import Storage
from trenchchat.core.channel import ChannelManager
from trenchchat.core.messaging import Messaging
from trenchchat.core.subscription import SubscriptionManager
from trenchchat.core.invite import InviteManager
from trenchchat.core.sync import SyncManager
from trenchchat.network.router import Router


# ---------------------------------------------------------------------------
# In-process message transport
# ---------------------------------------------------------------------------

class TestTransport:
    """
    Routes LXMF messages between in-process peers by directly invoking
    delivery callbacks, bypassing the Reticulum network layer.

    Usage:
        transport = TestTransport()
        transport.register(peer_a)
        transport.register(peer_b)
        # Now peer_a.router.send(lxm) delivers to peer_b's callbacks.
    """

    def __init__(self):
        # delivery_dest_hash_hex -> Router
        self._peers: dict[str, Router] = {}

    def register(self, peer: "TestPeer"):
        dest_hash_hex = peer.router.delivery_destination.hash.hex()
        self._peers[dest_hash_hex] = peer.router
        # Patch the peer's router.send to go through this transport
        peer.router.send = self._make_send(peer.identity.hash_hex)

    def _make_send(self, sender_identity_hex: str):
        def send(lxm: LXMF.LXMessage):
            dest_hash = lxm.get_destination().hash
            dest_hash_hex = dest_hash.hex()
            recipient_router = self._peers.get(dest_hash_hex)
            if recipient_router is None:
                # Unknown destination — simulate delivery failure
                if hasattr(lxm, "_failed_callback") and lxm._failed_callback:
                    lxm._failed_callback(lxm)
                return
            # Deliver asynchronously (matches real LXMF behaviour)
            def _deliver():
                time.sleep(0.05)
                for cb in recipient_router._delivery_callbacks:
                    try:
                        cb(lxm)
                    except Exception as e:
                        import RNS as _RNS
                        _RNS.log(f"TestTransport: delivery callback error: {e}",
                                 _RNS.LOG_ERROR)
            threading.Thread(target=_deliver, daemon=True).start()
        return send


@dataclass
class TestPeer:
    name: str
    data_dir: Path
    config: Config
    identity: Identity
    storage: Storage
    router: Router
    channel_mgr: ChannelManager
    messaging: Messaging
    subscription_mgr: SubscriptionManager
    invite_mgr: InviteManager
    sync_mgr: SyncManager
    _teardown_callbacks: list = field(default_factory=list, repr=False)

    def announce(self):
        """Announce delivery destination and all owned channels."""
        self.router.announce()
        self.channel_mgr.announce_all_owned()

    def teardown(self):
        for cb in self._teardown_callbacks:
            try:
                cb()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Session-scoped Reticulum instance
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def rns_instance(tmp_path_factory):
    """
    Initialize a single RNS.Reticulum for the entire test session.
    Uses a temp config dir so it doesn't touch ~/.reticulum.
    """
    rns_dir = tmp_path_factory.mktemp("rns_config")
    rns = RNS.Reticulum(configdir=str(rns_dir), loglevel=RNS.LOG_WARNING)
    yield rns


# ---------------------------------------------------------------------------
# Per-test peer factory
# ---------------------------------------------------------------------------

@pytest.fixture
def peer_factory(rns_instance, tmp_path):
    """
    Returns a factory function make_peer(name) -> TestPeer.

    Each peer gets its own subdirectory under pytest's tmp_path, so
    identities, databases, and message stores are fully isolated.

    A shared TestTransport is used so that router.send() calls are
    delivered directly to the recipient's callbacks without requiring
    actual Reticulum network paths.
    """
    created_peers: list[TestPeer] = []
    transport = TestTransport()

    def make_peer(name: str, display_name: str | None = None) -> TestPeer:
        peer_dir = tmp_path / name
        peer_dir.mkdir(parents=True, exist_ok=True)

        identity_path = peer_dir / "identity"
        db_path = peer_dir / "storage.db"
        messagestore_path = str(peer_dir / "messagestore")

        config = Config(data_dir=peer_dir)
        config._data["display_name"] = display_name or name.capitalize()

        identity = Identity(config, identity_path=identity_path)
        storage = Storage(db_path=db_path)
        router = Router(config, identity, storagepath=messagestore_path)

        channel_mgr = ChannelManager(identity, storage)
        messaging = Messaging(identity, storage, router)
        subscription_mgr = SubscriptionManager(identity, storage, router)
        invite_mgr = InviteManager(identity, storage, router)
        sync_mgr = SyncManager(identity, storage, router, messaging,
                               subscription_mgr, invite_mgr)

        channel_mgr.restore_owned_channels()

        peer = TestPeer(
            name=name,
            data_dir=peer_dir,
            config=config,
            identity=identity,
            storage=storage,
            router=router,
            channel_mgr=channel_mgr,
            messaging=messaging,
            subscription_mgr=subscription_mgr,
            invite_mgr=invite_mgr,
            sync_mgr=sync_mgr,
        )
        peer._teardown_callbacks.append(storage.close)
        created_peers.append(peer)

        # Register with the shared transport so messages are delivered in-process
        transport.register(peer)

        return peer

    yield make_peer

    for peer in created_peers:
        peer.teardown()
