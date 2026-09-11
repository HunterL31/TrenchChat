"""
Test fixtures for TrenchChat integration tests.

Each test peer gets its own isolated data directory, SQLite database and
Reticulum identity, and a Router wired to a FakeTransport rather than to
Reticulum. FakeTransport implements the network/base.py Transport interface,
so the managers run against the same seam they run against in production and
only the path below it is replaced: delivery goes straight to the recipient's
Router, on its own thread after a short delay, the way LXMF delivers.

Run the suite with --direct and every peer also gets a real IPTransport
listening on 127.0.0.1, with a QUIC session opened to every other peer as it
is built. The same managers then run over real handshakes, real frames and
real acknowledgements, and Router picks the direct path for every peer that
has one. A test whose subject is the Reticulum path itself carries the
reticulum_path marker and stays on FakeTransport in both modes.

A single RNS.Reticulum instance is still stood up for the session, because
Identity and core/naming.py mint real RNS destinations and hashes; nothing in
these tests sends over it.
"""

import hashlib
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import RNS

from trenchchat.config import Config
from trenchchat.core import actions
from trenchchat.core.identity import Identity
from trenchchat.core.protocol import pack_fields
from trenchchat.core.storage import Storage
from trenchchat.core.channel import ChannelManager
from trenchchat.core.direct import DirectMessageManager
from trenchchat.core.files import FileManager
from trenchchat.core.friends import FriendsManager
from trenchchat.core.messaging import Messaging
from trenchchat.core.subscription import SubscriptionManager
from trenchchat.core.invite import InviteManager
from trenchchat.core.reaction import ReactionManager
from trenchchat.core.presence import PresenceManager
from trenchchat.core.server import ServerManager
from trenchchat.core.sync import SyncManager
from trenchchat.core.voice import VoiceManager
from trenchchat.network.base import (
    InboundMessage, PATH_RETICULUM, SendState, Transport, TransportLimits,
    reticulum_limits,
)
from trenchchat.network.ip.transport import IPTransport
from trenchchat.network.lxmf_transport import LXMFTransport
from trenchchat.network.router import Router

from tests.fake_file_transport import FakeFileRegistry, FakeFileTransport
from tests.fake_voice import FakeVoiceRegistry, FakeVoiceTransport


def pytest_addoption(parser):
    """--direct runs every peer_factory peer over a real direct session."""
    parser.addoption(
        "--direct", action="store_true", default=False,
        help="give every test peer an IPTransport and open sessions between "
             "them, so the manager suite runs over direct QUIC sessions",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "reticulum_path: the test is about the Reticulum path itself, so its "
        "peers stay on FakeTransport even under --direct",
    )


# ---------------------------------------------------------------------------
# In-process message transport
# ---------------------------------------------------------------------------

# How long a message spends in flight, so delivery lands on another thread
# after the sender has carried on, the way LXMF delivers.
DELIVERY_DELAY_SECS = 0.05


class FakeNetwork:
    """Every peer's transport, addressed by identity hash.

    Stands in for the mesh: a peer is reachable while it is registered here,
    and unreachable the moment it is torn down.
    """

    def __init__(self):
        self._peers: dict[str, "FakeTransport"] = {}
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    def register(self, transport: "FakeTransport") -> None:
        """Make a peer reachable."""
        with self._lock:
            self._peers[transport.self_hex] = transport

    def unregister(self, transport: "FakeTransport") -> None:
        """Stop delivering to a peer and wait for anything already in flight.

        Delivery runs on its own thread, so without this a message dispatched
        moments before teardown lands in handlers that then query a Storage
        whose connection has just been closed. sqlite3 doesn't raise across
        threads for that -- it faults the interpreter.
        """
        with self._lock:
            if self._peers.get(transport.self_hex) is transport:
                del self._peers[transport.self_hex]
            threads = list(self._threads)
        for thread in threads:
            thread.join(timeout=2.0)
        with self._lock:
            self._threads = [t for t in self._threads if t.is_alive()]

    def peer(self, peer_hex: str) -> "FakeTransport | None":
        """The transport for a peer, or None if it is not on the network."""
        with self._lock:
            return self._peers.get(peer_hex)

    def reachable(self, peer_hex: str) -> bool:
        """Whether a peer is currently on the network."""
        return self.peer(peer_hex) is not None

    def deliver_later(self, target: "FakeTransport", message: InboundMessage) -> None:
        """Hand a message to a peer on its own thread, after a short flight."""
        def _deliver():
            time.sleep(DELIVERY_DELAY_SECS)
            if self.peer(target.self_hex) is not target:
                return
            try:
                target.accept(message)
            except Exception as e:
                RNS.log(f"FakeNetwork: delivery error: {e}", RNS.LOG_ERROR)

        thread = threading.Thread(target=_deliver, daemon=True)
        with self._lock:
            self._threads.append(thread)
        thread.start()


