"""
One QUIC connection between two identities, and the HELLO that authenticates it.

The connecting side pins the peer's certificate as the connection's only trust
root and checks no hostname, so the TLS handshake alone proves the far end
holds that certificate's key. aioquic will not request or expose a client
certificate through public API, so the listener never sees one: it sends a
fresh nonce first, and both sides sign
``own fingerprint || peer fingerprint || nonce || timestamp`` with their
Reticulum identity key. The nonce never leaves the pinned pair, so a signature
over it proves the identity is live on this connection and cannot be replayed
onto another. The certificate a connecting node asserts in its HELLO is a
claim, useful only as the pin for a later connection the other way.

Not one application frame is read before that passes: everything arriving
early is queued, and the first frame that is not part of the handshake ends
the session. The UDP endpoint is owned by the caller rather than by aioquic,
whose own connect() binds a dual-stack IPv6 socket and fails on an IPv4-only
host, and because Phase 3 hands the session the socket a punch opened.
"""

import asyncio
import os
import socket
import ssl
import struct
import time
from dataclasses import dataclass
from typing import Callable

import RNS
from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.asyncio.server import QuicServer
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.connection import QuicConnection
from aioquic.quic.events import (
    ConnectionTerminated, DatagramFrameReceived, HandshakeCompleted,
    StreamDataReceived,
)
from cryptography import x509

from trenchchat.network.ip import frames
from trenchchat.network.ip.certificate import (
    SessionCertificate, fingerprint_for, pem_for,
)

ALPN_PROTOCOL = "trenchchat-session/1"

NONCE_BYTES = 16
HELLO_MAX_SKEW_SECS = 60
HANDSHAKE_TIMEOUT_SECS = 15.0

# An idle session costs a keepalive every few tens of seconds on the direct
# path and nothing on the mesh. aioquic sends none of its own, so the
# transport's sweep pings well inside the idle timeout.
IDLE_TIMEOUT_SECS = 60.0
KEEPALIVE_SECS = 20.0

# Voice frames ride as unreliable datagrams from Phase 4; the size is the one
# the plan's direct column names.
MAX_DATAGRAM_FRAME_BYTES = 1200

MAX_CONNECTION_DATA_BYTES = 64 * 1024 * 1024
MAX_STREAM_DATA_BYTES = 32 * 1024 * 1024

# The largest certificate a peer may assert, matching F_UPGRADE_CERT.
MAX_CERT_BYTES = 2 * 1024

# Frames held while a connection has not authenticated. Small on purpose: an
# unproven peer must not be able to make this node hold anything.
MAX_PREAUTH_FRAMES = 32

# Messages waiting for an ACK on one session.
MAX_PENDING_ACKS = 1024

IDENTITY_KEY_BYTES = RNS.Identity.KEYSIZE // 8


class HelloRejected(Exception):
    """A HELLO failed one of the identity, signature, freshness or gate checks."""


def identity_hash_for(public_key: bytes) -> bytes:
    """The Reticulum identity hash of a 64-byte public key."""
    identity = RNS.Identity(create_keys=False)
    identity.load_public_key(public_key)
    return identity.hash


def hello_digest(own_fingerprint: bytes, peer_fingerprint: bytes, nonce: bytes,
                 timestamp: int) -> bytes:
    """The bytes an identity signs to bind itself to one connection."""
    return own_fingerprint + peer_fingerprint + nonce + struct.pack("!Q", timestamp)


def verify_hello(payload: dict, own_fingerprint: bytes, peer_fingerprint: bytes,
                 nonce: bytes, expected_hash: bytes | None
                 ) -> tuple[bytes, bytes]:
    """Check a HELLO and return the peer's identity hash and public key.

    expected_hash is the identity the connecting side pinned; the listening
    side passes None and learns who called.
    """
    public_key = payload.get("pub")
    signature = payload.get("sig")
    timestamp = payload.get("ts")
    if not isinstance(public_key, bytes) or len(public_key) != IDENTITY_KEY_BYTES:
        raise HelloRejected("public key is not an identity key")
    if not isinstance(signature, bytes) or not signature:
        raise HelloRejected("hello carries no signature")
    if not isinstance(timestamp, int) or isinstance(timestamp, bool):
        raise HelloRejected("hello carries no timestamp")
    peer_hash = identity_hash_for(public_key)
    if expected_hash is not None and peer_hash != expected_hash:
        raise HelloRejected("identity hash does not match the expected peer")
    if abs(int(time.time()) - timestamp) > HELLO_MAX_SKEW_SECS:
        raise HelloRejected(
            f"timestamp outside the {HELLO_MAX_SKEW_SECS} second window")
    identity = RNS.Identity(create_keys=False)
    identity.load_public_key(public_key)
    digest = hello_digest(peer_fingerprint, own_fingerprint, nonce, timestamp)
    if not identity.validate(signature, digest):
        raise HelloRejected("signature does not verify")
    return peer_hash, public_key


