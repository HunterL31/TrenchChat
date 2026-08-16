"""
In-process voice transport for tests.

Mirrors the semantics of RNSVoiceTransport without any RNS Links: a shared
FakeVoiceRegistry connects the transports of all peers in a test, connect()
runs the target's authorize callback (so core enforcement is exercised
exactly as a real inbound handshake would), and frames are delivered on a
short-lived thread after a small delay, matching real link timing closely
enough for the eventual-consistency helpers.
"""

import threading
import time

from trenchchat.network.voice_transport import (
    PEER_CONNECTING, PEER_IDLE, PEER_STREAMING, PEER_UNREACHABLE,
    VoiceTransportBase,
)

FAKE_DIAL_FALLBACK_SECS = 1.0
FAKE_GIVE_UP_ATTEMPTS = 4
FAKE_DELIVERY_DELAY = 0.02


class FakeVoiceRegistry:
    """Shared lookup table connecting the fake transports in one test."""

    def __init__(self):
        self.transports: dict[str, "FakeVoiceTransport"] = {}
        self.dial_log: list[tuple[str, str]] = []
        self.lock = threading.RLock()

    def register(self, transport: "FakeVoiceTransport"):
        with self.lock:
            self.transports[transport.self_hex] = transport


class FakeVoiceTransport(VoiceTransportBase):
    def __init__(self, self_hex: str, registry: FakeVoiceRegistry, *,
                 delivery_delay: float = FAKE_DELIVERY_DELAY,
                 drop_every_n: int = 0,
                 fail_connect_to: set[str] | None = None):
        super().__init__()
        self.self_hex = self_hex
        self.registry = registry
        self._delay = delivery_delay
        self._drop_every_n = drop_every_n
        self.fail_connect_to: set[str] = set(fail_connect_to or ())
        self._channel: str | None = None
        self._streams: set[str] = set()
        self._attempts: dict[str, int] = {}
        self._waiting_since: dict[str, float] = {}
        self._tx_counter = 0
        self._threads: list[threading.Thread] = []
        registry.register(self)

    # --- session lifecycle ---

    def start(self, channel_hash_hex: str) -> None:
        self._channel = channel_hash_hex

    def stop(self) -> None:
        for peer_hex in list(self._streams):
            self.disconnect(peer_hex)
        self._channel = None
        self._attempts.clear()
        self._waiting_since.clear()
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._threads = [t for t in self._threads if t.is_alive()]

    # --- peer lifecycle ---

    def connect(self, peer_hex: str) -> None:
        if self._channel is None or peer_hex in self._streams:
            return
        now = time.time()
        if self.self_hex >= peer_hex and self._attempts.get(peer_hex, 0) == 0:
            since = self._waiting_since.setdefault(peer_hex, now)
            if now - since < FAKE_DIAL_FALLBACK_SECS:
                return
        self._dial(peer_hex)

    def _dial(self, peer_hex: str) -> None:
        with self.registry.lock:
            self.registry.dial_log.append((self.self_hex, peer_hex))
        self._attempts[peer_hex] = self._attempts.get(peer_hex, 0) + 1

        if peer_hex in self.fail_connect_to:
            return
        target = self.registry.transports.get(peer_hex)
        if target is None or target._channel != self._channel:
            return
        if self.self_hex in target.fail_connect_to:
            return
        # The responder authorizes the dialer, exactly like a real HELLO.
        if not target._authorize(self.self_hex, self._channel):
            return

        self._streams.add(peer_hex)
        target._streams.add(self.self_hex)
        self._attempts.pop(peer_hex, None)
        self._waiting_since.pop(peer_hex, None)
        target._attempts.pop(self.self_hex, None)
        target._waiting_since.pop(self.self_hex, None)
        self._notify_peer_state(peer_hex, PEER_STREAMING)
        target._notify_peer_state(self.self_hex, PEER_STREAMING)

    def disconnect(self, peer_hex: str) -> None:
        self._streams.discard(peer_hex)
        self._attempts.pop(peer_hex, None)
        self._waiting_since.pop(peer_hex, None)
        target = self.registry.transports.get(peer_hex)
        if target is not None and self.self_hex in target._streams:
            target._streams.discard(self.self_hex)
            target._notify_peer_state(self.self_hex, PEER_IDLE)
        self._notify_peer_state(peer_hex, PEER_IDLE)

    def simulate_drop(self, peer_hex: str) -> None:
        """Model a link failure: both ends lose the stream, redial allowed."""
        self._streams.discard(peer_hex)
        target = self.registry.transports.get(peer_hex)
        if target is not None:
            target._streams.discard(self.self_hex)
            target._notify_peer_state(self.self_hex, PEER_CONNECTING)
        self._notify_peer_state(peer_hex, PEER_CONNECTING)

    # --- frames ---

    def send_frames(self, seq: int, frames: list[bytes]) -> None:
        self._tx_counter += 1
        if self._drop_every_n and self._tx_counter % self._drop_every_n == 0:
            return
        for peer_hex in list(self._streams):
            target = self.registry.transports.get(peer_hex)
            if target is None:
                continue

            def _deliver(t=target, s=seq, f=list(frames)):
                time.sleep(self._delay)
                if self.self_hex in t._streams:
                    t._notify_frames(self.self_hex, s, f)

            thread = threading.Thread(target=_deliver, daemon=True)
            self._threads.append(thread)
            thread.start()

    # --- state ---

    def connected_peers(self) -> set[str]:
        return set(self._streams)

    def peer_state(self, peer_hex: str) -> str:
        if peer_hex in self._streams:
            return PEER_STREAMING
        if self._attempts.get(peer_hex, 0) >= FAKE_GIVE_UP_ATTEMPTS:
            return PEER_UNREACHABLE
        if peer_hex in self._attempts or peer_hex in self._waiting_since:
            return PEER_CONNECTING
        return PEER_IDLE

    def tick(self) -> None:
        pass