class FakeTransport(Transport):
    """Delivers between in-process peers without touching Reticulum.

    Authentication is the transport's job on every path, so it is modelled
    here too: an authentic message reaches the recipient's Router, and one a
    test forged is refused before any handler sees it.
    """

    def __init__(self, self_hex: str, network: FakeNetwork, config: Config):
        self.self_hex = self_hex
        self._network = network
        self._config = config
        self._inbound_callback = None
        self._propagation_node: bytes | None = None
        self._announce_all = None
        # Peers this node cannot address right now, so a test can model a
        # path that has not resolved without unregistering the peer.
        self.unreachable: set[str] = set()
        # LXMF address hex -> the identity hash behind it, for the tests that
        # add a contact by address rather than by identity.
        self.addresses: dict[str, str] = {}
        self.address_requests: list[str] = []
        # Every message this peer put on the wire, in the form it went in:
        # the envelope as packed, so a test can assert what a foreign client
        # would actually receive.
        self.outbox: list[InboundMessage] = []
        self.announces: list = []
        self.channel_announces: list = []
        self.registered_channels: list[str] = []
        self.display_name = ""

    # --- message plane ---

    def send(self, dest_hex: str, fields: dict, content: str = "", *,
             on_delivered=None, on_failed=None, propagated: bool = False,
             envelope: bool = True) -> SendState:
        """Deliver to a peer on this network. NO_PATH if it is not on it."""
        return self.send_as(self.self_hex, dest_hex, fields, content,
                            on_failed=on_failed, envelope=envelope)

    def send_as(self, source_hex: str, dest_hex: str, fields: dict,
                content: str = "", *, on_failed=None, envelope: bool = True,
                authentic: bool = True) -> SendState:
        """Send while claiming to be source_hex, authentically or not.

        Tests use the dishonest form to model a peer that sets someone else's
        address on a message its signature cannot back.
        """
        target = (None if dest_hex in self.unreachable
                  else self._network.peer(dest_hex))
        if target is None:
            if on_failed is not None:
                on_failed(dest_hex)
            return SendState.NO_PATH
        message = InboundMessage(
            source_hex=source_hex,
            fields=pack_fields(fields) if envelope else dict(fields),
            content=content,
            timestamp=time.time(),
            hash=hashlib.sha256(
                f"{source_hex}:{dest_hex}:{time.time()!r}".encode()).digest(),
            path=PATH_RETICULUM,
        )
        self.outbox.append(message)
        if not authentic:
            RNS.log(
                f"FakeTransport: dropped inbound message with invalid "
                f"signature, claimed source {source_hex[:16]}…",
                RNS.LOG_WARNING,
            )
            return SendState.SENT
        self._network.deliver_later(target, message)
        return SendState.SENT

    def accept(self, message: InboundMessage) -> None:
        """Hand an authenticated message up to this peer's Router."""
        if self._inbound_callback is not None:
            self._inbound_callback(message)

    def can_reach(self, dest_hex: str) -> bool:
        """Whether the peer is on this network and addressable from here."""
        return (dest_hex not in self.unreachable
                and self._network.reachable(dest_hex))

    def request_path(self, dest_hex: str) -> None:
        """Nothing to ask: a peer on this network is reachable or it is not."""

    def limits_for(self, dest_hex: str) -> TransportLimits:
        """The Reticulum budgets, so managers read the production values."""
        return reticulum_limits()

    def drain(self, timeout: float) -> int:
        """Sends leave immediately here, so there is never anything to wait for."""
        return 0

    def set_inbound_callback(self, callback) -> None:
        """Register the callback every authenticated message arrives on."""
        self._inbound_callback = callback

    def stop(self) -> None:
        """Nothing to persist or tear down."""

    # --- peer keys ---

    def public_key_for(self, peer_hex: str) -> bytes | None:
        """The peer's public key, if a peer_factory peer owns that identity."""
        identity = signing_identity(peer_hex)
        return identity.get_public_key() if identity is not None else None

    def resolve_address(self, address_hex: str) -> str | None:
        """The identity behind an address, if this network knows one.

        A peer's own identity hash addresses it here; anything else has to be
        registered in `addresses` first, as an announce would teach it.
        """
        resolved = self.addresses.get(address_hex)
        if resolved is not None:
            return resolved
        if self._network.reachable(address_hex):
            return address_hex
        self.address_requests.append(address_hex)
        return None

    # --- announces ---

    def set_announce_all(self, announce_all) -> None:
        """Register what an announce of everything sends."""
        self._announce_all = announce_all

    def announce(self, attached_interface=None) -> None:
        """Record an announce; nothing on this network listens for one."""
        self.announces.append(attached_interface)

    def announce_user(self, attached_interface=None) -> None:
        """Record a trenchchat.user announce."""
        self.announces.append(attached_interface)

    def register_channel(self, channel_hash_hex: str, aspect_name: str) -> None:
        """Record a channel address claim."""
        if channel_hash_hex not in self.registered_channels:
            self.registered_channels.append(channel_hash_hex)

    def announce_channel(self, channel_hash_hex: str, app_data: bytes,
                         attached_interface=None) -> None:
        """Record a channel announce and its metadata."""
        self.channel_announces.append((channel_hash_hex, app_data))

    def set_display_name(self, display_name: str) -> None:
        """Record the name this node would announce."""
        self.display_name = display_name

    def release_quarantined(self, identity_hex: str) -> None:
        """Nothing is ever quarantined here: every sender is known."""

    # --- propagation ---

    def enable_propagation(self) -> None:
        """Host a store for other nodes' mail; here, only the setting moves."""
        self._config.propagation_enabled = True

    def disable_propagation(self) -> None:
        """Stop hosting a store."""
        self._config.propagation_enabled = False

    @property
    def outbound_propagation_node(self) -> bytes | None:
        """The node offline direct messages are left with, if one is set."""
        return self._propagation_node

    def set_outbound_propagation_node(self, destination_hash: bytes) -> None:
        """Choose the node offline direct messages are left with."""
        self._propagation_node = destination_hash

    def request_propagation_sync(self) -> bool:
        """No node holds anything on this network."""
        return False


