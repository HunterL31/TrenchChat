"""
Frame plane for live voice: RNS Link lifecycle and voice packet transport.

One voice session at a time. Each participant pair shares one bidirectional
link: the peer with the lexicographically smaller identity hash dials, the
other waits (and falls back to dialing if nothing arrives in time, covering
one-way reachability). The initiator identifies itself on the link and sends
VP_HELLO; the responder authorizes via an injected callback and answers
VP_ACCEPT. Only then do audio frames flow, as fire-and-forget unreliable
packets; losses are concealed by the codec, never retransmitted.

This module never touches Storage or core managers; authorization is a
callback so the layering matches network/router.py.
"""

import threading
import time

import RNS

from trenchchat import APP_NAME, APP_ASPECT_VOICE
from trenchchat.network.voice_wire import (
    VP_ACCEPT, VP_AUDIO, VP_BYE, VP_HELLO, VOICE_WIRE_VERSION,
    pack_accept, pack_audio, pack_bye, pack_hello, packet_type,
    unpack_accept, unpack_audio, unpack_hello,
)

# Public per-peer states reported by peer_state().
PEER_IDLE = "idle"
PEER_CONNECTING = "connecting"
PEER_STREAMING = "streaming"
PEER_UNREACHABLE = "unreachable"

VOICE_LINK_AUTH_TIMEOUT_SECS = 10.0
VOICE_DIAL_FALLBACK_SECS = 10.0
VOICE_HELLO_RETRY_SECS = 1.0
VOICE_HELLO_MAX_ATTEMPTS = 5
VOICE_REDIAL_BACKOFF = (2.0, 5.0, 10.0, 30.0)
VOICE_ANNOUNCE_INTERVAL_SECS = 60.0

# How long an exhausted connection is kept before it is dropped entirely.
# Nothing else removes a _PeerConn: one stale voice_state from a peer that
# never becomes reachable otherwise buys a mesh-wide path request and a link
# attempt every VOICE_REDIAL_BACKOFF[-1] seconds for the rest of the session,
# and _conns grows for the life of it.
VOICE_CONN_GIVE_UP_SECS = 300.0

# Ceiling on inbound link packets from one peer per second. The design rate is
# 50 (25 audio + headroom); above that each packet still costs a parse, a lock
# and a jitter-buffer push, and a VP_HELLO costs three database queries.
VOICE_PACKET_RATE_LIMIT = 100
VOICE_PACKET_RATE_WINDOW = 1.0

# Pending inbound links held before the oldest is dropped. These exist before
# any authentication -- the peer has not identified yet -- so this is the only
# thing bounding them besides the auth timeout.
MAX_PENDING_INBOUND_LINKS = 32

# Internal connection states.
_IDLE = "idle"
_WAITING = "waiting"       # expecting the peer to dial us
_DIALING = "dialing"       # outbound link pending (or waiting for a path)
_LINKED = "linked"         # link up, handshake in progress
_STREAMING = "streaming"


class VoiceTransportBase:
    """Injectable transport interface; see RNSVoiceTransport for semantics."""

    def __init__(self):
        self._frame_cb = None
        self._peer_state_cb = None
        self._authorize_cb = None

    def set_frame_callback(self, cb) -> None:
        self._frame_cb = cb

    def set_peer_state_callback(self, cb) -> None:
        self._peer_state_cb = cb

    def set_authorize_callback(self, cb) -> None:
        self._authorize_cb = cb

    def start(self, channel_hash_hex: str) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def connect(self, peer_hex: str) -> None:
        raise NotImplementedError

    def disconnect(self, peer_hex: str) -> None:
        raise NotImplementedError

    def send_frames(self, seq: int, frames: list[bytes]) -> None:
        raise NotImplementedError

    def connected_peers(self) -> set[str]:
        raise NotImplementedError

    def peer_state(self, peer_hex: str) -> str:
        raise NotImplementedError

    def tick(self) -> None:
        raise NotImplementedError

    # shared helpers for subclasses

    def _authorize(self, peer_hex: str, channel_hash_hex: str) -> bool:
        if self._authorize_cb is None:
            return False
        try:
            return bool(self._authorize_cb(peer_hex, channel_hash_hex))
        except Exception as e:
            RNS.log(f"TrenchChat [voice]: authorize callback error: {e}",
                    RNS.LOG_ERROR)
            return False

    def _notify_frames(self, peer_hex: str, seq: int, frames: list[bytes]):
        if self._frame_cb is None:
            return
        try:
            self._frame_cb(peer_hex, seq, frames)
        except Exception as e:
            RNS.log(f"TrenchChat [voice]: frame callback error: {e}",
                    RNS.LOG_ERROR)

    def _notify_peer_state(self, peer_hex: str, state: str):
        if self._peer_state_cb is None:
            return
        try:
            self._peer_state_cb(peer_hex, state)
        except Exception as e:
            RNS.log(f"TrenchChat [voice]: peer state callback error: {e}",
                    RNS.LOG_ERROR)


