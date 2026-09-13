"""
The direct session plane: its wire format, its handshake, and what it refuses.

A session is pinned by certificate and proved by identity, and the tests that
matter most here are the ones that drive the wire by hand: a peer that sends
frames before it has proved anything, one that replays a HELLO it captured,
one that claims to be somebody else once it is on. The well-behaved path is
exercised by every other test file under --direct; this file is about the
badly-behaved one.
"""

import asyncio
import os
import socket
import struct
import time

import msgpack
import pytest
import RNS
from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.quic.connection import QuicConnection
from aioquic.quic.events import (
    ConnectionTerminated, HandshakeCompleted, StreamDataReceived,
)

from tests.helpers import wait_for
from trenchchat.config import Config
from trenchchat.core.identity import Identity
from trenchchat.network.base import PATH_DIRECT, PATH_RETICULUM, SendState
from trenchchat.network.ip import frames, punch
from trenchchat.network.ip.certificate import (
    CERT_FILE_NAME, SessionCertificate, fingerprint_for,
)
from trenchchat.network.ip.endpoint import (
    bind_datagram_socket, bind_listen_sockets, socket_address, unmap_host,
)
from trenchchat.network.ip.session import (
    MAX_PREAUTH_FRAMES, dialer_configuration, hello_digest,
)
from trenchchat.network.ip.transport import IPTransport, MAX_PENDING_HANDSHAKES

CONNECT_TIMEOUT_SECS = 10.0
FRAME_TIMEOUT_SECS = 5.0

# Long enough that a path read racing a path_changed handler loses the race
# every time, short enough to spend on one test.
SLOW_HANDLER_SECS = 0.3


# ---------------------------------------------------------------------------
# A node with a direct transport and nothing above it
# ---------------------------------------------------------------------------

class IPNode:
    """One identity listening for direct sessions, with its inbox exposed."""

    def __init__(self, name: str, config: Config, identity: Identity,
                 transport: IPTransport):
        self.name = name
        self.config = config
        self.identity = identity
        self.transport = transport
        self.inbox: list = []
        self.paths: list = []
        self.appeared: list = []
        self.observed: list = []
        transport.set_inbound_callback(self.inbox.append)
        transport.set_observed_callback(
            lambda peer_hex, host, port: self.observed.append(
                (peer_hex, host, port)))
        transport.set_peer_event_callbacks(
            peer_appeared=lambda peer_hex, _iface: self.appeared.append(peer_hex),
            path_changed=lambda peer_hex, path: self.paths.append((peer_hex, path)),
        )

    @property
    def hash_hex(self) -> str:
        """This node's identity hash."""
        return self.identity.hash_hex

    def open_to(self, other: "IPNode") -> bool:
        """Open a session to another node in this test."""
        return self.transport.open_session(
            other.hash_hex, "127.0.0.1", other.transport.listen_port,
            other.transport.certificate_der,
        )


@pytest.fixture
def ip_node(rns_instance, tmp_path):
    """Factory for IPNodes, torn down with the test."""
    nodes: list[IPNode] = []

    def make(name: str, *, authorize=None, listen_host: str = "127.0.0.1",
             listen_port: int = 0) -> IPNode:
        node_dir = tmp_path / name
        node_dir.mkdir(parents=True, exist_ok=True)
        config = Config(data_dir=node_dir)
        identity = Identity(config, identity_path=node_dir / "identity")
        transport = IPTransport(
            config, identity, authorize=authorize or (lambda _peer: True),
            listen_host=listen_host, listen_port=listen_port,
        )
        node = IPNode(name, config, identity, transport)
        nodes.append(node)
        return node

    yield make

    for node in nodes:
        node.transport.stop()
        try:
            RNS.Transport.deregister_destination(node.identity.destination)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# A client that writes exactly what a test tells it to
# ---------------------------------------------------------------------------

class RawClient:
    """A QUIC client with none of DirectSession's manners."""

    def __init__(self, protocol, transport, identity, certificate):
        self.protocol = protocol
        self.transport = transport
        self.identity = identity
        self.certificate = certificate

    def close(self) -> None:
        """Drop the connection and its socket."""
        self.protocol.close()
        self.transport.close()


