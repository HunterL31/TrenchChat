"""
Voice frame plane over a direct session's unreliable datagrams.

The same exchange as the RNS plane and the same wire format: the peer with the
smaller identity hash sends VP_HELLO naming the channel, the other checks it
against membership and the voice permission and answers VP_ACCEPT, and only
then do frames flow, unreliably, losses concealed by the codec rather than
retransmitted. What changes is what carries them. There is no link to dial and
no identity to assert: the session is already up and its HELLO already proved
who is on the other end, so a peer is authorised before it has sent anything.

One frame per datagram, up to the path's packet budget. The mesh bundles two
frames into a 400-byte packet because a link MDU is 431 bytes and every packet
costs a header on shared airtime; a datagram on a punched UDP path costs
neither, and one frame per datagram is one frame's worth of loss when one goes
missing instead of two.

This module never touches Storage or core managers; authorisation is the
injected callback, exactly as network/voice_transport.py has it.
"""

import threading
import time

import RNS

from trenchchat.network.base import TransportLimits, direct_limits
from trenchchat.network.voice_transport import (
    PEER_CONNECTING, PEER_IDLE, PEER_STREAMING, PEER_UNREACHABLE,
    VOICE_DIAL_FALLBACK_SECS, VOICE_HELLO_MAX_ATTEMPTS, VOICE_HELLO_RETRY_SECS,
    VOICE_PACKET_RATE_LIMIT, VOICE_PACKET_RATE_WINDOW, VoiceTransportBase,
)
from trenchchat.network.voice_wire import (
    VOICE_WIRE_VERSION, VP_ACCEPT, VP_AUDIO, VP_BYE, VP_HELLO, pack_accept,
    pack_audio, pack_bye, pack_hello, packet_type, unpack_accept, unpack_audio,
    unpack_hello,
)

# Internal per-peer states. There is no dialing state: a session is up or it is
# not, and what is pending is only the hello exchange over it.
_IDLE = "idle"
_WAITING = "waiting"
_GREETING = "greeting"
_STREAMING = "streaming"


class _PeerVoice:
    """What this plane remembers about one peer for the life of a session."""

    def __init__(self, peer_hex: str):
        self.peer_hex = peer_hex
        self.state = _IDLE
        self.hello_sent_at = 0.0
        self.hello_attempts = 0
        self.waiting_until = 0.0
        self.packet_times: list[float] = []

    @property
    def exhausted(self) -> bool:
        """Whether this peer has been greeted as often as it is worth."""
        return self.hello_attempts >= VOICE_HELLO_MAX_ATTEMPTS