def asserted_certificate(payload: dict) -> bytes:
    """The certificate a connecting node claims, bounded and parsed.

    Nothing at the TLS layer binds it to the endpoint, so it is only ever a
    claim; it is checked here so a stored claim is usable as a pin later.
    """
    der = payload.get("cert")
    if not isinstance(der, bytes) or not der:
        raise HelloRejected("hello carries no certificate")
    if len(der) > MAX_CERT_BYTES:
        raise HelloRejected(f"certificate is {len(der)} bytes, over {MAX_CERT_BYTES}")
    try:
        x509.load_der_x509_certificate(der)
    except Exception as e:
        raise HelloRejected(f"certificate does not parse: {e}") from e
    return der


def _base_configuration(is_client: bool) -> QuicConfiguration:
    return QuicConfiguration(
        is_client=is_client,
        alpn_protocols=[ALPN_PROTOCOL],
        max_datagram_frame_size=MAX_DATAGRAM_FRAME_BYTES,
        max_data=MAX_CONNECTION_DATA_BYTES,
        max_stream_data=MAX_STREAM_DATA_BYTES,
        idle_timeout=IDLE_TIMEOUT_SECS,
    )


def listener_configuration(certificate: SessionCertificate) -> QuicConfiguration:
    """The listening side's configuration, presenting this node's certificate."""
    configuration = _base_configuration(is_client=False)
    configuration.certificate = certificate.certificate
    configuration.private_key = certificate.private_key
    return configuration


def dialer_configuration(certificate: SessionCertificate,
                         peer_cert_der: bytes) -> QuicConfiguration:
    """The connecting side's configuration: the peer's certificate as sole root.

    Setting cadata stops aioquic loading any default trust store, and
    server_name = None skips hostname verification, which a certificate that
    names nothing could never pass.
    """
    configuration = _base_configuration(is_client=True)
    configuration.verify_mode = ssl.CERT_REQUIRED
    configuration.cadata = pem_for(peer_cert_der)
    configuration.server_name = None
    configuration.certificate = certificate.certificate
    configuration.private_key = certificate.private_key
    return configuration


@dataclass
class SessionHooks:
    """What a session tells its transport, all called on the transport's loop.

    dispatch is the one way off that loop: it hands a manager callback to the
    worker pool, so a handler that blocks never stalls the connection.
    """

    authorize: Callable[[str], bool]
    on_ready: Callable[["DirectSession"], None]
    on_message: Callable[["DirectSession", bytes, bytes], None]
    on_closed: Callable[["DirectSession", str], None]
    dispatch: Callable[..., None]
    on_datagram: Callable[["DirectSession", bytes], None] | None = None


@dataclass
class _Pending:
    """One message written and not yet acknowledged."""

    on_delivered: Callable[[str], None] | None
    on_failed: Callable[[str], None] | None
    sent_at: float