class RawProtocol(QuicConnectionProtocol):
    """Reads frames into a queue and writes whatever bytes it is given."""

    def __init__(self, quic, stream_handler=None):
        super().__init__(quic, stream_handler=stream_handler)
        self.tls_done = asyncio.Event()
        self.termination: ConnectionTerminated | None = None
        self.stream_id: int | None = None
        self.inbound: asyncio.Queue = asyncio.Queue()
        self._decoder = frames.FrameDecoder(frames.MAX_FRAME_BYTES)

    def quic_event_received(self, event) -> None:
        """Queue every frame, and note the end of the connection."""
        if isinstance(event, HandshakeCompleted):
            self.tls_done.set()
        elif isinstance(event, StreamDataReceived):
            for frame in self._decoder.feed(event.data):
                self.inbound.put_nowait(frame)
        elif isinstance(event, ConnectionTerminated):
            self.termination = event
            self.tls_done.set()
            self.inbound.put_nowait(None)

    def open_stream(self) -> None:
        """Claim the control stream."""
        self.stream_id = self._quic.get_next_available_stream_id(
            is_unidirectional=False)

    def write(self, data: bytes) -> None:
        """Put bytes on the control stream, frames or not."""
        self._quic.send_stream_data(self.stream_id, data)
        self.transmit()

    async def next_frame(self, timeout: float = FRAME_TIMEOUT_SECS):
        """The next frame, or None when the peer ended the connection."""
        return await asyncio.wait_for(self.inbound.get(), timeout)


async def raw_connect(node: IPNode, pinned_der: bytes | None = None,
                      certificate: SessionCertificate | None = None
                      ) -> RawClient:
    """Connect to a node with its certificate pinned, as any peer would."""
    certificate = certificate or SessionCertificate.mint()
    configuration = dialer_configuration(
        certificate, pinned_der or node.transport.certificate_der)
    connection = QuicConnection(configuration=configuration)
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: RawProtocol(connection), sock=sock)
    protocol.connect(("127.0.0.1", node.transport.listen_port))
    await asyncio.wait_for(protocol.tls_done.wait(), CONNECT_TIMEOUT_SECS)
    return RawClient(protocol, transport, RNS.Identity(), certificate)


async def raw_hello(client: RawClient, node: IPNode, *,
                    timestamp: int | None = None) -> tuple[bytes, bytes]:
    """Open the stream, answer the nonce, and send a HELLO. Returns it and the nonce."""
    client.protocol.open_stream()
    client.protocol.write(frames.hi_frame())
    kind, payload = await client.protocol.next_frame()
    assert kind == frames.KIND_CHALLENGE
    nonce = payload["nonce"]
    stamp = int(time.time()) if timestamp is None else timestamp
    digest = hello_digest(client.certificate.fingerprint,
                          fingerprint_for(node.transport.certificate_der),
                          nonce, stamp)
    hello = frames.hello_frame(client.identity.get_public_key(), stamp,
                               client.identity.sign(digest),
                               certificate=client.certificate.der)
    client.protocol.write(hello)
    return hello, nonce


def envelope_from(client: RawClient, node: IPNode, *, source: bytes | None = None,
                  content: str = "hello") -> tuple[bytes, bytes]:
    """One signed envelope from a raw client, or from whoever source names."""
    packed = frames.pack_envelope(
        src=source if source is not None else client.identity.hash,
        dst=node.identity.hash, timestamp=time.time(), content=content,
        fields={}, protocol=True,
    )
    return packed, client.identity.sign(frames.envelope_digest(packed))


# ---------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------

class TestFrames:
    def test_every_frame_kind_round_trips(self):
        decoder = frames.FrameDecoder(frames.MAX_FRAME_BYTES)
        blob = (frames.hi_frame()
                + frames.challenge_frame(b"\x01" * 16)
                + frames.hello_frame(b"\x02" * 64, 12345, b"\x03" * 64,
                                     certificate=b"cert")
                + frames.msg_frame(b"envelope", b"signature")
                + frames.ack_frame(b"\x04" * 32)
                + frames.req_frame(7, "chunks", {"from": 1})
                + frames.resp_frame(7, True, {"bytes": b"x"}))
        read = decoder.feed(blob)
        assert [kind for kind, _ in read] == [
            frames.KIND_HI, frames.KIND_CHALLENGE, frames.KIND_HELLO,
            frames.KIND_MSG, frames.KIND_ACK, frames.KIND_REQ, frames.KIND_RESP,
        ]
        assert frames.read_msg(read[3][1]) == (b"envelope", b"signature")
        assert frames.read_ack(read[4][1]) == b"\x04" * 32
        assert frames.read_request(read[5][1]) == (7, "chunks", {"from": 1})
        assert frames.read_response(read[6][1]) == (7, True, {"bytes": b"x"})

    def test_a_frame_split_across_writes_is_read_once_whole(self):
        decoder = frames.FrameDecoder(frames.MAX_FRAME_BYTES)
        blob = frames.msg_frame(b"e" * 200, b"s" * 64)
        assert decoder.feed(blob[:3]) == []
        assert decoder.feed(blob[3:60]) == []
        assert decoder.buffered() > 0
        read = decoder.feed(blob[60:])
        assert len(read) == 1
        assert frames.read_msg(read[0][1]) == (b"e" * 200, b"s" * 64)

    def test_a_frame_over_the_limit_is_refused_before_it_is_read(self):
        decoder = frames.FrameDecoder(64)
        with pytest.raises(frames.FrameError):
            decoder.feed(frames.msg_frame(b"e" * 200, b"s" * 64))

    def test_a_body_that_is_not_a_map_is_refused(self):
        body = msgpack.packb([1, 2, 3], use_bin_type=True)
        blob = struct.pack(frames.FRAME_HEADER, frames.KIND_MSG, len(body)) + body
        with pytest.raises(frames.FrameError):
            frames.FrameDecoder(frames.MAX_FRAME_BYTES).feed(blob)

    def test_a_msg_without_its_parts_is_refused(self):
        with pytest.raises(frames.FrameError):
            frames.read_msg({"env": b"e"})
        with pytest.raises(frames.FrameError):
            frames.read_msg({"sig": b"s"})
        with pytest.raises(frames.FrameError):
            frames.read_ack({"hash": b"short"})