class IPVoiceTransport(VoiceTransportBase):
    """The voice frame plane carried by direct sessions."""

    def __init__(self, transport, identity, limits: TransportLimits | None = None):
        """
        transport: the IPTransport whose sessions carry the datagrams.
        identity: trenchchat.core.identity.Identity instance
        (passed in to avoid circular imports)
        limits: the path's budgets, for a test that wants narrower ones.
        """
        super().__init__()
        self._transport = transport
        self._identity = identity
        self._limits = limits or direct_limits()
        self._lock = threading.RLock()
        self._channel_hex: str | None = None
        self._peers: dict[str, _PeerVoice] = {}
        transport.set_datagram_callback(self._on_datagram)

    @property
    def packet_bytes(self) -> int:
        """The largest audio packet this path carries."""
        return self._limits.voice_packet_bytes

    @property
    def bitrate_bps(self) -> int:
        """What a pair on this path may encode at."""
        return self._limits.voice_bitrate_bps

    # --- session lifecycle ---

    def start(self, channel_hash_hex: str) -> None:
        """Enter a channel's voice session. Nothing is announced: a session
        already exists or there is nothing to say anything over."""
        with self._lock:
            self._channel_hex = channel_hash_hex

    def stop(self) -> None:
        """Leave, telling every peer that is streaming."""
        with self._lock:
            peers = list(self._peers)
            self._peers.clear()
            self._channel_hex = None
        for peer_hex in peers:
            self._send(peer_hex, pack_bye())

    # --- peer lifecycle ---

    def connect(self, peer_hex: str) -> None:
        """Greet a peer over the session, or wait for it to greet us.

        The smaller identity hash greets first, and the larger falls back to
        greeting after VOICE_DIAL_FALLBACK_SECS, which is the RNS plane's rule
        and covers a peer whose session came up later than ours.
        """
        now = time.time()
        greet = False
        with self._lock:
            if self._channel_hex is None:
                return
            if not self._transport.can_reach(peer_hex):
                self._peers.pop(peer_hex, None)
                return
            peer = self._peers.get(peer_hex)
            if peer is None:
                peer = _PeerVoice(peer_hex)
                self._peers[peer_hex] = peer
            if peer.state == _STREAMING:
                return
            if peer.state == _IDLE and self._identity.hash_hex >= peer_hex:
                peer.state = _WAITING
                peer.waiting_until = now + VOICE_DIAL_FALLBACK_SECS
                return
            if peer.state == _WAITING and now < peer.waiting_until:
                return
            if peer.state == _GREETING and \
                    now - peer.hello_sent_at < VOICE_HELLO_RETRY_SECS:
                return
            if peer.exhausted:
                return
            peer.state = _GREETING
            peer.hello_sent_at = now
            peer.hello_attempts += 1
            greet = True
        if greet:
            self._greet(peer_hex)

    def _greet(self, peer_hex: str) -> None:
        """Send one VP_HELLO naming the channel this node is in."""
        with self._lock:
            channel_hex = self._channel_hex
        if channel_hex is None:
            return
        self._send(peer_hex, pack_hello(bytes.fromhex(channel_hex)))

    def disconnect(self, peer_hex: str) -> None:
        """Stop streaming with one peer and tell it so."""
        with self._lock:
            existed = self._peers.pop(peer_hex, None) is not None
        if existed:
            self._send(peer_hex, pack_bye())
            self._notify_peer_state(peer_hex, PEER_IDLE)

    # --- frames ---

    def send_frames(self, seq: int, frames: list[bytes]) -> None:
        """Send a bundle as one datagram per frame, to every streaming peer.

        The pipeline bundles for the mesh's packet budget; here each frame goes
        on its own, numbered from the bundle's first sequence, which is how the
        receiver puts them back in order whichever path they took.
        """
        with self._lock:
            peers = [peer.peer_hex for peer in self._peers.values()
                     if peer.state == _STREAMING]
        if not peers:
            return
        for offset, frame in enumerate(frames):
            try:
                payload = pack_audio(seq + offset, [frame], self.packet_bytes)
            except ValueError as e:
                RNS.log(f"TrenchChat [voice]: refusing to send a frame: {e}",
                        RNS.LOG_WARNING)
                continue
            for peer_hex in peers:
                self._send(peer_hex, payload)

    def _send(self, peer_hex: str, payload: bytes) -> bool:
        """Put one voice packet on this peer's session."""
        return bool(self._transport.send_datagram(peer_hex, payload))

    # --- state ---

    def connected_peers(self) -> set[str]:
        """Every peer this plane is streaming with."""
        with self._lock:
            return {peer.peer_hex for peer in self._peers.values()
                    if peer.state == _STREAMING}

    def peer_state(self, peer_hex: str) -> str:
        """How this peer stands on this plane."""
        with self._lock:
            peer = self._peers.get(peer_hex)
            if peer is None:
                return PEER_IDLE
            if peer.state == _STREAMING:
                return PEER_STREAMING
            if peer.exhausted:
                return PEER_UNREACHABLE
            return PEER_CONNECTING

    def tick(self) -> None:
        """Repeat a hello that was not answered, and drop a session that went."""
        now = time.time()
        with self._lock:
            if self._channel_hex is None:
                return
            gone = [peer_hex for peer_hex in self._peers
                    if not self._transport.can_reach(peer_hex)]
            for peer_hex in gone:
                del self._peers[peer_hex]
            retry = [peer.peer_hex for peer in self._peers.values()
                     if peer.state in (_GREETING, _WAITING)
                     and not peer.exhausted
                     and now - peer.hello_sent_at >= VOICE_HELLO_RETRY_SECS
                     and now >= peer.waiting_until]
        for peer_hex in gone:
            self._notify_peer_state(peer_hex, PEER_IDLE)
        for peer_hex in retry:
            self.connect(peer_hex)

    # --- inbound ---

    def _on_datagram(self, peer_hex: str, payload: bytes) -> None:
        """One datagram off a session. Runs on the transport's loop.

        A frame is pushed from here, because a jitter buffer must not wait on
        a worker to get one. A greeting is handed off instead: authorising it
        is three database queries, and the loop carries every session this
        node holds.
        """
        try:
            kind = packet_type(payload)
        except ValueError:
            return
        if kind == VP_HELLO:
            self._transport.dispatch(self._handle_hello, peer_hex, payload)
            return
        with self._lock:
            peer = self._peers.get(peer_hex)
            if peer is None:
                return
            if not self._allow_packet(peer, time.time()):
                RNS.log(f"TrenchChat [voice]: rate-limited datagrams from "
                        f"{peer_hex[:12]}…", RNS.LOG_DEBUG)
                return
            state = peer.state
        if kind == VP_ACCEPT:
            self._handle_accept(peer_hex, payload)
        elif kind == VP_AUDIO:
            if state != _STREAMING:
                return
            try:
                seq, frames = unpack_audio(payload)
            except ValueError:
                return
            self._notify_frames(peer_hex, seq, frames)
        elif kind == VP_BYE:
            self.disconnect(peer_hex)

    def _allow_packet(self, peer: _PeerVoice, now: float) -> bool:
        """Caller holds the lock. The same per-peer ceiling the mesh plane has.

        A session authenticates its peer, so this is not about who is sending;
        it is about how much work one authenticated peer may make this node do,
        which no other layer bounds for a datagram.
        """
        times = peer.packet_times
        times[:] = [t for t in times if now - t < VOICE_PACKET_RATE_WINDOW]
        if len(times) >= VOICE_PACKET_RATE_LIMIT:
            return False
        times.append(now)
        return True

    def _handle_hello(self, peer_hex: str, payload: bytes) -> None:
        """Authorise a peer's greeting and answer it."""
        try:
            version, _codec, channel_hash = unpack_hello(payload)
        except ValueError:
            RNS.log(f"TrenchChat [voice]: malformed hello from {peer_hex[:12]}…",
                    RNS.LOG_WARNING)
            return
        if version != VOICE_WIRE_VERSION:
            RNS.log(f"TrenchChat [voice]: wire version {version} from "
                    f"{peer_hex[:12]}… is unsupported", RNS.LOG_WARNING)
            return
        channel_hex = channel_hash.hex()
        with self._lock:
            if self._channel_hex is None or channel_hex != self._channel_hex:
                return
            peer = self._peers.get(peer_hex)
            already = peer is not None and peer.state == _STREAMING
        if already:
            # Re-running the handshake on a live pair costs three database
            # queries for a packet the peer can repeat at will.
            return
        if not self._authorize(peer_hex, channel_hex):
            RNS.log(f"TrenchChat [voice]: refusing voice from {peer_hex[:12]}…: "
                    f"not authorised on {channel_hex[:12]}…", RNS.LOG_WARNING)
            return
        with self._lock:
            peer = self._peers.get(peer_hex)
            if peer is None:
                peer = _PeerVoice(peer_hex)
                self._peers[peer_hex] = peer
            peer.state = _STREAMING
            peer.hello_attempts = 0
        self._send(peer_hex, pack_accept())
        self._notify_peer_state(peer_hex, PEER_STREAMING)

    def _handle_accept(self, peer_hex: str, payload: bytes) -> None:
        """A peer took our greeting, so frames may flow."""
        try:
            unpack_accept(payload)
        except ValueError:
            return
        with self._lock:
            peer = self._peers.get(peer_hex)
            if peer is None or peer.state == _STREAMING:
                return
            peer.state = _STREAMING
            peer.hello_attempts = 0
        self._notify_peer_state(peer_hex, PEER_STREAMING)
