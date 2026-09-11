"""Phase 0 spike: a direct QUIC session between two peers, authenticated by RNS identity.

Run `demo` for the whole evidence set, or drive the roles by hand:

    python quic_session.py demo
    python quic_session.py mint   --peer-file /tmp/u/client.json
    python quic_session.py server --peer-file /tmp/u/server.json --port 4433 \
                                  --allow <client identity hash>
    python quic_session.py client --peer-file /tmp/u/client.json \
                                  --peer-card /tmp/u/server.card.json
    python quic_session.py relay  --peer-file /tmp/u/relay.json \
                                  --upstream /tmp/u/server.card.json --port 4434

A peer file holds this role's private material: an RNS identity and a self-signed
X.509 session certificate. A card is the public half a peer would send inside
`MT_UPGRADE_OFFER`: identity hash, 64-byte public key, certificate DER, address.

The session is pinned by certificate and proven by identity. The client passes the
server's certificate as the connection's only trust root and turns hostname checking
off, so only the holder of that certificate's private key can finish the handshake.
After the handshake the server issues a nonce and both sides sign
`own_cert_fingerprint || peer_cert_fingerprint || nonce || ts` with their RNS identity
key. See README.md in this directory for what that does and does not prove, and for
what aioquic exposes.
"""

import argparse
import asyncio
import base64
import datetime
import hashlib
import json
import os
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time

import aioquic
import msgpack
import RNS
from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.asyncio.server import serve
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.connection import QuicConnection
from aioquic.quic.events import (
    ConnectionTerminated,
    DatagramFrameReceived,
    HandshakeCompleted,
    StreamDataReceived,
)
from aioquic.tls import Context
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID

ALPN_PROTOCOL = "trenchchat-upgrade/0"
CERT_VALIDITY_DAYS = 30
CERT_COMMON_NAME = "trenchchat-session"
HELLO_MAX_SKEW_SECS = 60
NONCE_BYTES = 16
FRAME_LENGTH_BYTES = 4
MAX_FRAME_BYTES = 64 * 1024
MAX_DATA_BYTES = 16 * 1024 * 1024
MAX_STREAM_DATA_BYTES = 16 * 1024 * 1024
DATAGRAM_FRAME_BYTES = 1200
DATAGRAM_PAYLOAD_BYTES = 800
DATAGRAM_COUNT = 200
DATAGRAM_SETTLE_SECS = 2.0
BULK_BYTES = 50 * 1024 * 1024
BULK_CHUNK_BYTES = 256 * 1024
IDLE_TIMEOUT_SECS = 30.0
SUBPROCESS_START_TIMEOUT_SECS = 20.0


class HelloRejected(Exception):
    """A HELLO failed one of the identity, signature or freshness checks."""


def b64(raw: bytes) -> str:
    """Encode bytes for a JSON card or peer file."""
    return base64.b64encode(raw).decode()


def unb64(text: str) -> bytes:
    """Decode a base64 field from a JSON card or peer file."""
    return base64.b64decode(text)


def mint_certificate() -> tuple[ed25519.Ed25519PrivateKey, x509.Certificate]:
    """Mint a self-signed Ed25519 session certificate with no name a peer could rely on."""
    key = ed25519.Ed25519PrivateKey.generate()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CERT_COMMON_NAME)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=CERT_VALIDITY_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, None)
    )
    return key, cert


def cert_fingerprint(der: bytes) -> bytes:
    """The sha256 of a certificate's DER encoding, which is what a HELLO signs over."""
    return hashlib.sha256(der).digest()


def hello_digest(own_fp: bytes, peer_fp: bytes, nonce: bytes, timestamp: int) -> bytes:
    """The bytes an identity signs to bind itself to one session."""
    return own_fp + peer_fp + nonce + struct.pack("!Q", timestamp)