class TestEnvelope:
    def test_an_envelope_round_trips_with_its_fields(self):
        packed = frames.pack_envelope(
            src=b"\x01" * 16, dst=b"\x02" * 16, timestamp=1234.5,
            content="hello", fields={0x01: b"channel", 0x02: "Alice"},
            protocol=True,
        )
        parsed = frames.unpack_envelope(packed)
        assert parsed["src"] == b"\x01" * 16
        assert parsed["dst"] == b"\x02" * 16
        assert parsed["ts"] == 1234.5
        assert parsed["content"] == "hello"
        assert parsed["fields"] == {0x01: b"channel", 0x02: "Alice"}
        assert parsed["proto"] is True

    def test_an_envelope_is_named_by_the_hash_an_ack_carries(self):
        packed = frames.pack_envelope(
            src=b"\x01" * 16, dst=b"\x02" * 16, timestamp=1.0, content="",
            fields={}, protocol=False)
        assert len(frames.envelope_hash(packed)) == frames.ENVELOPE_HASH_BYTES
        assert frames.envelope_digest(packed).startswith(frames.ENVELOPE_DOMAIN)

    @pytest.mark.parametrize("mangle", [
        {"src": b"short"},
        {"dst": "not bytes"},
        {"ts": "soon"},
        {"content": 7},
        {"fields": [1, 2]},
        {"proto": "yes"},
    ])
    def test_an_envelope_of_the_wrong_shape_is_refused(self, mangle):
        body = {
            "src": b"\x01" * 16, "dst": b"\x02" * 16, "ts": 1.0,
            "content": "", "fields": {}, "proto": True,
        }
        body.update(mangle)
        with pytest.raises(frames.FrameError):
            frames.unpack_envelope(msgpack.packb(body, use_bin_type=True))

    def test_an_envelope_missing_a_field_is_refused(self):
        with pytest.raises(frames.FrameError):
            frames.unpack_envelope(msgpack.packb({"src": b"\x01" * 16},
                                                 use_bin_type=True))


# ---------------------------------------------------------------------------
# The certificate
# ---------------------------------------------------------------------------

class TestCertificate:
    def test_it_is_minted_once_and_reloaded_after_that(self, tmp_path):
        first = SessionCertificate.load_or_create(tmp_path)
        second = SessionCertificate.load_or_create(tmp_path)
        assert first.der == second.der
        assert first.fingerprint == second.fingerprint
        assert len(first.fingerprint) == 32

    def test_an_unreadable_certificate_is_re_minted(self, tmp_path):
        first = SessionCertificate.load_or_create(tmp_path)
        (tmp_path / CERT_FILE_NAME).write_bytes(b"not a certificate")
        second = SessionCertificate.load_or_create(tmp_path)
        assert second.der != first.der
        assert SessionCertificate.load_or_create(tmp_path).der == second.der

    @pytest.mark.skipif(os.name == "nt",
                        reason="POSIX modes; Windows uses an ACL instead")
    def test_the_certificate_file_is_owner_only(self, tmp_path):
        SessionCertificate.load_or_create(tmp_path)
        mode = (tmp_path / CERT_FILE_NAME).stat().st_mode
        assert mode & 0o077 == 0


# ---------------------------------------------------------------------------
# A session that behaves
# ---------------------------------------------------------------------------