class _PeerConn:
    """Bookkeeping for one peer's link across dial/handshake/redial cycles."""

    def __init__(self, peer_hex: str):
        self.peer_hex = peer_hex
        self.state = _IDLE
        self.link = None
        self.is_initiator = False
        self.dial_attempts = 0
        self.next_dial_at = 0.0
        self.waiting_deadline = 0.0
        self.hello_sent_at = 0.0
        self.hello_attempts = 0
        self.last_failure_at = 0.0
        # Inbound link-packet timestamps, for the per-peer rate limit.
        self.packet_times: list[float] = []

    @property
    def exhausted(self) -> bool:
        return self.dial_attempts >= len(VOICE_REDIAL_BACKOFF)


class RNSVoiceTransport(VoiceTransportBase):
    """Real RNS Link implementation of the voice frame plane."""

    def __init__(self, identity):
        super().__init__()
        self._identity = identity
        self._lock = threading.RLock()
        self._channel_hex: str | None = None
        self._conns: dict[str, _PeerConn] = {}
        self._by_link: dict[int, str] = {}
        self._pending_inbound: dict[int, tuple] = {}   # id(link) -> (link, ts)
        self._last_announce = 0.0

        self._dest = RNS.Destination(
            identity.rns_identity,
            RNS.Destination.IN,
            RNS.Destination.SINGLE,
            APP_NAME,
            APP_ASPECT_VOICE,
        )
        self._dest.set_link_established_callback(self._on_inbound_link)

    # --- session lifecycle ---

    def start(self, channel_hash_hex: str) -> None:
        with self._lock:
            self._channel_hex = channel_hash_hex
        self.announce()

    def stop(self) -> None:
        with self._lock:
            conns = list(self._conns.values())
            pending = [link for link, _ in self._pending_inbound.values()]
            self._conns.clear()
            self._by_link.clear()
            self._pending_inbound.clear()
            self._channel_hex = None
        for conn in conns:
            self._teardown_link(conn.link, polite=True)
        for link in pending:
            self._teardown_link(link, polite=False)

    def announce(self, attached_interface=None) -> None:
        try:
            self._dest.announce(attached_interface=attached_interface)
            self._last_announce = time.time()
        except Exception as e:
            RNS.log(f"TrenchChat [voice]: announce failed: {e}",
                    RNS.LOG_WARNING)

    # --- peer lifecycle ---

    def connect(self, peer_hex: str) -> None:
        with self._lock:
            if self._channel_hex is None:
                return
            conn = self._conns.get(peer_hex)
            if conn is None:
                conn = _PeerConn(peer_hex)
                self._conns[peer_hex] = conn
            if conn.state in (_DIALING, _LINKED, _STREAMING):
                return
            now = time.time()
            if conn.state == _WAITING:
                if now < conn.waiting_deadline:
                    return
            elif self._identity.hash_hex >= peer_hex and conn.dial_attempts == 0:
                # The smaller hash dials; we wait, with a fallback so one-way
                # reachability still converges.
                conn.state = _WAITING
                conn.waiting_deadline = now + VOICE_DIAL_FALLBACK_SECS
                return
            if now < conn.next_dial_at:
                return
        self._dial(peer_hex)

    def disconnect(self, peer_hex: str) -> None:
        with self._lock:
            conn = self._conns.pop(peer_hex, None)
            if conn is None:
                return
            if conn.link is not None:
                self._by_link.pop(id(conn.link), None)
        self._teardown_link(conn.link, polite=True)
        self._notify_peer_state(peer_hex, PEER_IDLE)

    def send_frames(self, seq: int, frames: list[bytes]) -> None:
        with self._lock:
            links = [conn.link for conn in self._conns.values()
                     if conn.state == _STREAMING and conn.link is not None]
        if not links:
            return
        payload = pack_audio(seq, frames)
        for link in links:
            try:
                RNS.Packet(link, payload, create_receipt=False).send()
            except Exception as e:
                RNS.log(f"TrenchChat [voice]: frame send failed: {e}",
                        RNS.LOG_DEBUG)

    def connected_peers(self) -> set[str]:
        with self._lock:
            return {conn.peer_hex for conn in self._conns.values()
                    if conn.state == _STREAMING}

    def peer_state(self, peer_hex: str) -> str:
        with self._lock:
            conn = self._conns.get(peer_hex)
            if conn is None:
                return PEER_IDLE
            if conn.state == _STREAMING:
                return PEER_STREAMING
            if conn.exhausted:
                return PEER_UNREACHABLE
            return PEER_CONNECTING

    # --- housekeeping ---

    def tick(self) -> None:
        now = time.time()
        dial_needed: list[str] = []
        hello_needed: list[_PeerConn] = []
        expired_inbound: list = []

        with self._lock:
            if self._channel_hex is None:
                return
            for conn in list(self._conns.values()):
                if conn.state == _WAITING and now >= conn.waiting_deadline:
                    conn.state = _IDLE
                if (conn.exhausted and conn.state == _IDLE
                        and now - conn.last_failure_at >= VOICE_CONN_GIVE_UP_SECS):
                    # Every re-dial is a mesh-wide path request, so a peer we
                    # have never reached is dropped rather than retried for
                    # the rest of the session.
                    RNS.log(
                        f"TrenchChat [voice]: giving up on {conn.peer_hex[:12]}… "
                        f"after {conn.dial_attempts} dial attempts",
                        RNS.LOG_DEBUG,
                    )
                    del self._conns[conn.peer_hex]
                    continue
                if conn.state == _IDLE and now >= conn.next_dial_at:
                    dial_needed.append(conn.peer_hex)
                if conn.state == _LINKED and conn.is_initiator:
                    if conn.hello_attempts >= VOICE_HELLO_MAX_ATTEMPTS:
                        expired_inbound.append(conn.link)
                        self._register_link_failure(conn)
                    elif now - conn.hello_sent_at >= VOICE_HELLO_RETRY_SECS:
                        hello_needed.append(conn)
            for key, (link, accepted_at) in list(self._pending_inbound.items()):
                if now - accepted_at > VOICE_LINK_AUTH_TIMEOUT_SECS:
                    del self._pending_inbound[key]
                    expired_inbound.append(link)

        for peer_hex in dial_needed:
            self._dial(peer_hex)
        for conn in hello_needed:
            self._send_hello(conn)
        for link in expired_inbound:
            self._teardown_link(link, polite=False)

        if now - self._last_announce >= VOICE_ANNOUNCE_INTERVAL_SECS:
            self.announce()

    # --- outbound dialing ---

    def _dial(self, peer_hex: str) -> None:
        with self._lock:
            conn = self._conns.get(peer_hex)
            if conn is None or conn.state in (_DIALING, _LINKED, _STREAMING):
                return
            conn.state = _DIALING
            conn.is_initiator = True

        try:
            delivery_hash = RNS.Destination.hash(
                bytes.fromhex(peer_hex), "lxmf", "delivery")
            peer_identity = RNS.Identity.recall(delivery_hash)
            if peer_identity is None:
                RNS.Transport.request_path(delivery_hash)
                self._defer_dial(peer_hex)
                return
            dest = RNS.Destination(
                peer_identity,
                RNS.Destination.OUT,
                RNS.Destination.SINGLE,
                APP_NAME,
                APP_ASPECT_VOICE,
            )
            if not RNS.Transport.has_path(dest.hash):
                RNS.Transport.request_path(dest.hash)
                self._defer_dial(peer_hex)
                return
            link = RNS.Link(
                dest,
                established_callback=self._on_outbound_established,
                closed_callback=self._on_link_closed,
            )
            link.set_packet_callback(self._on_link_packet)
            with self._lock:
                conn = self._conns.get(peer_hex)
                if conn is None:
                    self._teardown_link(link, polite=False)
                    return
                conn.link = link
                self._by_link[id(link)] = peer_hex
        except Exception as e:
            RNS.log(f"TrenchChat [voice]: dial to {peer_hex[:12]}… "
                    f"failed: {e}", RNS.LOG_WARNING)
            self._defer_dial(peer_hex)

    def _defer_dial(self, peer_hex: str) -> None:
        with self._lock:
            conn = self._conns.get(peer_hex)
            if conn is None:
                return
            self._register_link_failure(conn)
        self._notify_peer_state(peer_hex, self.peer_state(peer_hex))

    def _register_link_failure(self, conn: _PeerConn) -> None:
        """Caller holds the lock. Schedules the next dial with backoff."""
        if conn.link is not None:
            self._by_link.pop(id(conn.link), None)
            conn.link = None
        conn.state = _IDLE
        conn.last_failure_at = time.time()
        backoff = VOICE_REDIAL_BACKOFF[
            min(conn.dial_attempts, len(VOICE_REDIAL_BACKOFF) - 1)]
        conn.dial_attempts += 1
        conn.next_dial_at = time.time() + backoff
        conn.hello_attempts = 0

    def _on_outbound_established(self, link) -> None:
        with self._lock:
            peer_hex = self._by_link.get(id(link))
            conn = self._conns.get(peer_hex) if peer_hex else None
            if conn is None:
                self._teardown_link(link, polite=False)
                return
            conn.state = _LINKED
        try:
            link.identify(self._identity.rns_identity)
        except Exception as e:
            RNS.log(f"TrenchChat [voice]: link identify failed: {e}",
                    RNS.LOG_WARNING)
        self._send_hello(conn)

    def _send_hello(self, conn: _PeerConn) -> None:
        with self._lock:
            channel_hex = self._channel_hex
            link = conn.link
            if channel_hex is None or link is None or conn.state != _LINKED:
                return
            conn.hello_sent_at = time.time()
            conn.hello_attempts += 1
        try:
            payload = pack_hello(bytes.fromhex(channel_hex))
            RNS.Packet(link, payload, create_receipt=False).send()
        except Exception as e:
            RNS.log(f"TrenchChat [voice]: hello send failed: {e}",
                    RNS.LOG_WARNING)

    # --- inbound links ---

    def _on_inbound_link(self, link) -> None:
        link.set_packet_callback(self._on_link_packet)
        link.set_link_closed_callback(self._on_link_closed)
        evicted = None
        accepted = False
        with self._lock:
            if self._channel_hex is not None:
                # These are held before the peer has identified, so nothing
                # here knows who they are yet and the auth timeout is the only
                # other bound. Drop the oldest rather than grow.
                if len(self._pending_inbound) >= MAX_PENDING_INBOUND_LINKS:
                    oldest = min(self._pending_inbound,
                                 key=lambda k: self._pending_inbound[k][1])
                    evicted, _ = self._pending_inbound.pop(oldest)
                self._pending_inbound[id(link)] = (link, time.time())
                accepted = True
        if evicted is not None:
            self._teardown_link(evicted, polite=False)
        if not accepted:
            self._teardown_link(link, polite=False)

    def _handle_hello(self, link, payload: bytes) -> None:
        try:
            version, codec_id, channel_hash = unpack_hello(payload)
        except ValueError:
            self._drop_inbound(link, "malformed hello")
            return
        if version != VOICE_WIRE_VERSION:
            self._drop_inbound(link, f"wire version {version} unsupported")
            return

        remote_identity = link.get_remote_identity()
        if remote_identity is None:
            # HELLO raced ahead of the identify packet; the initiator
            # retries, so simply wait for the next one.
            return
        peer_hex = remote_identity.hash.hex()
        channel_hex = channel_hash.hex()

        if not self._authorize(peer_hex, channel_hex):
            self._drop_inbound(
                link, f"unauthorized peer {peer_hex[:12]}…")
            return

        drop = None
        with self._lock:
            self._pending_inbound.pop(id(link), None)
            conn = self._conns.get(peer_hex)
            if conn is None:
                conn = _PeerConn(peer_hex)
                self._conns[peer_hex] = conn

            # The canonical link between a pair is the one initiated by the
            # smaller identity hash. If we are the canonical initiator and
            # have a live attempt of our own, refuse this duplicate, both
            # sides then deterministically converge on our link.
            own_attempt_live = conn.link is not None and conn.link is not link \
                and conn.state in (_DIALING, _LINKED, _STREAMING)
            if own_attempt_live and self._identity.hash_hex < peer_hex:
                accepted = False
            else:
                if conn.link is not None and conn.link is not link:
                    self._by_link.pop(id(conn.link), None)
                    drop = conn.link
                conn.link = link
                self._by_link[id(link)] = peer_hex
                conn.state = _STREAMING
                conn.is_initiator = False
                conn.dial_attempts = 0
                conn.hello_attempts = 0
                accepted = True

        if not accepted:
            self._teardown_link(link, polite=False)
            return
        if drop is not None:
            self._teardown_link(drop, polite=False)
        try:
            RNS.Packet(link, pack_accept(), create_receipt=False).send()
        except Exception as e:
            RNS.log(f"TrenchChat [voice]: accept send failed: {e}",
                    RNS.LOG_WARNING)
        self._notify_peer_state(peer_hex, PEER_STREAMING)

    def _drop_inbound(self, link, reason: str) -> None:
        RNS.log(f"TrenchChat [voice]: refusing link — {reason}",
                RNS.LOG_WARNING)
        with self._lock:
            self._pending_inbound.pop(id(link), None)
        self._teardown_link(link, polite=False)

    # --- packet dispatch ---

    def _allow_packet(self, conn: "_PeerConn", now: float) -> bool:
        """Caller holds the lock. Per-peer ceiling on inbound link packets.

        The frame plane bypasses LXMF, so the router's control throttle never
        sees it. Nothing else paces a peer that sends faster than the design
        rate, and each packet costs a parse, this lock, and a buffer push.
        """
        times = conn.packet_times
        times[:] = [t for t in times if now - t < VOICE_PACKET_RATE_WINDOW]
        if len(times) >= VOICE_PACKET_RATE_LIMIT:
            return False
        times.append(now)
        return True

    def _on_link_packet(self, data, packet) -> None:
        link = packet.link
        try:
            ptype = packet_type(data)
        except ValueError:
            return

        if ptype == VP_HELLO:
            with self._lock:
                established = self._by_link.get(id(link))
                conn = self._conns.get(established) if established else None
                # Re-running the handshake on a live link re-authorises the
                # peer -- three database queries -- and replies, for a packet
                # they can repeat at will.
                if conn is not None and conn.state == _STREAMING:
                    return
            self._handle_hello(link, data)
            return

        with self._lock:
            peer_hex = self._by_link.get(id(link))
            conn = self._conns.get(peer_hex) if peer_hex else None
            if conn is not None and not self._allow_packet(conn, time.time()):
                conn = None
                RNS.log(
                    f"TrenchChat [voice]: rate-limited link packets from "
                    f"{peer_hex[:12]}…",
                    RNS.LOG_DEBUG,
                )

        if conn is None:
            return

        if ptype == VP_ACCEPT:
            try:
                unpack_accept(data)
            except ValueError:
                return
            with self._lock:
                if conn.state == _LINKED:
                    conn.state = _STREAMING
                    conn.dial_attempts = 0
            self._notify_peer_state(conn.peer_hex, PEER_STREAMING)
        elif ptype == VP_AUDIO:
            if conn.state != _STREAMING:
                return
            try:
                seq, frames = unpack_audio(data)
            except ValueError:
                return
            self._notify_frames(conn.peer_hex, seq, frames)
        elif ptype == VP_BYE:
            self._handle_link_gone(link)

    # --- link close / failure ---

    def _on_link_closed(self, link) -> None:
        self._handle_link_gone(link)

    def _handle_link_gone(self, link) -> None:
        with self._lock:
            self._pending_inbound.pop(id(link), None)
            peer_hex = self._by_link.pop(id(link), None)
            conn = self._conns.get(peer_hex) if peer_hex else None
            if conn is None or conn.link is not link:
                return
            self._register_link_failure(conn)
        self._notify_peer_state(peer_hex, self.peer_state(peer_hex))

    def _teardown_link(self, link, *, polite: bool) -> None:
        if link is None:
            return
        try:
            if polite:
                try:
                    RNS.Packet(link, pack_bye(), create_receipt=False).send()
                except Exception:
                    pass
            link.teardown()
        except Exception as e:
            RNS.log(f"TrenchChat [voice]: link teardown error: {e}",
                    RNS.LOG_DEBUG)