def identity_hash_for(public_key: bytes) -> bytes:
    """The RNS identity hash of a 64-byte public key, which is sha256 truncated to 16 bytes."""
    return hashlib.sha256(public_key).digest()[: RNS.Identity.TRUNCATED_HASHLENGTH // 8]


def verify_hello(payload: dict, own_fp: bytes, peer_fp: bytes, nonce: bytes,
                 expected_hash: bytes | None) -> bytes:
    """Check a HELLO's identity hash, signature and freshness; return the peer identity hash."""
    public_key = payload.get("pub", b"")
    signature = payload.get("sig", b"")
    timestamp = payload.get("ts", 0)
    if len(public_key) != RNS.Identity.KEYSIZE // 8:
        raise HelloRejected("public key is not 64 bytes")
    peer_hash = identity_hash_for(public_key)
    if expected_hash is not None and peer_hash != expected_hash:
        raise HelloRejected("identity hash does not match the expected peer")
    if abs(int(time.time()) - int(timestamp)) > HELLO_MAX_SKEW_SECS:
        raise HelloRejected("timestamp outside the 60 second window")
    identity = RNS.Identity(create_keys=False)
    identity.load_public_key(public_key)
    if not identity.validate(signature, hello_digest(peer_fp, own_fp, nonce, int(timestamp))):
        raise HelloRejected("signature does not verify")
    return peer_hash


def pack_frame(kind: str, payload: dict) -> bytes:
    """Length-prefix one msgpack control frame."""
    body = msgpack.packb({"kind": kind, "payload": payload}, use_bin_type=True)
    if len(body) > MAX_FRAME_BYTES:
        raise ValueError("control frame over the frame ceiling")
    return struct.pack("!I", len(body)) + body


class _StreamState:
    """One QUIC stream, read either as control frames or as raw bulk bytes."""

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.frames: asyncio.Queue = asyncio.Queue()
        self.raw: asyncio.Queue = asyncio.Queue()
        self.raw_mode = False
        self.ended = False

    def feed(self, data: bytes, end_stream: bool) -> None:
        """Take inbound stream bytes and route them to the frame or raw queue."""
        if self.raw_mode:
            if data:
                self.raw.put_nowait(data)
            if end_stream:
                self.ended = True
                self.raw.put_nowait(b"")
            return
        self.buffer += data
        while len(self.buffer) >= FRAME_LENGTH_BYTES:
            size = struct.unpack("!I", self.buffer[:FRAME_LENGTH_BYTES])[0]
            if size > MAX_FRAME_BYTES:
                raise ValueError("inbound frame over the frame ceiling")
            if len(self.buffer) < FRAME_LENGTH_BYTES + size:
                break
            body = bytes(self.buffer[FRAME_LENGTH_BYTES:FRAME_LENGTH_BYTES + size])
            del self.buffer[: FRAME_LENGTH_BYTES + size]
            self.frames.put_nowait(msgpack.unpackb(body, raw=False))
        if end_stream:
            self.ended = True
            self.frames.put_nowait(None)

    def switch_to_raw(self) -> None:
        """Stop parsing frames on this stream and hand everything already buffered to raw."""
        self.raw_mode = True
        if self.buffer:
            self.raw.put_nowait(bytes(self.buffer))
            self.buffer = bytearray()


class SessionProtocol(QuicConnectionProtocol):
    """A QUIC connection carrying length-prefixed control frames and raw bulk streams."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._streams: dict[int, _StreamState] = {}
        self.datagrams: asyncio.Queue = asyncio.Queue()
        self.datagrams_received = 0
        self.handshake_completed = asyncio.Event()
        self.handshake_at: float | None = None
        self.termination: ConnectionTerminated | None = None

    def stream(self, stream_id: int) -> _StreamState:
        """The state for one stream, created on first sight."""
        state = self._streams.get(stream_id)
        if state is None:
            state = _StreamState()
            self._streams[stream_id] = state
        return state

    def open_stream(self) -> int:
        """Allocate the next client-initiated bidirectional stream id."""
        stream_id = self._quic.get_next_available_stream_id(is_unidirectional=False)
        self.stream(stream_id)
        return stream_id

    def send_frame(self, stream_id: int, kind: str, payload: dict,
                   end_stream: bool = False) -> None:
        """Write one control frame to a stream and flush the connection."""
        self._quic.send_stream_data(stream_id, pack_frame(kind, payload), end_stream)
        self.transmit()

    def send_raw(self, stream_id: int, data: bytes, end_stream: bool = False) -> None:
        """Write raw bytes to a stream and flush the connection."""
        self._quic.send_stream_data(stream_id, data, end_stream)
        self.transmit()

    def send_datagram(self, data: bytes) -> None:
        """Send one unreliable datagram and flush the connection."""
        self._quic.send_datagram_frame(data)
        self.transmit()

    async def next_frame(self, stream_id: int) -> dict | None:
        """Await the next control frame on a stream, or None when the peer ended it."""
        return await self.stream(stream_id).frames.get()

    def quic_event_received(self, event) -> None:
        """Route QUIC events into the stream and datagram queues."""
        if isinstance(event, HandshakeCompleted):
            self.handshake_at = time.perf_counter()
            self.handshake_completed.set()
        elif isinstance(event, StreamDataReceived):
            self.stream(event.stream_id).feed(event.data, event.end_stream)
        elif isinstance(event, DatagramFrameReceived):
            self.datagrams_received += 1
            self.datagrams.put_nowait(event.data)
        elif isinstance(event, ConnectionTerminated):
            self.termination = event
            self.handshake_completed.set()


class ServerProtocol(SessionProtocol):
    """The listening side: issues the nonce, checks the peer's HELLO, then serves requests."""

    def __init__(self, *args, context: "ServerContext", **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._context = context
        self._nonce = os.urandom(NONCE_BYTES)
        self._peer_hash: bytes | None = None
        self._tasks: set[asyncio.Task] = set()
        self._seen: set[int] = set()

    def quic_event_received(self, event) -> None:
        """Dispatch every newly seen stream to a handler task."""
        super().quic_event_received(event)
        if isinstance(event, StreamDataReceived) and event.stream_id not in self._seen:
            self._seen.add(event.stream_id)
            task = asyncio.ensure_future(self._serve_stream(event.stream_id))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _serve_stream(self, stream_id: int) -> None:
        """Read one request frame from a stream and answer it."""
        try:
            frame = await self.next_frame(stream_id)
            if frame is None:
                return
            kind = frame.get("kind")
            if kind == "hi":
                await self._authenticate(stream_id)
            elif kind == "bulk":
                await self._serve_bulk(stream_id, frame.get("payload", {}))
            elif kind == "stats":
                self.send_frame(stream_id, "stats", {"datagrams": self.datagrams_received},
                                end_stream=True)
            else:
                self.send_frame(stream_id, "error", {"reason": "unknown request"},
                                end_stream=True)
        except Exception as exc:
            RNS.log(f"TrenchChat [spike]: stream {stream_id} failed: {exc}", RNS.LOG_ERROR)

    async def _authenticate(self, stream_id: int) -> None:
        """Issue the nonce, verify the peer's HELLO, answer with this node's HELLO."""
        self.send_frame(stream_id, "challenge", {"nonce": self._nonce})
        frame = await self.next_frame(stream_id)
        if frame is None or frame.get("kind") != "hello":
            self.close(reason_phrase="no hello")
            return
        payload = frame.get("payload", {})
        peer_cert = payload.get("cert", b"")
        try:
            peer_hash = verify_hello(payload, self._context.cert_fp, cert_fingerprint(peer_cert),
                                     self._nonce, None)
        except HelloRejected as exc:
            RNS.log(f"TrenchChat [spike]: rejecting HELLO: {exc}", RNS.LOG_WARNING)
            self.send_frame(stream_id, "error", {"reason": str(exc)}, end_stream=True)
            self.close(reason_phrase="hello rejected")
            self._context.record_rejection(str(exc))
            return
        if not self._context.is_eligible(peer_hash):
            RNS.log(f"TrenchChat [spike]: rejecting ineligible peer {peer_hash.hex()}",
                    RNS.LOG_WARNING)
            self.send_frame(stream_id, "error", {"reason": "ineligible identity"},
                            end_stream=True)
            self.close(reason_phrase="ineligible")
            self._context.record_rejection("ineligible identity")
            return
        self._peer_hash = peer_hash
        timestamp = int(time.time())
        digest = hello_digest(self._context.cert_fp, cert_fingerprint(peer_cert), self._nonce,
                              timestamp)
        self.send_frame(stream_id, "hello", {
            "pub": self._context.identity.get_public_key(),
            "ts": timestamp,
            "sig": self._context.identity.sign(digest),
        })
        RNS.log(f"TrenchChat [spike]: session authenticated with {peer_hash.hex()}",
                RNS.LOG_NOTICE)
        self._context.record_session(peer_hash)

    async def _serve_bulk(self, stream_id: int, payload: dict) -> None:
        """Write the requested number of bytes to a stream, then echo datagrams back."""
        if self._peer_hash is None:
            self.send_frame(stream_id, "error", {"reason": "not authenticated"}, end_stream=True)
            self.close(reason_phrase="not authenticated")
            return
        total = int(payload.get("bytes", 0))
        chunk = os.urandom(BULK_CHUNK_BYTES)
        sent = 0
        while sent < total:
            piece = chunk[: min(BULK_CHUNK_BYTES, total - sent)]
            self.send_raw(stream_id, piece, end_stream=sent + len(piece) >= total)
            sent += len(piece)
            await asyncio.sleep(0)


class ServerContext:
    """Everything the listening side knows: its identity, its certificate and who may connect."""

    def __init__(self, peer: dict, allowed: set[bytes], allow_any: bool,
                 report_path: str | None) -> None:
        self.identity = RNS.Identity(create_keys=False)
        self.identity.load_private_key(unb64(peer["identity_private"]))
        self.cert_der = unb64(peer["cert_der"])
        self.cert_fp = cert_fingerprint(self.cert_der)
        self.allowed = allowed
        self.allow_any = allow_any
        self.report_path = report_path
        self.sessions: list[str] = []
        self.rejections: list[str] = []

    def is_eligible(self, peer_hash: bytes) -> bool:
        """Stand-in for the real eligibility query over the stored members table."""
        return self.allow_any or peer_hash in self.allowed

    def record_session(self, peer_hash: bytes) -> None:
        """Note an authenticated peer and flush the report."""
        self.sessions.append(peer_hash.hex())
        self.flush()

    def record_rejection(self, reason: str) -> None:
        """Note a refused HELLO and flush the report."""
        self.rejections.append(reason)
        self.flush()

    def flush(self) -> None:
        """Write the running report so another process can read what this server saw."""
        if self.report_path is None:
            return
        with open(self.report_path, "w") as handle:
            json.dump({"sessions": self.sessions, "rejections": self.rejections}, handle)


def load_peer(path: str) -> dict:
    """Read a peer file, minting identity and certificate if it does not exist yet."""
    if os.path.exists(path):
        with open(path) as handle:
            return json.load(handle)
    identity = RNS.Identity()
    key, cert = mint_certificate()
    peer = {
        "identity_private": b64(identity.get_private_key()),
        "identity_hash": identity.hash.hex(),
        "public_key": b64(identity.get_public_key()),
        "cert_der": b64(cert.public_bytes(serialization.Encoding.DER)),
        "cert_key_pem": key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode(),
    }
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as handle:
        json.dump(peer, handle, indent=2)
    return peer


def write_card(peer: dict, path: str, host: str, port: int) -> dict:
    """Write the public half of a peer file: what would ride inside an upgrade offer."""
    card = {
        "identity_hash": peer["identity_hash"],
        "public_key": peer["public_key"],
        "cert_der": peer["cert_der"],
        "host": host,
        "port": port,
    }
    with open(path, "w") as handle:
        json.dump(card, handle, indent=2)
    return card


def load_card(path: str) -> dict:
    """Read a peer's card."""
    with open(path) as handle:
        return json.load(handle)


def pem_for(der: bytes) -> bytes:
    """Re-encode a DER certificate as PEM, which is the only form `cadata` accepts."""
    return x509.load_der_x509_certificate(der).public_bytes(serialization.Encoding.PEM)


def server_configuration(peer: dict) -> QuicConfiguration:
    """The listening side's QUIC configuration, presenting this node's session certificate."""
    config = QuicConfiguration(
        is_client=False,
        alpn_protocols=[ALPN_PROTOCOL],
        max_datagram_frame_size=DATAGRAM_FRAME_BYTES,
        max_data=MAX_DATA_BYTES,
        max_stream_data=MAX_STREAM_DATA_BYTES,
        idle_timeout=IDLE_TIMEOUT_SECS,
    )
    config.certificate = x509.load_der_x509_certificate(unb64(peer["cert_der"]))
    config.private_key = serialization.load_pem_private_key(
        peer["cert_key_pem"].encode(), password=None
    )
    return config


def client_configuration(peer: dict, pinned_cert_der: bytes) -> QuicConfiguration:
    """The connecting side's configuration: the peer's certificate as the only trust root."""
    config = QuicConfiguration(
        is_client=True,
        alpn_protocols=[ALPN_PROTOCOL],
        max_datagram_frame_size=DATAGRAM_FRAME_BYTES,
        max_data=MAX_DATA_BYTES,
        max_stream_data=MAX_STREAM_DATA_BYTES,
        idle_timeout=IDLE_TIMEOUT_SECS,
        verify_mode=ssl.CERT_REQUIRED,
    )
    config.cadata = pem_for(pinned_cert_der)
    config.server_name = None
    config.certificate = x509.load_der_x509_certificate(unb64(peer["cert_der"]))
    config.private_key = serialization.load_pem_private_key(
        peer["cert_key_pem"].encode(), password=None
    )
    return config


async def open_session(host: str, port: int, config: QuicConfiguration
                       ) -> tuple[asyncio.DatagramTransport, SessionProtocol]:
    """Open a client QUIC connection on a socket of the right address family.

    `aioquic.asyncio.connect` always binds an AF_INET6 dual-stack socket, which fails
    on a host with IPv6 disabled, so the spike builds the endpoint itself.
    """
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
    family, _, _, _, addr = infos[0]
    sock = socket.socket(family, socket.SOCK_DGRAM)
    sock.bind(("::", 0) if family == socket.AF_INET6 else ("0.0.0.0", 0))
    connection = QuicConnection(configuration=config)
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: SessionProtocol(connection), sock=sock
    )
    protocol.connect(addr)
    return transport, protocol


async def client_handshake(protocol: SessionProtocol, peer: dict, card: dict,
                           hello_out: str | None) -> tuple[int, bytes]:
    """Open the control stream, answer the server's challenge, check its HELLO in return."""
    identity = RNS.Identity(create_keys=False)
    identity.load_private_key(unb64(peer["identity_private"]))
    own_cert = unb64(peer["cert_der"])
    peer_cert_fp = cert_fingerprint(unb64(card["cert_der"]))
    stream_id = protocol.open_stream()
    protocol.send_frame(stream_id, "hi", {})
    frame = await protocol.next_frame(stream_id)
    if frame is None or frame.get("kind") != "challenge":
        raise HelloRejected("no challenge from the peer")
    nonce = frame["payload"]["nonce"]
    timestamp = int(time.time())
    digest = hello_digest(cert_fingerprint(own_cert), peer_cert_fp, nonce, timestamp)
    hello = {
        "pub": identity.get_public_key(),
        "cert": own_cert,
        "ts": timestamp,
        "sig": identity.sign(digest),
    }
    if hello_out is not None:
        with open(hello_out, "wb") as handle:
            handle.write(msgpack.packb(hello, use_bin_type=True))
    protocol.send_frame(stream_id, "hello", hello)
    frame = await protocol.next_frame(stream_id)
    if frame is None:
        raise HelloRejected("peer ended the control stream")
    if frame.get("kind") != "hello":
        raise HelloRejected(str(frame.get("payload", {}).get("reason", frame.get("kind"))))
    peer_hash = verify_hello(frame["payload"], cert_fingerprint(own_cert), peer_cert_fp, nonce,
                             bytes.fromhex(card["identity_hash"]))
    return stream_id, peer_hash


async def run_bulk(protocol: SessionProtocol, total: int) -> tuple[int, float]:
    """Pull `total` bytes over one bidirectional stream and return the bytes and seconds."""
    stream_id = protocol.open_stream()
    state = protocol.stream(stream_id)
    state.switch_to_raw()
    protocol.send_frame(stream_id, "bulk", {"bytes": total})
    started = time.perf_counter()
    received = 0
    while received < total:
        chunk = await state.raw.get()
        if chunk == b"":
            break
        received += len(chunk)
    return received, time.perf_counter() - started


async def run_datagrams(protocol: SessionProtocol, count: int) -> tuple[int, int]:
    """Send `count` unreliable datagrams, count the echoes, and ask the peer what it saw."""
    payload = os.urandom(DATAGRAM_PAYLOAD_BYTES - 4)
    for index in range(count):
        protocol.send_datagram(struct.pack("!I", index) + payload)
        await asyncio.sleep(0)
    echoed = 0
    deadline = time.perf_counter() + DATAGRAM_SETTLE_SECS
    while echoed < count and time.perf_counter() < deadline:
        try:
            await asyncio.wait_for(protocol.datagrams.get(), timeout=0.2)
            echoed += 1
        except asyncio.TimeoutError:
            pass
    stream_id = protocol.open_stream()
    protocol.send_frame(stream_id, "stats", {})
    frame = await asyncio.wait_for(protocol.next_frame(stream_id), timeout=5.0)
    seen = frame["payload"]["datagrams"] if frame else 0
    return echoed, seen


async def echo_datagrams(protocol: SessionProtocol, stop: asyncio.Event) -> None:
    """Echo every inbound datagram until told to stop; the server side of the datagram test."""
    while not stop.is_set():
        try:
            data = await asyncio.wait_for(protocol.datagrams.get(), timeout=0.2)
        except asyncio.TimeoutError:
            continue
        protocol.send_datagram(data)


def free_udp_port() -> int:
    """Pick a UDP port the kernel says is free."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def role_server(args: argparse.Namespace) -> int:
    """Listen for direct sessions, authenticate them, and serve bulk and datagram requests."""
    peer = load_peer(args.peer_file)
    port = args.port or free_udp_port()
    card_path = args.card or args.peer_file.replace(".json", ".card.json")
    write_card(peer, card_path, args.host, port)
    allowed = {bytes.fromhex(value) for value in args.allow}
    for path in args.allow_card:
        allowed.add(bytes.fromhex(load_card(path)["identity_hash"]))
    context = ServerContext(peer, allowed, args.allow_any, args.report)
    context.flush()
    protocols: list[SessionProtocol] = []

    def build(*pargs, **pkwargs) -> ServerProtocol:
        protocol = ServerProtocol(*pargs, context=context, **pkwargs)
        protocols.append(protocol)
        return protocol

    server = await serve(args.host, port, configuration=server_configuration(peer),
                         create_protocol=build)
    RNS.log(f"TrenchChat [spike]: server {peer['identity_hash']} listening on "
            f"{args.host}:{port}", RNS.LOG_NOTICE)
    print(f"listening {args.host}:{port}", flush=True)
    stop = asyncio.Event()
    echoes: dict[int, asyncio.Task] = {}
    try:
        while not stop.is_set():
            await asyncio.sleep(0.2)
            for protocol in list(protocols):
                if id(protocol) not in echoes:
                    echoes[id(protocol)] = asyncio.ensure_future(echo_datagrams(protocol, stop))
            if args.run_secs and time.time() > args.started + args.run_secs:
                stop.set()
    finally:
        stop.set()
        server.close()
    return 0


async def role_client(args: argparse.Namespace) -> int:
    """Open a direct session, prove identity both ways, then measure bulk and datagrams."""
    peer = load_peer(args.peer_file)
    card = load_card(args.peer_card)
    host = args.via_host or card["host"]
    port = args.via_port or card["port"]
    result: dict = {"role": "client", "target": f"{host}:{port}",
                    "pinned": card["identity_hash"]}
    started = time.perf_counter()
    transport = None
    try:
        config = client_configuration(peer, unb64(card["cert_der"]))
        transport, protocol = await open_session(host, port, config)
        await asyncio.wait_for(protocol.handshake_completed.wait(), timeout=args.timeout)
        if protocol.termination is not None:
            raise ConnectionError(f"{protocol.termination.reason_phrase or 'terminated'}")
        result["handshake_ms"] = round((protocol.handshake_at - started) * 1000, 2)
        _, peer_hash = await asyncio.wait_for(
            client_handshake(protocol, peer, card, args.hello_out), timeout=args.timeout
        )
        result["authenticated_peer"] = peer_hash.hex()
        result["session"] = "ok"
        if args.bulk_bytes:
            cpu_started = time.process_time()
            received, seconds = await asyncio.wait_for(run_bulk(protocol, args.bulk_bytes),
                                                       timeout=args.timeout)
            cpu = time.process_time() - cpu_started
            result["bulk_bytes"] = received
            result["bulk_secs"] = round(seconds, 3)
            result["bulk_mbytes_per_sec"] = round(received / seconds / (1024 * 1024), 2)
            result["bulk_mbits_per_sec"] = round(received * 8 / seconds / 1_000_000, 2)
            result["bulk_receiver_cpu_secs"] = round(cpu, 3)
        if args.datagrams:
            echoed, seen = await run_datagrams(protocol, args.datagrams)
            result["datagrams_sent"] = args.datagrams
            result["datagrams_seen_by_peer"] = seen
            result["datagrams_echoed_back"] = echoed
        protocol.close()
    except Exception as exc:
        result["session"] = "failed"
        result["error_type"] = type(exc).__name__
        result["error"] = str(exc)
    finally:
        if transport is not None:
            transport.close()
    if args.out:
        with open(args.out, "w") as handle:
            json.dump(result, handle, indent=2)
    print(json.dumps(result), flush=True)
    return 0 if result.get("session") == "ok" else 1


async def _forge_attempt(label: str, card: dict, peer: dict, hello: dict) -> dict:
    """Connect to the server with a pinned certificate and offer the given HELLO."""
    outcome = {"attempt": label, "accepted": None, "reason": ""}
    transport = None
    try:
        transport, protocol = await open_session(
            card["host"], card["port"], client_configuration(peer, unb64(card["cert_der"]))
        )
        await asyncio.wait_for(protocol.handshake_completed.wait(), timeout=10.0)
        if protocol.termination is not None:
            raise ConnectionError(protocol.termination.reason_phrase or "terminated")
        stream_id = protocol.open_stream()
        protocol.send_frame(stream_id, "hi", {})
        frame = await asyncio.wait_for(protocol.next_frame(stream_id), timeout=10.0)
        nonce = frame["payload"]["nonce"]
        if hello.get("resign_nonce"):
            identity = RNS.Identity(create_keys=False)
            identity.load_private_key(unb64(peer["identity_private"]))
            own_fp = cert_fingerprint(unb64(peer["cert_der"]))
            timestamp = int(time.time())
            hello = {
                "pub": hello["pub"],
                "cert": unb64(peer["cert_der"]),
                "ts": timestamp,
                "sig": identity.sign(hello_digest(own_fp, cert_fingerprint(unb64(card["cert_der"])),
                                                  nonce, timestamp)),
            }
        protocol.send_frame(stream_id, "hello", hello)
        reply = await asyncio.wait_for(protocol.next_frame(stream_id), timeout=10.0)
        if reply is not None and reply.get("kind") == "hello":
            outcome["accepted"] = True
            outcome["reason"] = "server answered with its own HELLO"
        else:
            outcome["accepted"] = False
            payload = (reply or {}).get("payload", {})
            outcome["reason"] = str(payload.get("reason", "control stream ended"))
        protocol.close()
    except Exception as exc:
        outcome["accepted"] = False
        outcome["reason"] = f"{type(exc).__name__}: {exc}"
    finally:
        if transport is not None:
            transport.close()
    return outcome


async def role_relay(args: argparse.Namespace) -> int:
    """Re-originate connections to the server, try to impersonate, then listen for a victim."""
    peer = load_peer(args.peer_file)
    upstream = load_card(args.upstream)
    port = args.port or free_udp_port()
    card_path = args.card or args.peer_file.replace(".json", ".card.json")
    write_card(peer, card_path, args.host, port)
    attempts = []

    with open(args.replay, "rb") as handle:
        captured = msgpack.unpackb(handle.read(), raw=False)
    attempts.append(await _forge_attempt("replay the captured client HELLO", upstream, peer,
                                         captured))
    forged = dict(captured)
    forged["resign_nonce"] = True
    attempts.append(await _forge_attempt("claim the client's public key, sign with the relay key",
                                         upstream, peer, forged))
    identity = RNS.Identity(create_keys=False)
    identity.load_private_key(unb64(peer["identity_private"]))
    honest = {"pub": identity.get_public_key(), "resign_nonce": True}
    attempts.append(await _forge_attempt("connect honestly as the relay's own identity",
                                         upstream, peer, honest))

    with open(args.report, "w") as handle:
        json.dump({"attempts": attempts}, handle, indent=2)

    context = ServerContext(peer, set(), True, None)
    inbound: list[SessionProtocol] = []

    def build(*pargs, **pkwargs) -> ServerProtocol:
        protocol = ServerProtocol(*pargs, context=context, **pkwargs)
        inbound.append(protocol)
        return protocol

    server = await serve(args.host, port, configuration=server_configuration(peer),
                         create_protocol=build)
    RNS.log(f"TrenchChat [spike]: relay listening on {args.host}:{port}", RNS.LOG_NOTICE)
    print(f"listening {args.host}:{port}", flush=True)
    try:
        await asyncio.sleep(args.run_secs)
    finally:
        server.close()
    return 0


def role_baseline(args: argparse.Namespace) -> int:
    """Measure the TCP plus TLS 1.3 fallback on the same loopback, for comparison with QUIC."""
    peer = load_peer(args.peer_file)
    cert_path = args.peer_file + ".cert.pem"
    key_path = args.peer_file + ".key.pem"
    with open(cert_path, "wb") as handle:
        handle.write(pem_for(unb64(peer["cert_der"])))
    with open(key_path, "w") as handle:
        handle.write(peer["cert_key_pem"])

    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert_path, key_path)
    client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_ctx.check_hostname = False
    client_ctx.load_verify_locations(cadata=pem_for(unb64(peer["cert_der"])).decode())

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    result: dict = {"role": "baseline", "bytes": args.bulk_bytes}

    def serve_once() -> None:
        raw, _ = listener.accept()
        with server_ctx.wrap_socket(raw, server_side=True) as stream:
            chunk = os.urandom(BULK_CHUNK_BYTES)
            sent = 0
            while sent < args.bulk_bytes:
                piece = chunk[: min(BULK_CHUNK_BYTES, args.bulk_bytes - sent)]
                stream.sendall(piece)
                sent += len(piece)

    thread = threading.Thread(target=serve_once, daemon=True)
    thread.start()
    started = time.perf_counter()
    with socket.create_connection(("127.0.0.1", port)) as raw:
        with client_ctx.wrap_socket(raw) as stream:
            result["handshake_ms"] = round((time.perf_counter() - started) * 1000, 2)
            result["tls_version"] = stream.version()
            transfer_started = time.perf_counter()
            cpu_started = time.process_time()
            received = 0
            while received < args.bulk_bytes:
                data = stream.recv(BULK_CHUNK_BYTES)
                if not data:
                    break
                received += len(data)
            seconds = time.perf_counter() - transfer_started
            cpu = time.process_time() - cpu_started
    thread.join(timeout=10)
    listener.close()
    result["bulk_bytes"] = received
    result["bulk_secs"] = round(seconds, 3)
    result["bulk_mbytes_per_sec"] = round(received / seconds / (1024 * 1024), 2)
    result["bulk_mbits_per_sec"] = round(received * 8 / seconds / 1_000_000, 2)
    result["bulk_process_cpu_secs"] = round(cpu, 3)
    if args.out:
        with open(args.out, "w") as handle:
            json.dump(result, handle, indent=2)
    print(json.dumps(result), flush=True)
    return 0


def role_mint(args: argparse.Namespace) -> int:
    """Create a peer file and its card without starting anything."""
    peer = load_peer(args.peer_file)
    card_path = args.card or args.peer_file.replace(".json", ".card.json")
    write_card(peer, card_path, args.host, args.port)
    print(json.dumps({"identity_hash": peer["identity_hash"], "card": card_path}), flush=True)
    return 0


def _spawn(argv: list[str], log_path: str) -> subprocess.Popen:
    """Start a spike role as a subprocess with its output captured."""
    handle = open(log_path, "w")
    return subprocess.Popen([sys.executable, os.path.abspath(__file__)] + argv,
                            stdout=handle, stderr=subprocess.STDOUT)


def _await_listening(log_path: str, process: subprocess.Popen) -> None:
    """Block until a spawned role prints its listening line."""
    deadline = time.time() + SUBPROCESS_START_TIMEOUT_SECS
    while time.time() < deadline:
        if os.path.exists(log_path):
            with open(log_path) as handle:
                if "listening " in handle.read():
                    return
        if process.poll() is not None:
            raise RuntimeError(f"role exited early, see {log_path}")
        time.sleep(0.2)
    raise RuntimeError(f"role never reported listening, see {log_path}")


def _run_client(argv: list[str], log_path: str) -> dict:
    """Run the client role to completion and return its result document."""
    with open(log_path, "w") as handle:
        subprocess.run([sys.executable, os.path.abspath(__file__)] + argv,
                       stdout=handle, stderr=subprocess.STDOUT, check=False)
    out = argv[argv.index("--out") + 1]
    with open(out) as handle:
        return json.load(handle)


def role_demo(args: argparse.Namespace) -> int:
    """Run every Phase 0 QUIC check end to end and print one report."""
    work = os.path.abspath(args.work_dir)
    os.makedirs(work, exist_ok=True)
    paths = {name: os.path.join(work, name) for name in (
        "server.json", "server.card.json", "client.json", "client.card.json", "relay.json",
        "relay.card.json", "client.hello", "server.report.json", "relay.report.json",
        "client.result.json", "relay.client.result.json", "server.log", "relay.log",
        "client.log", "relay.client.log",
    )}
    for path in paths.values():
        if os.path.exists(path):
            os.remove(path)

    client_peer = load_peer(paths["client.json"])
    server_port = free_udp_port()
    relay_port = free_udp_port()
    report: dict = {"aioquic": _aioquic_facts()}

    server = _spawn(["server", "--peer-file", paths["server.json"], "--card",
                     paths["server.card.json"], "--port", str(server_port), "--allow",
                     client_peer["identity_hash"], "--report", paths["server.report.json"],
                     "--run-secs", str(args.run_secs)], paths["server.log"])
    try:
        _await_listening(paths["server.log"], server)
        report["direct"] = _run_client(
            ["client", "--peer-file", paths["client.json"], "--peer-card",
             paths["server.card.json"], "--hello-out", paths["client.hello"], "--out",
             paths["client.result.json"], "--bulk-bytes", str(args.bulk_bytes), "--datagrams",
             str(args.datagrams)], paths["client.log"])

        relay = _spawn(["relay", "--peer-file", paths["relay.json"], "--card",
                        paths["relay.card.json"], "--upstream", paths["server.card.json"],
                        "--port", str(relay_port), "--replay", paths["client.hello"], "--report",
                        paths["relay.report.json"], "--run-secs", str(args.relay_secs)],
                       paths["relay.log"])
        try:
            _await_listening(paths["relay.log"], relay)
            report["relay_rejected_by_pinning"] = _run_client(
                ["client", "--peer-file", paths["client.json"], "--peer-card",
                 paths["server.card.json"], "--via", f"127.0.0.1:{relay_port}", "--out",
                 paths["relay.client.result.json"], "--bulk-bytes", "0", "--datagrams", "0",
                 "--timeout", "10"], paths["relay.client.log"])
            with open(paths["relay.report.json"]) as handle:
                report["relay_forgery_attempts"] = json.load(handle)["attempts"]
        finally:
            relay.terminate()
            relay.wait(timeout=10)
        with open(paths["server.report.json"]) as handle:
            report["server_seen"] = json.load(handle)
    finally:
        server.terminate()
        server.wait(timeout=10)

    print(json.dumps(report, indent=2), flush=True)
    return 0 if report["direct"].get("session") == "ok" else 1


def _aioquic_facts() -> dict:
    """Record which aioquic API the spike relied on, and which parts are underscore-private."""
    probe = Context(is_client=True)
    return {
        "version": aioquic.__version__,
        "client_certificate_request": "Context._request_client_certificate, private, commented "
                                      "'For test purposes only'",
        "peer_certificate": "QuicConnection.tls._peer_certificate, private",
        "keying_material_exporter": "absent",
        "cadata_encoding": "PEM only, via tls.load_pem_x509_certificates",
        "request_client_certificate_default": getattr(probe, "_request_client_certificate", None),
        "peer_certificate_is_public": hasattr(probe, "peer_certificate"),
        "exporter_is_public": hasattr(probe, "export_keying_material"),
    }


def build_parser() -> argparse.ArgumentParser:
    """Build the command line for every spike role."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subs = parser.add_subparsers(dest="role", required=True)

    mint = subs.add_parser("mint", help="create a peer file and its card")
    mint.add_argument("--peer-file", required=True)
    mint.add_argument("--card")
    mint.add_argument("--host", default="127.0.0.1")
    mint.add_argument("--port", type=int, default=0)

    server = subs.add_parser("server", help="listen for direct sessions")
    server.add_argument("--peer-file", required=True)
    server.add_argument("--card")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=0)
    server.add_argument("--allow", action="append", default=[],
                        help="identity hash allowed to open a session")
    server.add_argument("--allow-card", action="append", default=[])
    server.add_argument("--allow-any", action="store_true")
    server.add_argument("--report")
    server.add_argument("--run-secs", type=float, default=0.0)

    client = subs.add_parser("client", help="open a direct session and measure it")
    client.add_argument("--peer-file", required=True)
    client.add_argument("--peer-card", required=True)
    client.add_argument("--via", help="connect to this host:port instead of the card's address")
    client.add_argument("--hello-out", help="write this client's HELLO for a replay test")
    client.add_argument("--out")
    client.add_argument("--bulk-bytes", type=int, default=BULK_BYTES)
    client.add_argument("--datagrams", type=int, default=DATAGRAM_COUNT)
    client.add_argument("--timeout", type=float, default=120.0)

    relay = subs.add_parser("relay", help="impersonate the server and try to forge a HELLO")
    relay.add_argument("--peer-file", required=True)
    relay.add_argument("--card")
    relay.add_argument("--upstream", required=True)
    relay.add_argument("--replay", required=True)
    relay.add_argument("--report", required=True)
    relay.add_argument("--host", default="127.0.0.1")
    relay.add_argument("--port", type=int, default=0)
    relay.add_argument("--run-secs", type=float, default=60.0)

    baseline = subs.add_parser("baseline", help="measure the TCP plus TLS 1.3 fallback")
    baseline.add_argument("--peer-file", required=True)
    baseline.add_argument("--bulk-bytes", type=int, default=BULK_BYTES)
    baseline.add_argument("--out")

    demo = subs.add_parser("demo", help="run every check and print one report")
    demo.add_argument("--work-dir", default="/tmp/trenchchat-upgrade-spike")
    demo.add_argument("--bulk-bytes", type=int, default=BULK_BYTES)
    demo.add_argument("--datagrams", type=int, default=DATAGRAM_COUNT)
    demo.add_argument("--run-secs", type=float, default=300.0)
    demo.add_argument("--relay-secs", type=float, default=60.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse the command line and run the requested role."""
    args = build_parser().parse_args(argv)
    if args.role == "mint":
        return role_mint(args)
    if args.role == "demo":
        return role_demo(args)
    if args.role == "baseline":
        return role_baseline(args)
    if args.role == "client":
        if args.via:
            host, _, port = args.via.rpartition(":")
            args.via_host, args.via_port = host, int(port)
        else:
            args.via_host, args.via_port = None, None
        return asyncio.run(role_client(args))
    if args.role == "server":
        args.started = time.time()
        return asyncio.run(role_server(args))
    return asyncio.run(role_relay(args))


if __name__ == "__main__":
    sys.exit(main())