class TestSession:
    def test_a_session_carries_a_message_and_acknowledges_it(self, ip_node):
        alice, bob = ip_node("alice"), ip_node("bob")
        assert alice.open_to(bob)

        delivered: list[str] = []
        state = alice.transport.send(bob.hash_hex, {0x01: b"channel"}, "hello",
                                     on_delivered=delivered.append)
        assert state is SendState.SENT
        assert wait_for(lambda: bob.inbox, msg="the message")
        message = bob.inbox[0]
        assert message.source_hex == alice.hash_hex
        assert message.content == "hello"
        assert message.fields == {0x01: b"channel"}
        assert message.path == PATH_DIRECT
        assert message.trenchchat_protocol is True
        assert wait_for(lambda: delivered == [bob.hash_hex],
                        msg="the acknowledgement")

    def test_both_ends_can_send_over_one_session(self, ip_node):
        alice, bob = ip_node("alice"), ip_node("bob")
        assert alice.open_to(bob)

        alice.transport.send(bob.hash_hex, {}, "out")
        bob.transport.send(alice.hash_hex, {}, "back")
        assert wait_for(lambda: bob.inbox and alice.inbox, msg="both messages")
        assert alice.inbox[0].content == "back"
        assert alice.inbox[0].source_hex == bob.hash_hex

    def test_a_message_for_a_client_that_is_not_trenchchat_stays_unwrapped(
            self, ip_node):
        alice, bob = ip_node("alice"), ip_node("bob")
        assert alice.open_to(bob)
        alice.transport.send(bob.hash_hex, {0x06: b"image"}, "hi",
                             envelope=False)
        assert wait_for(lambda: bob.inbox, msg="the message")
        assert bob.inbox[0].trenchchat_protocol is False

    def test_a_session_reports_the_direct_paths_budgets(self, ip_node):
        alice, bob = ip_node("alice"), ip_node("bob")
        assert alice.open_to(bob)
        limits = alice.transport.limits_for(bob.hash_hex)
        assert limits.control_messages_per_minute == 600
        assert limits.ephemeral_control is True
        assert limits.shared_file_bytes == 200 * 1024 * 1024

    def test_a_session_learns_the_peers_key_from_its_hello(self, ip_node):
        alice, bob = ip_node("alice"), ip_node("bob")
        assert alice.open_to(bob)
        assert alice.transport.public_key_for(bob.hash_hex) == \
            bob.identity.rns_identity.get_public_key()
        assert bob.transport.public_key_for(alice.hash_hex) == \
            alice.identity.rns_identity.get_public_key()
        assert alice.transport.public_key_for("ab" * 16) is None

    def test_a_session_coming_up_is_a_peer_appearing_on_a_new_path(self, ip_node):
        alice, bob = ip_node("alice"), ip_node("bob")
        assert alice.open_to(bob)
        assert wait_for(lambda: alice.appeared and bob.appeared,
                        msg="both peer events")
        assert (bob.hash_hex, PATH_DIRECT) in alice.paths
        assert (alice.hash_hex, PATH_DIRECT) in bob.paths
        assert bob.hash_hex in alice.appeared
        assert alice.hash_hex in bob.appeared

    def test_each_side_says_where_the_other_arrived_from(self, ip_node):
        """The one thing a node behind a NAT cannot work out for itself.

        Nothing inside its own network can see its translated address, and
        asking a service for it would be a center. The peer on the other end
        saw it, and says so in the hello that authenticates the session. Both
        ends say it: a node that only ever accepts sessions would otherwise
        learn nothing about itself, and a pair who each need an observation
        before they can name the other would come down to who dialled.
        """
        alice, bob = ip_node("alice"), ip_node("bob")
        assert alice.open_to(bob)

        assert wait_for(lambda: alice.observed and bob.observed,
                        msg="the observed address at both ends")
        peer_hex, host, port = alice.observed[0]
        assert peer_hex == bob.hash_hex
        assert host == "127.0.0.1"
        assert port == alice.transport.listen_port

        peer_hex, host, port = bob.observed[0]
        assert peer_hex == alice.hash_hex
        assert host == "127.0.0.1"
        assert port == bob.transport.listen_port

    def test_a_session_going_down_puts_the_peer_back_on_the_mesh(self, ip_node):
        alice, bob = ip_node("alice"), ip_node("bob")
        assert alice.open_to(bob)
        assert alice.transport.can_reach(bob.hash_hex)

        alice.transport.close_session(bob.hash_hex)
        assert wait_for(lambda: not alice.transport.can_reach(bob.hash_hex),
                        msg="the session to end")
        assert (bob.hash_hex, PATH_RETICULUM) in alice.paths
        assert wait_for(lambda: not bob.transport.can_reach(alice.hash_hex),
                        msg="the other end to notice")
        assert alice.transport.send(bob.hash_hex, {}, "too late") \
            is SendState.NO_PATH

    def test_a_path_is_announced_before_anyone_else_can_see_it(self, ip_node):
        """The state and the event are one step, on the way up and down.

        A handler that takes its time is what makes the order visible: while
        it runs, no other thread may see the session it is being told about.
        A manager that saw it first would act on a path this node had not
        announced, and nothing downstream could tell that had happened.
        """
        alice, bob = ip_node("alice"), ip_node("bob")

        def announce(peer_hex: str, path: str) -> None:
            time.sleep(SLOW_HANDLER_SECS)
            alice.paths.append((peer_hex, path))

        alice.transport.set_peer_event_callbacks(path_changed=announce)

        assert alice.open_to(bob)
        assert alice.transport.can_reach(bob.hash_hex)
        assert (bob.hash_hex, PATH_DIRECT) in alice.paths, \
            "the session was reachable before it was announced"

        alice.transport.close_session(bob.hash_hex)
        assert wait_for(lambda: not alice.transport.can_reach(bob.hash_hex),
                        msg="the session to end")
        assert (bob.hash_hex, PATH_RETICULUM) in alice.paths, \
            "the session was gone before its end was announced"

    def test_a_session_that_ends_fails_what_it_had_not_acknowledged(self, ip_node):
        alice, bob = ip_node("alice"), ip_node("bob")
        assert alice.open_to(bob)
        failed: list[str] = []
        session = alice.transport.session_for(bob.hash_hex)
        # Queue a message and end the session before its acknowledgement can
        # come back, which is what a dropped link looks like from here.
        alice.transport._loop.call_soon_threadsafe(
            lambda: (session.send_message(b"envelope", b"signature",
                                          None, failed.append),
                     session.shut_down("link dropped")))
        assert wait_for(lambda: failed == [bob.hash_hex],
                        msg="the failure callback")

    def test_datagrams_cross_an_authenticated_session(self, ip_node):
        alice, bob = ip_node("alice"), ip_node("bob")
        assert alice.open_to(bob)
        seen: list[bytes] = []
        bob_session = bob.transport.session_for(alice.hash_hex)
        bob.transport._loop.call_soon_threadsafe(
            lambda: bob_session._hooks.__setattr__(
                "on_datagram", lambda _s, data: seen.append(data)))
        session = alice.transport.session_for(bob.hash_hex)
        alice.transport._loop.call_soon_threadsafe(
            session.send_datagram, b"voice frame")
        assert wait_for(lambda: seen == [b"voice frame"], msg="the datagram")

    def test_stopping_closes_every_session(self, ip_node):
        alice, bob = ip_node("alice"), ip_node("bob")
        assert alice.open_to(bob)
        alice.transport.stop()
        assert alice.transport.session_count() == 0
        assert wait_for(lambda: not bob.transport.can_reach(alice.hash_hex),
                        msg="the other end to notice")