class DirectSession(QuicConnectionProtocol):
    """One authenticated QUIC connection to one peer."""

    def __init__(self, quic: QuicConnection, stream_handler=None, *,
                 identity, certificate: SessionCertificate, hooks: SessionHooks,
                 is_client: bool, expected_peer_hex: str = "",
                 peer_cert_der: bytes = b"", refuse: str = ""):
        """
        identity: trenchchat.core.identity.Identity instance
        (passed in to avoid circular imports)
        expected_peer_hex and peer_cert_der are set by the connecting side,
        which knows who it dialled; the listening side learns both from HELLO.
        refuse names the reason a session is over its caps, and is refused as
        soon as it has a socket to be refused on.
        """
        super().__init__(quic, stream_handler=stream_handler)
        self._identity = identity
        self._certificate = certificate
        self._hooks = hooks
        self._is_client = is_client
        self._expected_peer_hex = expected_peer_hex
        self._peer_cert_der = peer_cert_der
        self._refuse = refuse

        self.peer_hex = ""
        self.peer_public_key = b""
        self.opened_at = 0.0
        self.bytes_in = 0
        self.bytes_out = 0
        self.round_trip_secs: float | None = None

        self._authenticated = False
        self._closed_fired = False
        self._closed_reason = ""
        self._terminated = False
        self._nonce = b""
        self._control_stream_id: int | None = None
        self._decoders: dict[int, frames.FrameDecoder] = {}
        self._preauth: asyncio.Queue = asyncio.Queue()
        self._pending: dict[bytes, _Pending] = {}
        self._datagram_transport = None
        self._handshake_task: asyncio.Task | None = None
        self._verifier: RNS.Identity | None = None
        self._ready = asyncio.Event()
        # aioquic's own wait_connected() only records the handshake when a
        # waiter is already registered, so a connection that completes before
        # the handshake task runs never wakes it. This does not miss it.
        self._tls_done = asyncio.Event()

    # --- lifecycle ---

    def connection_made(self, transport) -> None:
        """Start the listening side's handshake as soon as there is a socket."""
        super().connection_made(transport)
        if self._refuse:
            self.fail(self._refuse)
            return
        if not self._is_client and self._handshake_task is None:
            self._handshake_task = asyncio.ensure_future(self._listen_handshake())

    def own_datagram_transport(self, transport) -> None:
        """Take ownership of a socket this session alone uses.

        A session the listener accepted shares the listening socket and owns
        nothing; one this node dialled closes its own when it ends.
        """
        self._datagram_transport = transport

    @property
    def authenticated(self) -> bool:
        """Whether the HELLO has passed and application frames may flow."""
        return self._authenticated

    @property
    def peer_certificate_der(self) -> bytes:
        """The certificate the peer asserted or this node pinned, if any."""
        return self._peer_cert_der

    @property
    def closed_reason(self) -> str:
        """Why the session ended, as far as this side knows."""
        return self._closed_reason

    async def dial(self, address) -> bool:
        """Connect, prove identity both ways, and return whether it held."""
        self.connect(address)
        self._handshake_task = asyncio.ensure_future(self._dial_handshake())
        try:
            await asyncio.wait_for(self._handshake_task,
                                   timeout=HANDSHAKE_TIMEOUT_SECS)
        except asyncio.TimeoutError:
            self.fail("handshake timed out")
        return self._authenticated

    def fail(self, reason: str) -> None:
        """End the session with a reason, logged once and never guessed at."""
        if self._closed_reason:
            return
        self._closed_reason = reason
        who = self.peer_hex[:12] or "an unproven peer"
        RNS.log(f"TrenchChat [ip]: closing the session with {who}…: {reason}",
                RNS.LOG_WARNING)
        try:
            self.close(reason_phrase=reason[:100])
        except Exception as e:
            RNS.log(f"TrenchChat [ip]: could not close a session: {e}", RNS.LOG_DEBUG)
        self._finish(reason)

    def shut_down(self, reason: str = "stopping") -> None:
        """Close a session this node is done with."""
        if not self._closed_reason:
            self._closed_reason = reason
        try:
            self.close(reason_phrase=reason[:100])
        except Exception as e:
            RNS.log(f"TrenchChat [ip]: could not close a session: {e}", RNS.LOG_DEBUG)
        self._finish(reason)

    def _finish(self, reason: str) -> None:
        """Fire the closed hook once, fail everything outstanding, drop the socket."""
        if self._closed_fired:
            return
        self._closed_fired = True
        self._closed_reason = self._closed_reason or reason
        self._ready.set()
        self._cancel_handshake()
        pending = list(self._pending.items())
        self._pending.clear()
        for _hash, entry in pending:
            if entry.on_failed is not None:
                self._hooks.dispatch(entry.on_failed, self.peer_hex)
        self._preauth.put_nowait(None)
        try:
            self._hooks.on_closed(self, reason)
        except Exception as e:
            RNS.log(f"TrenchChat [ip]: session closed hook error: {e}", RNS.LOG_ERROR)
        if self._datagram_transport is not None:
            try:
                self._datagram_transport.close()
            except Exception:
                pass
            self._datagram_transport = None

    def _cancel_handshake(self) -> None:
        """Stop a handshake that will never finish, rather than leave it pending."""
        task = self._handshake_task
        self._handshake_task = None
        if task is None or task.done():
            return
        try:
            if task is not asyncio.current_task():
                task.cancel()
        except RuntimeError:
            task.cancel()

    # --- QUIC events ---

    def quic_event_received(self, event) -> None:
        """Route QUIC's events into the frame reader and the session's state."""
        if isinstance(event, HandshakeCompleted):
            self.opened_at = time.time()
            self._tls_done.set()
        elif isinstance(event, StreamDataReceived):
            self._on_stream_data(event)
        elif isinstance(event, DatagramFrameReceived):
            self._on_datagram(event.data)
        elif isinstance(event, ConnectionTerminated):
            self._terminated = True
            self._tls_done.set()
            self._finish(event.reason_phrase or "connection terminated")

    def _on_stream_data(self, event: StreamDataReceived) -> None:
        self.bytes_in += len(event.data)
        if self._control_stream_id is None:
            self._control_stream_id = event.stream_id
        decoder = self._decoders.get(event.stream_id)
        if decoder is None:
            if not self._authenticated and event.stream_id != self._control_stream_id:
                self.fail("a second stream before the hello")
                return
            decoder = frames.FrameDecoder(
                frames.MAX_FRAME_BYTES if self._authenticated
                else frames.MAX_HANDSHAKE_FRAME_BYTES)
            self._decoders[event.stream_id] = decoder
        try:
            parsed = decoder.feed(event.data)
        except frames.FrameError as e:
            self.fail(str(e))
            return
        for kind, payload in parsed:
            if self._authenticated:
                self._dispatch(event.stream_id, kind, payload)
                continue
            if self._preauth.qsize() >= MAX_PREAUTH_FRAMES:
                self.fail("too many frames before the hello")
                return
            self._preauth.put_nowait((kind, payload))

    def _on_datagram(self, data: bytes) -> None:
        self.bytes_in += len(data)
        if not self._authenticated:
            RNS.log("TrenchChat [ip]: dropped a datagram before the hello",
                    RNS.LOG_DEBUG)
            return
        if self._hooks.on_datagram is not None:
            self._hooks.on_datagram(self, data)

    # --- handshake ---

    async def _next_handshake_frame(self) -> tuple[int, dict]:
        """The next frame, refusing anything that is not part of the handshake."""
        item = await asyncio.wait_for(self._preauth.get(),
                                      timeout=HANDSHAKE_TIMEOUT_SECS)
        if item is None:
            raise HelloRejected(self._closed_reason or "the peer went away")
        kind, payload = item
        if kind not in frames.HANDSHAKE_KINDS:
            raise HelloRejected("application frame before the hello")
        return kind, payload

    async def _dial_handshake(self) -> None:
        """Open the control stream, answer the nonce, check the peer's hello."""
        try:
            await asyncio.wait_for(self._tls_done.wait(),
                                   timeout=HANDSHAKE_TIMEOUT_SECS)
        except asyncio.TimeoutError:
            self.fail("the peer did not complete the TLS handshake")
            return
        if self._terminated:
            RNS.log(f"TrenchChat [ip]: the peer would not complete a pinned "
                    f"handshake: {self._closed_reason}", RNS.LOG_WARNING)
            return
        try:
            own_fingerprint = self._certificate.fingerprint
            peer_fingerprint = fingerprint_for(self._peer_cert_der)
            self._control_stream_id = self._quic.get_next_available_stream_id(
                is_unidirectional=False)
            self._decoders[self._control_stream_id] = frames.FrameDecoder()
            self._write(frames.hi_frame())
            kind, payload = await self._next_handshake_frame()
            if kind != frames.KIND_CHALLENGE:
                raise HelloRejected("the peer sent no nonce")
            nonce = payload.get("nonce")
            if not isinstance(nonce, bytes) or len(nonce) != NONCE_BYTES:
                raise HelloRejected("the peer's nonce is not 16 bytes")
            timestamp = int(time.time())
            signature = self._identity.rns_identity.sign(hello_digest(
                own_fingerprint, peer_fingerprint, nonce, timestamp))
            self._write(frames.hello_frame(
                self._identity.rns_identity.get_public_key(), timestamp,
                signature, certificate=self._certificate.der))
            kind, payload = await self._next_handshake_frame()
            if kind != frames.KIND_HELLO:
                raise HelloRejected("the peer answered no hello")
            expected = (bytes.fromhex(self._expected_peer_hex)
                        if self._expected_peer_hex else None)
            peer_hash, public_key = verify_hello(
                payload, own_fingerprint, peer_fingerprint, nonce, expected)
            self._authenticate(peer_hash.hex(), public_key)
        except (HelloRejected, frames.FrameError, asyncio.TimeoutError,
                ValueError) as e:
            self.fail(str(e) or type(e).__name__)

    async def _listen_handshake(self) -> None:
        """Issue the nonce, check the caller's hello, then answer with our own."""
        try:
            kind, _payload = await self._next_handshake_frame()
            if kind != frames.KIND_HI:
                raise HelloRejected("the control stream did not open with a hi")
            self._nonce = os.urandom(NONCE_BYTES)
            self._write(frames.challenge_frame(self._nonce))
            kind, payload = await self._next_handshake_frame()
            if kind != frames.KIND_HELLO:
                raise HelloRejected("the caller sent no hello")
            own_fingerprint = self._certificate.fingerprint
            asserted = asserted_certificate(payload)
            if self._peer_cert_der and asserted != self._peer_cert_der:
                raise HelloRejected("the caller asserted a certificate it did "
                                    "not offer")
            self._peer_cert_der = asserted
            peer_fingerprint = fingerprint_for(self._peer_cert_der)
            peer_hash, public_key = verify_hello(
                payload, own_fingerprint, peer_fingerprint, self._nonce, None)
            peer_hex = peer_hash.hex()
            if not self._hooks.authorize(peer_hex):
                raise HelloRejected("ineligible identity")
            timestamp = int(time.time())
            signature = self._identity.rns_identity.sign(hello_digest(
                own_fingerprint, peer_fingerprint, self._nonce, timestamp))
            self._write(frames.hello_frame(
                self._identity.rns_identity.get_public_key(), timestamp,
                signature))
            self._authenticate(peer_hex, public_key)
        except (HelloRejected, frames.FrameError, asyncio.TimeoutError,
                ValueError) as e:
            self.fail(str(e) or type(e).__name__)

    def _authenticate(self, peer_hex: str, public_key: bytes) -> None:
        """Mark the session proven, hand it up, and let everything held through.

        The transport is told the session is ready before the queue is drained:
        it registers the session's inbound queue there, and a message that
        arrived with the hello has nowhere to go until it has. Nothing awaits
        between the flag and the drain, so a frame that arrives during it cannot
        overtake one that arrived before.
        """
        self.peer_hex = peer_hex
        self.peer_public_key = public_key
        self._verifier = RNS.Identity(create_keys=False)
        self._verifier.load_public_key(public_key)
        self._authenticated = True
        if self.opened_at == 0.0:
            self.opened_at = time.time()
        for decoder in self._decoders.values():
            decoder.limit = frames.MAX_FRAME_BYTES
        self._ready.set()
        RNS.log(f"TrenchChat [ip]: session up with {peer_hex[:12]}…",
                RNS.LOG_NOTICE)
        try:
            self._hooks.on_ready(self)
        except Exception as e:
            RNS.log(f"TrenchChat [ip]: session ready hook error: {e}", RNS.LOG_ERROR)
        stream_id = self._control_stream_id or 0
        while not self._preauth.empty():
            item = self._preauth.get_nowait()
            if item is None:
                continue
            self._dispatch(stream_id, item[0], item[1])

    # --- frames ---

    def _dispatch(self, stream_id: int, kind: int, payload: dict) -> None:
        """One authenticated frame."""
        try:
            if kind == frames.KIND_MSG:
                envelope, signature = frames.read_msg(payload)
                self._hooks.on_message(self, envelope, signature)
            elif kind == frames.KIND_ACK:
                self._on_ack(frames.read_ack(payload))
            elif kind in (frames.KIND_REQ, frames.KIND_RESP):
                RNS.log(f"TrenchChat [ip]: ignoring a request frame on stream "
                        f"{stream_id} from {self.peer_hex[:12]}…", RNS.LOG_DEBUG)
            elif kind in frames.HANDSHAKE_KINDS:
                self.fail("a second hello on an authenticated session")
            else:
                RNS.log(f"TrenchChat [ip]: unknown frame {kind:#x} from "
                        f"{self.peer_hex[:12]}…", RNS.LOG_DEBUG)
        except frames.FrameError as e:
            self.fail(str(e))

    def _write(self, data: bytes, stream_id: int | None = None) -> None:
        """Write one frame to a stream and flush the connection."""
        target = self._control_stream_id if stream_id is None else stream_id
        if target is None:
            raise HelloRejected("no control stream to write to")
        self._quic.send_stream_data(target, data)
        self.bytes_out += len(data)
        self.transmit()

    def send_message(self, envelope: bytes, signature: bytes,
                     on_delivered=None, on_failed=None) -> bool:
        """Write one MSG and remember it until its ACK. False if it could not go."""
        if not self._authenticated or self._closed_fired:
            if on_failed is not None:
                self._hooks.dispatch(on_failed, self.peer_hex)
            return False
        try:
            frame = frames.msg_frame(envelope, signature)
        except frames.FrameError as e:
            RNS.log(f"TrenchChat [ip]: refusing to send an oversize message: {e}",
                    RNS.LOG_WARNING)
            if on_failed is not None:
                self._hooks.dispatch(on_failed, self.peer_hex)
            return False
        if len(self._pending) >= MAX_PENDING_ACKS:
            oldest = min(self._pending, key=lambda k: self._pending[k].sent_at)
            entry = self._pending.pop(oldest)
            if entry.on_failed is not None:
                self._hooks.dispatch(entry.on_failed, self.peer_hex)
        self._pending[frames.envelope_hash(envelope)] = _Pending(
            on_delivered=on_delivered, on_failed=on_failed, sent_at=time.time())
        try:
            self._write(frame)
        except Exception as e:
            self.fail(f"could not write a message: {e}")
            return False
        return True

    def verify(self, signature: bytes, data: bytes) -> bool:
        """Whether the identity that proved itself here signed these bytes."""
        if self._verifier is None or not isinstance(signature, bytes):
            return False
        try:
            return bool(self._verifier.validate(signature, data))
        except Exception:
            return False

    def keepalive(self) -> None:
        """Send one PING, so a session with nothing to say is not dropped as idle."""
        if not self._authenticated or self._closed_fired:
            return
        try:
            self._quic.send_ping(int(time.time() * 1000) & 0xFFFFFFFF)
            self.transmit()
        except Exception as e:
            RNS.log(f"TrenchChat [ip]: keepalive to {self.peer_hex[:12]}… "
                    f"failed: {e}", RNS.LOG_DEBUG)

    def send_ack(self, message_hash: bytes) -> None:
        """Acknowledge one envelope this node has taken."""
        if not self._authenticated or self._closed_fired:
            return
        try:
            self._write(frames.ack_frame(message_hash))
        except Exception as e:
            self.fail(f"could not write an acknowledgement: {e}")

    def send_datagram(self, payload: bytes) -> bool:
        """Send one unreliable datagram. False when the session cannot carry it."""
        if not self._authenticated or self._closed_fired:
            return False
        if len(payload) > MAX_DATAGRAM_FRAME_BYTES:
            return False
        try:
            self._quic.send_datagram_frame(payload)
            self.bytes_out += len(payload)
            self.transmit()
        except Exception as e:
            RNS.log(f"TrenchChat [ip]: datagram to {self.peer_hex[:12]}… "
                    f"failed: {e}", RNS.LOG_DEBUG)
            return False
        return True

    def _on_ack(self, message_hash: bytes) -> None:
        entry = self._pending.pop(message_hash, None)
        if entry is None:
            return
        self._note_round_trip(time.time() - entry.sent_at)
        if entry.on_delivered is not None:
            self._hooks.dispatch(entry.on_delivered, self.peer_hex)

    def _note_round_trip(self, seconds: float) -> None:
        """Smooth the acknowledged round trip, which is what the panel shows."""
        if self.round_trip_secs is None:
            self.round_trip_secs = seconds
        else:
            self.round_trip_secs = (self.round_trip_secs * 0.8) + (seconds * 0.2)

    def expire_pending(self, timeout_secs: float) -> int:
        """Fail anything unacknowledged for too long. Returns how many."""
        cutoff = time.time() - timeout_secs
        stale = [h for h, entry in self._pending.items() if entry.sent_at < cutoff]
        for message_hash in stale:
            entry = self._pending.pop(message_hash)
            RNS.log(f"TrenchChat [ip]: no acknowledgement from "
                    f"{self.peer_hex[:12]}… within {timeout_secs:.0f}s",
                    RNS.LOG_WARNING)
            if entry.on_failed is not None:
                self._hooks.dispatch(entry.on_failed, self.peer_hex)
        return len(stale)

    def pending_acks(self) -> int:
        """How many messages are still waiting to be acknowledged."""
        return len(self._pending)

    def stats(self) -> dict:
        """What this node knows about its own session, for the diagnostics panel."""
        return {
            "peer": self.peer_hex,
            "since": self.opened_at,
            "round_trip_secs": self.round_trip_secs,
            "bytes_in": self.bytes_in,
            "bytes_out": self.bytes_out,
            "pending_acks": len(self._pending),
        }