class DirectTestTransport(IPTransport):
    """A real direct session that honours the fake network's reachability.

    A test makes a peer unreachable by putting it in FakeTransport.unreachable,
    which stands for a path that has not resolved. A live QUIC session would
    reach it anyway and the test would be asserting nothing, so this path is
    unreachable wherever the other one is.
    """

    def __init__(self, config: Config, identity, fake: FakeTransport):
        super().__init__(config, identity, authorize=lambda _peer: True,
                         listen_host="127.0.0.1", listen_port=0)
        self._fake = fake

    def can_reach(self, dest_hex: str) -> bool:
        """Whether a session is up and the test has not cut the peer off."""
        return (dest_hex not in self._fake.unreachable
                and super().can_reach(dest_hex))


def deliver(source, recipient: "TestPeer", fields: dict, content: str = "", *,
            envelope: bool = True) -> None:
    """Hand one authenticated message straight to a peer's Router.

    source is the sending peer, or the identity hex it claims to be. Delivery
    is immediate rather than threaded, so a test can assert on what the
    handlers did as soon as this returns.
    """
    source_hex = source if isinstance(source, str) else source.identity.hash_hex
    recipient.transport.accept(InboundMessage(
        source_hex=source_hex,
        fields=pack_fields(fields) if envelope else dict(fields),
        content=content,
        timestamp=time.time(),
        hash=hashlib.sha256(f"{source_hex}:{content}".encode()).digest(),
        path=PATH_RETICULUM,
    ))