# ---------------------------------------------------------------------------
# A session that does not
# ---------------------------------------------------------------------------

class TestPinning:
    def test_a_third_party_cannot_stand_in_for_the_peer(self, ip_node):
        """A relay re-originating the connection holds the wrong key.

        The certificate is the connection's only trust root, so the TLS
        handshake ends before any application byte: mallory's listener answers
        with her own certificate and the pin does not match it.
        """
        alice, bob, mallory = ip_node("alice"), ip_node("bob"), ip_node("mallory")
        assert not alice.transport.open_session(
            bob.hash_hex, "127.0.0.1", mallory.transport.listen_port,
            bob.transport.certificate_der,
        )
        assert alice.transport.session_count() == 0
        assert mallory.transport.session_count() == 0

    def test_a_peer_that_answers_with_another_identity_is_refused(self, ip_node):
        """The pin proves a key, and the HELLO proves who holds it."""
        alice, bob, carol = ip_node("alice"), ip_node("bob"), ip_node("carol")
        assert not alice.transport.open_session(
            carol.hash_hex, "127.0.0.1", bob.transport.listen_port,
            bob.transport.certificate_der,
        )
        assert not alice.transport.can_reach(carol.hash_hex)


class TestHandshakeRefusals:
    def test_an_application_frame_before_the_hello_closes_the_session(self, ip_node):
        bob = ip_node("bob")

        async def run():
            client = await raw_connect(bob)
            client.protocol.open_stream()
            packed, signature = envelope_from(client, bob)
            client.protocol.write(frames.msg_frame(packed, signature))
            assert await client.protocol.next_frame() is None
            client.close()

        asyncio.run(run())
        assert bob.transport.session_count() == 0
        assert bob.inbox == []

    def test_an_oversize_frame_closes_the_session(self, ip_node):
        bob = ip_node("bob")

        async def run():
            client = await raw_connect(bob)
            client.protocol.open_stream()
            # A length no frame may claim, written before anything is parsed.
            client.protocol.write(struct.pack(
                frames.FRAME_HEADER, frames.KIND_MSG,
                frames.MAX_FRAME_BYTES + 1))
            assert await client.protocol.next_frame() is None
            client.close()

        asyncio.run(run())
        assert bob.transport.session_count() == 0

    def test_more_frames_than_the_preauth_queue_holds_close_the_session(
            self, ip_node):
        bob = ip_node("bob")

        async def run():
            client = await raw_connect(bob)
            client.protocol.open_stream()
            for _ in range(MAX_PREAUTH_FRAMES + 4):
                client.protocol.write(frames.hi_frame())
            assert await client.protocol.next_frame(timeout=10.0) is not None
            client.close()

        asyncio.run(run())
        assert bob.transport.session_count() == 0

    def test_a_hello_with_a_stale_timestamp_is_refused(self, ip_node):
        bob = ip_node("bob")

        async def run():
            client = await raw_connect(bob)
            await raw_hello(client, bob, timestamp=int(time.time()) - 3600)
            assert await client.protocol.next_frame() is None
            client.close()

        asyncio.run(run())
        assert bob.transport.session_count() == 0

    def test_a_captured_hello_cannot_be_replayed_on_a_new_connection(self, ip_node):
        """Every HELLO signs the nonce of the connection it was made on."""
        bob = ip_node("bob")

        async def run():
            first = await raw_connect(bob)
            hello, _nonce = await raw_hello(first, bob)
            kind, _payload = await first.protocol.next_frame()
            assert kind == frames.KIND_HELLO
            first.close()

            replay = await raw_connect(bob)
            replay.protocol.open_stream()
            replay.protocol.write(frames.hi_frame())
            kind, payload = await replay.protocol.next_frame()
            assert kind == frames.KIND_CHALLENGE
            replay.protocol.write(hello)
            assert await replay.protocol.next_frame() is None
            replay.close()

        asyncio.run(run())
        assert wait_for(lambda: bob.transport.session_count() == 0,
                        msg="both connections gone")

    def test_a_hello_signed_by_another_key_is_refused(self, ip_node):
        """The key in the HELLO is the key the signature is checked against.

        A relay that claims a member's public key has to sign the nonce with
        that member's private key, which is the thing it does not have.
        """
        bob = ip_node("bob")
        victim = RNS.Identity()

        async def run():
            client = await raw_connect(bob)
            client.protocol.open_stream()
            client.protocol.write(frames.hi_frame())
            kind, payload = await client.protocol.next_frame()
            assert kind == frames.KIND_CHALLENGE
            stamp = int(time.time())
            digest = hello_digest(
                client.certificate.fingerprint,
                fingerprint_for(bob.transport.certificate_der),
                payload["nonce"], stamp)
            # The victim's public key, signed by the caller's own identity.
            client.protocol.write(frames.hello_frame(
                victim.get_public_key(), stamp, client.identity.sign(digest),
                certificate=client.certificate.der))
            assert await client.protocol.next_frame() is None
            client.close()

        asyncio.run(run())
        assert bob.transport.session_count() == 0
        assert not bob.transport.can_reach(victim.hash.hex())

    def test_more_pending_handshakes_than_the_cap_are_refused(self, ip_node):
        bob = ip_node("bob")

        async def run():
            held = []
            for _ in range(MAX_PENDING_HANDSHAKES):
                client = await raw_connect(bob)
                client.protocol.open_stream()
                client.protocol.write(frames.hi_frame())
                await client.protocol.next_frame()
                held.append(client)
            assert bob.transport.pending_handshake_count() == \
                MAX_PENDING_HANDSHAKES
            over = await raw_connect(bob)
            over.protocol.open_stream()
            over.protocol.write(frames.hi_frame())
            assert await over.protocol.next_frame() is None
            for client in held + [over]:
                client.close()

        asyncio.run(run())