def bind_datagram_socket(host: str, port: int) -> socket.socket:
    """A UDP socket bound where the caller asked, in the family it resolves to.

    aioquic binds a dual-stack IPv6 socket of its own, which an IPv4-only host
    refuses outright; a session also has to be able to run on the socket a
    punch opened, so the socket is always this side's to make.

    The port is claimed exclusively, and SO_REUSEADDR is deliberately not set:
    on a UDP socket it lets a second socket bind the same port, after which the
    kernel decides which of them an arriving datagram reaches, and a second node
    on the host silently takes this one's sessions. UDP has no TIME_WAIT, so a
    port is free to bind again the moment it is closed either way.
    """
    info = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)[0]
    family, _type, _proto, _canonical, address = info
    sock = socket.socket(family, socket.SOCK_DGRAM)
    sock.bind(address)
    return sock


class ProbeAwareQuicServer(QuicServer):
    """A listener that hands a punch probe to its owner before QUIC sees it.

    A probe aimed at a mapped or observed candidate arrives here rather than at
    an attempt's own socket, and QUIC would drop it unread. The handler answers
    it on this socket, which is the one a router forwards and therefore the one
    whose mapping is worth punching.
    """

    def __init__(self, *args, probe_handler=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._probe_handler = probe_handler
        self._socket_transport = None

    def connection_made(self, transport) -> None:
        """Keep the socket, so a probe can be answered on it."""
        super().connection_made(transport)
        self._socket_transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        """Answer a probe here; hand everything else to QUIC unchanged."""
        if self._probe_handler is not None:
            try:
                if self._probe_handler(data, addr, self.send_to):
                    return
            except Exception as e:
                RNS.log(f"TrenchChat [ip]: probe handler error: {e}", RNS.LOG_ERROR)
        super().datagram_received(data, addr)

    def send_to(self, data: bytes, addr) -> None:
        """Write one datagram back out of the listening socket."""
        if self._socket_transport is not None:
            self._socket_transport.sendto(data, addr)


async def create_listener(sock: socket.socket, configuration: QuicConfiguration,
                          create_protocol, probe_handler=None
                          ) -> QuicServer:
    """Accept sessions on a socket this node owns.

    probe_handler(data, addr, send) -> bool sees every datagram first and says
    whether it took it; a punch probe is the only thing it ever takes.
    """
    loop = asyncio.get_running_loop()
    _transport, server = await loop.create_datagram_endpoint(
        lambda: ProbeAwareQuicServer(configuration=configuration,
                                     create_protocol=create_protocol,
                                     probe_handler=probe_handler),
        sock=sock,
    )
    return server


async def dial_session(host: str, port: int, configuration: QuicConfiguration,
                       create_protocol, sock: socket.socket | None = None
                       ) -> "DirectSession":
    """Open one outbound connection, on a given socket or on a fresh one."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
    family, _type, _proto, _canonical, address = infos[0]
    if sock is None:
        sock = socket.socket(family, socket.SOCK_DGRAM)
        sock.bind(("::", 0) if family == socket.AF_INET6 else ("0.0.0.0", 0))
    connection = QuicConnection(configuration=configuration)
    transport, session = await loop.create_datagram_endpoint(
        lambda: create_protocol(connection), sock=sock)
    session.own_datagram_transport(transport)
    await session.dial(address)
    return session