def forge(sender: "TestPeer", recipient: "TestPeer", fields: dict,
          content: str = "", *, claimed_source_hex: str | None = None,
          envelope: bool = True) -> SendState:
    """Send a message whose signature does not back the address on it.

    Models a peer that set someone else's source address: LXMF flags it as
    SIGNATURE_INVALID and still delivers it, so the transport is what refuses
    it. Nothing reaches a handler, which is the whole claim.
    """
    return sender.transport.send_as(
        claimed_source_hex or sender.identity.hash_hex,
        recipient.identity.hash_hex, fields, content,
        envelope=envelope, authentic=False,
    )


def lxmf_transport_for(peer: "TestPeer", *, inbound: bool = False) -> LXMFTransport:
    """A real Reticulum transport beside a peer, torn down with it.

    Peers run on FakeTransport, so the handful of tests that are about LXMF
    itself -- its wire format, its quarantine, its propagation-node mode --
    stand one of these up rather than asserting against a model of it. With
    inbound set, what it authenticates goes into the peer's own Router, which
    is the production chain from the wire to the managers.

    One per peer: a second LXMRouter would re-register the same delivery
    identity, which RNS refuses.
    """
    if peer.lxmf_transport is not None:
        if inbound:
            peer.lxmf_transport.set_inbound_callback(peer.router._on_inbound)
        return peer.lxmf_transport
    transport = LXMFTransport(
        peer.config, peer.identity,
        storagepath=str(peer.data_dir / "messagestore"),
    )

    def _stop():
        # LXMRouter.jobloop is `while True` with no exit condition, and
        # exit_handler sets a flag jobloop never reads, so every router keeps a
        # thread calling jobs() against torn-down state for the life of the
        # process. LXMF exposes no way to stop it, so make the thread harmless:
        # it keeps spinning, but on nothing.
        transport.lxmf_router.jobs = lambda: None
        transport.stop()
        for dest in (transport.owned_destinations()
                     + [transport.lxmf_router.propagation_destination]):
            if dest is not None:
                try:
                    RNS.Transport.deregister_destination(dest)
                except Exception:
                    pass
        for handler in transport.announce_handlers():
            try:
                RNS.Transport.deregister_announce_handler(handler)
            except Exception:
                pass

    peer._teardown_callbacks.insert(0, _stop)
    peer.lxmf_transport = transport
    if inbound:
        transport.set_inbound_callback(peer.router._on_inbound)
    return transport


@dataclass
class TestPeer:
    name: str
    data_dir: Path
    config: Config
    identity: Identity
    storage: Storage
    router: Router
    transport: FakeTransport
    channel_mgr: ChannelManager
    server_mgr: ServerManager
    messaging: Messaging
    subscription_mgr: SubscriptionManager
    invite_mgr: InviteManager
    reaction_mgr: ReactionManager
    sync_mgr: SyncManager
    presence_mgr: PresenceManager
    friends_mgr: FriendsManager
    direct_mgr: DirectMessageManager
    voice_mgr: VoiceManager
    voice_transport: FakeVoiceTransport
    file_mgr: FileManager
    file_transport: FakeFileTransport
    direct_file_transport: FakeFileTransport
    lxmf_transport: "LXMFTransport | None" = None
    ip_transport: "DirectTestTransport | None" = None
    _teardown_callbacks: list = field(default_factory=list, repr=False)

    def announce(self):
        """Announce delivery destination, user aspect and all owned channels."""
        self.router.announce_all()

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

    Nothing is sent over it: Identity and core/naming.py mint real RNS
    destinations and hashes, and those need a live stack.

    The config declares no interfaces.  Reticulum's default config enables
    AutoInterface, whose multicast discovery is not used by these tests at all
    (FakeTransport delivers between peers in-process) but does intermittently
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

# identity hash hex -> RNS.Identity, so test fixtures can sign fabricated
# history the way a real client would. Real messages carry an author
# signature (core/authorship.py); history poked straight into storage has to
# carry one too, or it is correctly treated as unverifiable.
_IDENTITY_REGISTRY: dict = {}