class TestAuthenticatedMisbehaviour:
    def test_a_message_claiming_another_sender_is_dropped(self, ip_node):
        """A session carries its own identity's messages and nobody else's."""
        bob = ip_node("bob")

        async def run():
            client = await raw_connect(bob)
            await raw_hello(client, bob)
            assert (await client.protocol.next_frame())[0] == frames.KIND_HELLO
            packed, signature = envelope_from(client, bob, source=b"\x09" * 16)
            client.protocol.write(frames.msg_frame(packed, signature))
            honest, honest_sig = envelope_from(client, bob, content="honest")
            client.protocol.write(frames.msg_frame(honest, honest_sig))
            assert (await client.protocol.next_frame())[0] == frames.KIND_ACK
            client.close()

        asyncio.run(run())
        assert [m.content for m in bob.inbox] == ["honest"]

    def test_a_message_with_a_bad_envelope_signature_is_dropped(self, ip_node):
        bob = ip_node("bob")

        async def run():
            client = await raw_connect(bob)
            await raw_hello(client, bob)
            assert (await client.protocol.next_frame())[0] == frames.KIND_HELLO
            packed, _signature = envelope_from(client, bob, content="forged")
            client.protocol.write(frames.msg_frame(packed, b"\x00" * 64))
            honest, honest_sig = envelope_from(client, bob, content="honest")
            client.protocol.write(frames.msg_frame(honest, honest_sig))
            assert (await client.protocol.next_frame())[0] == frames.KIND_ACK
            client.close()

        asyncio.run(run())
        assert [m.content for m in bob.inbox] == ["honest"]

    def test_a_message_addressed_to_someone_else_is_dropped(self, ip_node):
        bob = ip_node("bob")

        async def run():
            client = await raw_connect(bob)
            await raw_hello(client, bob)
            assert (await client.protocol.next_frame())[0] == frames.KIND_HELLO
            packed = frames.pack_envelope(
                src=client.identity.hash, dst=b"\x09" * 16,
                timestamp=time.time(), content="misdirected", fields={},
                protocol=True)
            client.protocol.write(frames.msg_frame(
                packed, client.identity.sign(frames.envelope_digest(packed))))
            honest, honest_sig = envelope_from(client, bob, content="honest")
            client.protocol.write(frames.msg_frame(honest, honest_sig))
            assert (await client.protocol.next_frame())[0] == frames.KIND_ACK
            client.close()

        asyncio.run(run())
        assert [m.content for m in bob.inbox] == ["honest"]


