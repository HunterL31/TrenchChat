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
from trenchchat.core.server import ServerManager
from trenchchat.core.sync import SyncManager
from trenchchat.network.router import Router


# ---------------------------------------------------------------------------
# In-process message transport
# ---------------------------------------------------------------------------

def forge(lxm: LXMF.LXMessage) -> LXMF.LXMessage:
    """Mark a message as having failed LXMF signature validation.

    Locally constructed LXMessages never go through pack/unpack, so the
    transport marks them signature-valid to model a correctly signed delivery.
    Adversarial tests wrap the message in this helper to model the opposite: a
    peer that set ``source_hash`` to someone else's delivery hash, which LXMF
    flags as SIGNATURE_INVALID but still delivers.
    """
    lxm._tc_forged = True
    lxm.signature_validated = False
    lxm.unverified_reason = LXMF.LXMessage.SIGNATURE_INVALID
    return lxm


class TestTransport:
    """
    Routes LXMF messages between in-process peers by handing them to the
    recipient's Router._on_message_received, bypassing only the Reticulum
    network layer -- not the router's own inbound authentication.

    Because these messages are built in-process rather than unpacked from
    wire bytes, LXMF's signature fields are never populated, so the transport
    stamps ``signature_validated = True`` to model an authentic signed
    delivery.  Tests that need an unauthenticated message call forge() on it
    first; see tests/test_adversarial.py.

    Usage:
        transport = TestTransport()
        transport.register(peer_a)
        transport.register(peer_b)
        # Now peer_a.router.send(lxm) delivers to peer_b's callbacks.
    """

    def __init__(self):
        # delivery_dest_hash_hex -> Router
        self._peers: dict[str, Router] = {}
        self._threads: list[threading.Thread] = []

    def register(self, peer: "TestPeer"):
        dest_hash_hex = peer.router.delivery_destination.hash.hex()
        self._peers[dest_hash_hex] = peer.router
        # Patch the peer's router.send to go through this transport
        peer.router.send = self._make_send(peer.identity.hash_hex)

    def unregister(self, peer: "TestPeer"):
        """Stop delivering to a peer and wait for anything already in flight.

        Delivery runs on its own thread, so without this a message dispatched
        moments before teardown lands in handlers that then query a Storage
        whose connection has just been closed. sqlite3 doesn't raise across
        threads for that -- it faults the interpreter.
        """
        self._peers.pop(peer.router.delivery_destination.hash.hex(), None)
        for thread in list(self._threads):
            thread.join(timeout=2.0)
        self._threads = [t for t in self._threads if t.is_alive()]

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
            # Model LXMF's signature verdict: authentic unless the test forged it.
            if getattr(lxm, "_tc_forged", False):
                lxm.signature_validated = False
                lxm.unverified_reason = LXMF.LXMessage.SIGNATURE_INVALID
            else:
                lxm.signature_validated = True
                lxm.unverified_reason = None

            # Deliver asynchronously (matches real LXMF behaviour)
            def _deliver():
                time.sleep(0.05)
                # The recipient may have been torn down while this was queued.
                if self._peers.get(dest_hash_hex) is not recipient_router:
                    return
                try:
                    recipient_router._on_message_received(lxm)
                except Exception as e:
                    import RNS as _RNS
                    _RNS.log(f"TestTransport: delivery error: {e}", _RNS.LOG_ERROR)
            thread = threading.Thread(target=_deliver, daemon=True)
            self._threads.append(thread)
            thread.start()
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
    server_mgr: ServerManager
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

    The config declares no interfaces.  Reticulum's default config enables
    AutoInterface, whose multicast discovery is not used by these tests at all
    (TestTransport delivers between peers in-process) but does intermittently
    fault the interpreter on Windows -- "No multicast echoes received" followed
    by an access violation -- which crashes the run before pytest can report.
    Declaring an empty interface set removes that source of nondeterminism
    without changing what any test exercises.
    """
    rns_dir = tmp_path_factory.mktemp("rns_config")
    (rns_dir / "config").write_text(
        "[reticulum]\n"
        "  enable_transport = False\n"
        "  share_instance = False\n"
        "  panic_on_interface_error = False\n"
        "\n"
        "[logging]\n"
        "  loglevel = 3\n"
        "\n"
        "[interfaces]\n",
        encoding="utf-8",
    )
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
        server_mgr = ServerManager(identity, storage)
        messaging = Messaging(identity, storage, router)
        subscription_mgr = SubscriptionManager(identity, storage, router)
        invite_mgr = InviteManager(identity, storage, router)
        sync_mgr = SyncManager(identity, storage, router, messaging,
                               subscription_mgr, invite_mgr)

        channel_mgr.restore_owned_channels()
        server_mgr.restore_owned_servers()

        peer = TestPeer(
            name=name,
            data_dir=peer_dir,
            config=config,
            identity=identity,
            storage=storage,
            router=router,
            channel_mgr=channel_mgr,
            server_mgr=server_mgr,
            messaging=messaging,
            subscription_mgr=subscription_mgr,
            invite_mgr=invite_mgr,
            sync_mgr=sync_mgr,
        )
        # Every peer stands up an LXMRouter with its own destinations, links
        # and callbacks.  Left running, these accumulate across the whole
        # session -- several hundred by the end of a full run -- and the
        # interpreter eventually faults on Windows partway through. Tearing
        # each one down with the peer keeps a full-suite run stable.
        def _stop_router(r=router, ch=channel_mgr, sv=server_mgr, ident=identity):
            # LXMRouter.jobloop is `while True` with no exit condition, and
            # exit_handler sets a flag jobloop never reads, so every router
            # keeps a thread calling jobs() against torn-down state for the
            # life of the process -- which is what faults the interpreter once
            # enough have piled up. LXMF exposes no way to stop it, so make the
            # thread harmless: it keeps spinning, but on nothing.
            r.lxmf_router.jobs = lambda: None
            r.lxmf_router.exit_handler()
            # exit_handler tears down LXMF's own delivery/user destinations and
            # unhooks propagation_destination's callbacks, but never deregisters
            # propagation_destination itself, nor the destinations Identity and
            # ChannelManager/ServerManager register directly with RNS.Transport --
            # all of those otherwise stay in the global destination table for the
            # life of the session.
            owned = (list(getattr(ch, "_owned_destinations", {}).values())
                     + list(getattr(sv, "_owned_destinations", {}).values()))
            for dest in ([getattr(r, "_user_dest", None),
                          getattr(r, "_delivery_dest", None),
                          r.lxmf_router.propagation_destination,
                          ident.destination]
                         + owned):
                if dest is not None:
                    try:
                        RNS.Transport.deregister_destination(dest)
                    except Exception:
                        pass
            handler = getattr(ch, "_announce_handler", None)
            if handler is not None:
                try:
                    RNS.Transport.deregister_announce_handler(handler)
                except Exception:
                    pass

        # Order matters: stop inbound delivery before anything it touches goes
        # away, and close storage last.
        peer._teardown_callbacks.append(lambda p=peer: transport.unregister(p))
        peer._teardown_callbacks.append(_stop_router)
        peer._teardown_callbacks.append(storage.close)
        created_peers.append(peer)

        # Register with the shared transport so messages are delivered in-process
        transport.register(peer)

        return peer

    yield make_peer

    for peer in created_peers:
        peer.teardown()