# Every peer currently on a FakeNetwork, so helpers can answer "is this peer
# reachable" the way the RNS destination table used to.
_LIVE_PEERS: set[str] = set()


def signing_identity(identity_hash_hex: str):
    """The RNS identity for a peer built by peer_factory, if known."""
    return _IDENTITY_REGISTRY.get(identity_hash_hex)


def peer_is_live(identity_hash_hex: str) -> bool:
    """Whether a peer built by peer_factory is still on its network."""
    return identity_hash_hex in _LIVE_PEERS


@pytest.fixture
def peer_factory(request, rns_instance, tmp_path):
    """
    Returns a factory function make_peer(name) -> TestPeer.

    Each peer gets its own subdirectory under pytest's tmp_path, so
    identities, databases, and message stores are fully isolated.

    A shared FakeNetwork carries messages between them, so a send reaches the
    recipient's Router without any Reticulum path resolution. Under --direct
    each peer also listens for direct sessions on 127.0.0.1 and opens one to
    every peer already built, so the same test runs over QUIC instead.
    """
    direct_by_default = (
        request.config.getoption("--direct")
        and request.node.get_closest_marker("reticulum_path") is None
    )
    created_peers: list[TestPeer] = []
    network = FakeNetwork()
    voice_registry = FakeVoiceRegistry()
    file_registry = FakeFileRegistry()

    def make_peer(name: str, display_name: str | None = None,
                  direct: bool | None = None,
                  open_sessions: bool = True) -> TestPeer:
        """One peer. direct overrides --direct for a test that needs a session.

        open_sessions False gives the peer a direct transport wired into its
        Router and no sessions on it, which is what the upgrade handshake needs
        in either mode: opening one is the thing under test.
        """
        direct = direct_by_default if direct is None else direct
        peer_dir = tmp_path / name
        peer_dir.mkdir(parents=True, exist_ok=True)

        identity_path = peer_dir / "identity"
        db_path = peer_dir / "storage.db"

        config = Config(data_dir=peer_dir)
        config._data["display_name"] = display_name or name.capitalize()

        identity = Identity(config, identity_path=identity_path)
        storage = Storage(db_path=db_path)
        transport = FakeTransport(identity.hash_hex, network, config)
        ip_transport = (DirectTestTransport(config, identity, transport)
                        if direct else None)
        router = Router(config, identity, transport=transport,
                        direct_transport=ip_transport)

        channel_mgr = ChannelManager(identity, storage, router)
        server_mgr = ServerManager(identity, storage)
        messaging = Messaging(identity, storage, router)
        subscription_mgr = SubscriptionManager(identity, storage, router)
        invite_mgr = InviteManager(identity, storage, router)
        reaction_mgr = ReactionManager(identity, storage, router)
        sync_mgr = SyncManager(identity, storage, router, messaging,
                               subscription_mgr, invite_mgr,
                               reaction_mgr=reaction_mgr)
        presence_mgr = PresenceManager(identity.hash_hex, config)
        friends_mgr = FriendsManager(storage, identity.hash_hex, presence_mgr,
                                     identity=identity, router=router)
        presence_mgr.add_seen_callback(friends_mgr.record_seen)
        presence_mgr.add_presence_callback(friends_mgr.record_presence)
        direct_mgr = DirectMessageManager(identity, storage, friends_mgr, presence_mgr)
        messaging.set_direct_manager(direct_mgr)
        friends_mgr.set_message_filer(
            lambda peer_hex, _f=friends_mgr, _d=direct_mgr, _m=messaging:
                actions.file_message_requests(_f, _d, _m, peer_hex)
        )
        reaction_mgr.set_direct_manager(direct_mgr)
        trenchchat_gate = actions.trenchchat_peer_gate(storage)
        messaging.set_trenchchat_gate(trenchchat_gate)
        reaction_mgr.set_trenchchat_gate(trenchchat_gate)

        voice_transport = FakeVoiceTransport(identity.hash_hex, voice_registry)
        voice_mgr = VoiceManager(identity, storage, router, subscription_mgr,
                                 config, transport=voice_transport,
                                 state_refresh_secs=0.5, roster_ttl_secs=2.0)

        file_transport = FakeFileTransport(identity.hash_hex, file_registry)
        # Wired for every peer and reaching nobody until a test opens a
        # session on it, so a test that is not about the direct path sees the
        # mesh plane exactly as it always did.
        direct_file_transport = FakeFileTransport(identity.hash_hex,
                                                  file_registry, direct=True)
        file_mgr = FileManager(identity, storage, presence_mgr,
                               transport=file_transport,
                               direct_transport=direct_file_transport)

        channel_mgr.restore_owned_channels()

        peer = TestPeer(
            name=name,
            data_dir=peer_dir,
            config=config,
            identity=identity,
            storage=storage,
            router=router,
            transport=transport,
            channel_mgr=channel_mgr,
            server_mgr=server_mgr,
            messaging=messaging,
            subscription_mgr=subscription_mgr,
            invite_mgr=invite_mgr,
            reaction_mgr=reaction_mgr,
            sync_mgr=sync_mgr,
            presence_mgr=presence_mgr,
            friends_mgr=friends_mgr,
            direct_mgr=direct_mgr,
            voice_mgr=voice_mgr,
            voice_transport=voice_transport,
            file_mgr=file_mgr,
            file_transport=file_transport,
            direct_file_transport=direct_file_transport,
            ip_transport=ip_transport,
        )

        # Drive VoiceManager.tick the way the testenv ticker thread would,
        # so fallback dialing, roster TTL expiry and speaking decay behave
        # under wait_for polling.
        ticker_stop = threading.Event()

        def _voice_ticker():
            while not ticker_stop.wait(0.2):
                try:
                    voice_mgr.tick()
                except Exception as e:
                    RNS.log(f"TestVoiceTicker: {e}", RNS.LOG_ERROR)

        ticker_thread = threading.Thread(target=_voice_ticker, daemon=True)
        ticker_thread.start()

        def _stop_voice():
            ticker_stop.set()
            ticker_thread.join(timeout=2.0)
            voice_mgr.leave_voice()
            voice_transport.stop()

        def _stop_files():
            file_mgr.stop()
            file_transport.join_threads()
            direct_file_transport.join_threads()

        def _leave_network(t=transport):
            _LIVE_PEERS.discard(t.self_hex)
            network.unregister(t)

        # Identity registers a real RNS destination, which otherwise stays in
        # the global destination table for the life of the session -- several
        # hundred by the end of a full run, which is what eventually faults
        # the interpreter on Windows.
        def _release_destinations(ident=identity):
            if ident.destination is not None:
                try:
                    RNS.Transport.deregister_destination(ident.destination)
                except Exception:
                    pass

        # Order matters: stop the voice ticker and inbound delivery before
        # anything they touch goes away, and close storage last.
        def _stop_direct(t=ip_transport):
            if t is not None:
                t.stop()

        peer._teardown_callbacks.append(_stop_direct)
        peer._teardown_callbacks.append(_stop_voice)
        peer._teardown_callbacks.append(_stop_files)
        peer._teardown_callbacks.append(_leave_network)
        peer._teardown_callbacks.append(_release_destinations)
        peer._teardown_callbacks.append(storage.close)
        created_peers.append(peer)
        _IDENTITY_REGISTRY[identity.hash_hex] = identity.rns_identity
        _LIVE_PEERS.add(identity.hash_hex)

        network.register(transport)
        if ip_transport is not None and open_sessions:
            for other in created_peers:
                # A peer torn down and rebuilt under the same name (a restart)
                # leaves its old self in the list, with this peer's identity.
                if (other.ip_transport is None
                        or other.identity.hash_hex == identity.hash_hex
                        or not peer_is_live(other.identity.hash_hex)):
                    continue
                assert ip_transport.open_session(
                    other.identity.hash_hex, "127.0.0.1",
                    other.ip_transport.listen_port,
                    other.ip_transport.certificate_der,
                ), f"no direct session between {name} and {other.name}"

        return peer

    yield make_peer

    for peer in created_peers:
        peer.teardown()