class TestFramesHeldForTheHandshake:
    """Everything arriving before the hello passes is queued and let through
    after it, which is only true if the session is registered with its
    transport before the queue is drained: a message that arrives in the same
    write as the hello has nowhere to go otherwise, and the sender learns it
    was lost only when its acknowledgement times out."""

    def test_a_message_written_with_the_hello_is_delivered(self, ip_node):
        bob = ip_node("bob")

        async def run():
            client = await raw_connect(bob)
            client.protocol.open_stream()
            client.protocol.write(frames.hi_frame())
            kind, payload = await client.protocol.next_frame()
            assert kind == frames.KIND_CHALLENGE
            stamp = int(time.time())
            digest = hello_digest(client.certificate.fingerprint,
                                  fingerprint_for(bob.transport.certificate_der),
                                  payload["nonce"], stamp)
            hello = frames.hello_frame(client.identity.get_public_key(), stamp,
                                       client.identity.sign(digest),
                                       certificate=client.certificate.der)
            packed, signature = envelope_from(client, bob, content="pipelined")

            client.protocol.write(hello + frames.msg_frame(packed, signature))

            assert (await client.protocol.next_frame())[0] == frames.KIND_HELLO
            assert (await client.protocol.next_frame())[0] == frames.KIND_ACK
            client.close()

        asyncio.run(run())
        assert [m.content for m in bob.inbox] == ["pipelined"]


# ---------------------------------------------------------------------------
# Sockets
# ---------------------------------------------------------------------------

class TestSocketOwnership:
    """A UDP port carries one node's sessions and nobody else's.

    SO_REUSEADDR on a UDP socket lets a second socket bind the same port, and
    the kernel then chooses which of them a datagram reaches. A second node on
    the host would take sessions meant for this one, which the caller cannot
    tell from an impostor; now that every session and every probe runs on the
    listening socket, that port is the whole of this node's direct path."""

    def test_a_bound_socket_keeps_its_port_to_itself(self):
        sock = bind_datagram_socket("127.0.0.1", 0)
        other = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            with pytest.raises(OSError):
                other.bind(("127.0.0.1", sock.getsockname()[1]))
        finally:
            other.close()
            sock.close()

    def test_a_port_that_is_taken_is_not_the_end_of_the_matter(self, ip_node):
        """One endpoint carries everything this node does over IP, so a node
        that has none holds no sessions at all, in or out. A configured port
        somebody else has is worth a port the kernel picks instead: the peer is
        told whichever port this node actually listens on."""
        first = ip_node("first")
        taken = first.transport.listen_port
        second = ip_node("second", listen_port=taken)

        assert second.transport.listen_port not in (0, taken)
        assert second.open_to(first)

    def test_a_named_host_binds_exactly_that_one_socket(self):
        sockets = bind_listen_sockets("127.0.0.1", 0)
        try:
            assert len(sockets) == 1
            assert sockets[0].family == socket.AF_INET
        finally:
            for sock in sockets:
                sock.close()


class TestAddressForms:
    """A v4-mapped address is this node's own socket talking about itself."""

    def test_a_mapped_address_is_unmapped_for_everybody_above(self):
        assert unmap_host("::ffff:203.0.113.7") == "203.0.113.7"
        assert unmap_host("203.0.113.7") == "203.0.113.7"
        assert unmap_host("fd00::1") == "fd00::1"
        assert unmap_host("not an address") == "not an address"

    def test_an_ipv4_address_goes_out_of_a_dual_stack_socket_mapped(self):
        assert socket_address("203.0.113.7", 42420, socket.AF_INET6) == \
            ("::ffff:203.0.113.7", 42420, 0, 0)
        assert socket_address("fd00::1", 42420, socket.AF_INET6) == \
            ("fd00::1", 42420, 0, 0)

    def test_an_ipv4_socket_has_no_form_for_an_ipv6_address(self):
        assert socket_address("fd00::1", 42420, socket.AF_INET) is None
        assert socket_address("203.0.113.7", 42420, socket.AF_INET) == \
            ("203.0.113.7", 42420)
        assert socket_address("peer.example.com", 1, socket.AF_INET) is None


# ---------------------------------------------------------------------------
# One socket for everything
# ---------------------------------------------------------------------------

def probe_once(target: tuple[str, int], nonce: bytes,
               timeout: float = 2.0) -> list[bytes]:
    """Send one probe from a socket of its own and read what comes back."""
    sock = bind_datagram_socket("127.0.0.1", 0)
    answers: list[bytes] = []
    try:
        sock.settimeout(timeout)
        sock.sendto(punch.probe_datagram(nonce), target)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, _source = sock.recvfrom(punch.DATAGRAM_BYTES)
            except (socket.timeout, TimeoutError):
                break
            answers.append(data)
            if len(answers) >= 2:
                break
    finally:
        sock.close()
    return answers


class TestOneSocketForEverything:
    """Dialling, accepting and probing all happen on the listening socket.

    A punched mapping forwards the socket that opened it and nothing else, so
    the port a node names in its candidates has to be the port it dials from,
    accepts on, and answers probes on. Anything else and the pair a NAT lets
    through is not the pair the session runs over.
    """

    def test_one_socket_carries_a_dialled_and_an_accepted_session_at_once(
            self, ip_node):
        alice, bob, carol = ip_node("alice"), ip_node("bob"), ip_node("carol")
        assert alice.open_to(bob), "alice could not dial bob"
        assert carol.open_to(alice), "carol could not dial alice"

        assert alice.transport.session_count() == 2
        alice.transport.send(bob.hash_hex, {}, "dialled")
        alice.transport.send(carol.hash_hex, {}, "accepted")
        assert wait_for(lambda: bob.inbox and carol.inbox, msg="both messages")
        assert bob.inbox[0].content == "dialled"
        assert carol.inbox[0].content == "accepted"

    def test_a_dialler_is_seen_at_the_port_it_listens_on(self, ip_node):
        """The observed address is only useful if it names the listening port.

        A peer behind a NAT offers what it was told about itself, and its
        router forwards that mapping to the socket that opened it. Naming an
        attempt's own socket would hand every peer an address that stops
        working the moment the attempt ends.
        """
        alice, bob = ip_node("alice"), ip_node("bob")
        assert alice.open_to(bob)

        assert wait_for(lambda: alice.observed, msg="the observed address")
        peer_hex, host, port = alice.observed[0]
        assert peer_hex == bob.hash_hex
        assert host == "127.0.0.1"
        assert port == alice.transport.listen_port

    def test_a_probe_is_answered_on_the_listening_socket_while_sessions_are_up(
            self, ip_node):
        alice, bob = ip_node("alice"), ip_node("bob")
        assert alice.open_to(bob)
        nonce = b"\x44" * 16
        channel = bob.transport.open_probe_channel(nonce)
        assert channel is not None
        try:
            answers = probe_once(("127.0.0.1", bob.transport.listen_port), nonce)
        finally:
            bob.transport.close_probe_channel(nonce)

        assert punch.ack_datagram(nonce) in answers, \
            "the listening socket never acknowledged the probe"
        assert punch.probe_datagram(nonce) in answers, \
            "the listening socket never probed back"
        alice.transport.send(bob.hash_hex, {}, "still up")
        assert wait_for(lambda: [m.content for m in bob.inbox] == ["still up"],
                        msg="the session after the probe")

    def test_a_probe_for_no_live_attempt_is_not_answered(self, ip_node):
        bob = ip_node("bob")
        assert probe_once(("127.0.0.1", bob.transport.listen_port),
                          b"\x45" * 16, timeout=1.0) == []

    def test_a_closed_channel_stops_answering(self, ip_node):
        bob = ip_node("bob")
        nonce = b"\x46" * 16
        assert bob.transport.open_probe_channel(nonce) is not None
        bob.transport.close_probe_channel(nonce)
        assert probe_once(("127.0.0.1", bob.transport.listen_port), nonce,
                          timeout=1.0) == []

