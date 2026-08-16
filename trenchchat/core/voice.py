"""
Live group voice sessions.

Every channel implicitly has one voice room. Signalling rides LXMF control
messages (join/leave/state) and is deliberately low-rate; audio frames never
touch LXMF — they flow over RNS Links managed by a VoiceTransport
(trenchchat/network/voice_transport.py) injected at construction.

Every signalling message asserts state about the sender only; nobody relays
third-party presence. A joiner learns the current occupants because each
participant, on receiving the join, unicasts one voice_state describing
itself. The roster is therefore an eventually-consistent presence hint;
established and identified links are the ground truth for who is heard.

Callbacks fire on background threads. GUI consumers must marshal into Qt
via signals and gate the join control on has_permission(channel, self,
VOICE_CHAT).
"""

import threading
import time

import RNS
import LXMF

from trenchchat.core.actions import compute_channel_recipients
from trenchchat.core.identity import Identity
from trenchchat.core.permissions import (
    VOICE_CHAT, is_open_join, permissions_from_json,
)
from trenchchat.core.protocol import (
    F_CHANNEL_HASH, F_MSG_TYPE, F_TIMESTAMP,
    F_VOICE_CODEC, F_VOICE_JOINED_AT, F_VOICE_MUTED, F_VOICE_STATE,
    MT_VOICE_JOIN, MT_VOICE_LEAVE, MT_VOICE_STATE,
)
from trenchchat.core.storage import Storage
from trenchchat.core.subscription import SubscriptionManager
from trenchchat.network.router import Router
from trenchchat.network.voice_wire import (
    SEQ_MODULUS, VOICE_FRAME_MS, seq_distance,
)

VOICE_STATE_REFRESH_SECS = 60.0
VOICE_ROSTER_TTL_SECS = 180.0
VOICE_SIGNAL_MAX_AGE_SECS = 120.0
VOICE_STATE_MIN_INTERVAL_SECS = 2.0
MAX_VOICE_PARTICIPANTS = 8
SPEAKING_HOLD_SECS = 0.3
VOICE_CODEC_OPUS = "opus"

STATE_JOINED = "joined"
STATE_LEFT = "left"

# Session callback states.
SESSION_JOINED = "joined"
SESSION_LEFT = "left"
SESSION_AUDIO_ERROR = "audio_error"

# Roster link_state values, in rough order of goodness.
LINK_SELF = "self"
LINK_STREAMING = "streaming"
LINK_CONNECTING = "connecting"
LINK_UNREACHABLE = "unreachable"
LINK_SIGNALLED = "signalled"


class VoiceManager:
    """Voice session lifecycle, signalling, and per-channel rosters."""

    def __init__(self, identity: Identity, storage: Storage, router: Router,
                 subscription_mgr: SubscriptionManager, config=None,
                 transport=None, audio_factory=None,
                 state_refresh_secs: float = VOICE_STATE_REFRESH_SECS,
                 roster_ttl_secs: float = VOICE_ROSTER_TTL_SECS):
        self._identity = identity
        self._storage = storage
        self._router = router
        self._subscription_mgr = subscription_mgr
        self._config = config
        self._transport = transport
        self._audio_factory = audio_factory
        self._state_refresh_secs = state_refresh_secs
        self._roster_ttl_secs = roster_ttl_secs

        # channel_hash_hex -> peer_hex -> {muted, joined_at, last_heard, codec}
        self._rosters: dict[str, dict[str, dict]] = {}
        self._lock = threading.RLock()

        self._session_channel: str | None = None
        self._joined_at = 0.0
        self._muted = False
        self._last_state_sent = 0.0
        self._state_dirty = False
        self._audio_pipeline = None
        self._audio_error = ""

        self._tx_packets = 0
        self._rx_frames: dict[str, int] = {}
        self._rx_quality: dict[str, dict] = {}
        self._last_frame_at: dict[str, float] = {}
        self._speaking: dict[str, bool] = {}

        self._roster_callbacks: list = []
        self._speaking_callbacks: list = []
        self._session_callbacks: list = []

        if self._transport is not None:
            self._transport.set_frame_callback(self._on_frames)
            self._transport.set_peer_state_callback(self._on_peer_link_state)
            self._transport.set_authorize_callback(self._authorize_link)

        router.add_delivery_callback(self._on_lxmf_message)

    # --- public session API ---

    @property
    def current_channel(self) -> str | None:
        return self._session_channel

    @property
    def is_muted(self) -> bool:
        return self._muted

    @property
    def audio_pipeline(self):
        """The active audio pipeline, or None (diagnostics / dev harness)."""
        return self._audio_pipeline

    def join_voice(self, channel_hash_hex: str) -> bool:
        """Enter a channel's voice session.

        Returns False if already in a session, the channel is unknown, the
        caller lacks voice_chat on a non-open-join channel, or the session
        is full. Link dialing and audio start are asynchronous.
        """
        if self._session_channel is not None:
            return False
        if not self._may_voice_self(channel_hash_hex):
            return False
        now = time.time()
        with self._lock:
            roster = self._live_roster(channel_hash_hex, now)
            if len(roster) >= MAX_VOICE_PARTICIPANTS:
                RNS.log(
                    f"TrenchChat [voice]: session for "
                    f"{channel_hash_hex[:12]}… is full",
                    RNS.LOG_WARNING,
                )
                return False
            self._session_channel = channel_hash_hex
            self._joined_at = now
            self._upsert_entry(channel_hash_hex, self._identity.hash_hex,
                               muted=self._muted, joined_at=now, now=now)
            peers = [p for p in roster if p != self._identity.hash_hex]

        if self._transport is not None:
            self._transport.start(channel_hash_hex)
        self._start_audio()
        self._broadcast(MT_VOICE_JOIN, channel_hash_hex)
        self._last_state_sent = now
        if self._transport is not None:
            for peer_hex in peers:
                self._transport.connect(peer_hex)

        self._notify_roster(channel_hash_hex)
        self._notify_session(SESSION_JOINED)
        return True

    def leave_voice(self) -> None:
        """Leave the current voice session; a no-op when not in one."""
        channel_hash_hex = self._session_channel
        if channel_hash_hex is None:
            return
        self._broadcast(MT_VOICE_LEAVE, channel_hash_hex)
        self._stop_audio()
        if self._transport is not None:
            self._transport.stop()
        with self._lock:
            self._session_channel = None
            roster = self._rosters.get(channel_hash_hex, {})
            roster.pop(self._identity.hash_hex, None)
            self._rx_frames.clear()
            self._rx_quality.clear()
            self._last_frame_at.clear()
            self._speaking.clear()
        self._notify_roster(channel_hash_hex)
        self._notify_session(SESSION_LEFT)

    def set_muted(self, muted: bool) -> None:
        """Set the local mute state and advertise it (coalesced)."""
        if muted == self._muted:
            return
        self._muted = muted
        channel_hash_hex = self._session_channel
        if channel_hash_hex is None:
            return
        with self._lock:
            entry = self._rosters.get(channel_hash_hex, {}).get(
                self._identity.hash_hex)
            if entry is not None:
                entry["muted"] = muted
        if self._audio_pipeline is not None:
            try:
                self._audio_pipeline.set_muted(muted)
            except Exception as e:
                RNS.log(f"TrenchChat [voice]: pipeline mute error: {e}",
                        RNS.LOG_ERROR)
        now = time.time()
        if now - self._last_state_sent >= VOICE_STATE_MIN_INTERVAL_SECS:
            self._broadcast(MT_VOICE_STATE, channel_hash_hex)
            self._last_state_sent = now
        else:
            self._state_dirty = True
        self._notify_roster(channel_hash_hex)

    # --- roster read model ---

    def get_roster(self, channel_hash_hex: str) -> list[dict]:
        """Current voice occupants of a channel, freshest signal first."""
        now = time.time()
        with self._lock:
            entries = [
                (peer_hex, dict(entry))
                for peer_hex, entry in
                self._rosters.get(channel_hash_hex, {}).items()
            ]
        result = []
        for peer_hex, entry in entries:
            result.append({
                "identity_hash": peer_hex,
                "muted": entry["muted"],
                "joined_at": entry["joined_at"],
                "link_state": self._link_state_for(channel_hash_hex,
                                                   peer_hex, now),
                "speaking": self._speaking.get(peer_hex, False),
            })
        result.sort(key=lambda r: r["joined_at"])
        return result

    # --- event callbacks ---

    def add_roster_callback(self, cb) -> None:
        self._roster_callbacks.append(cb)

    def add_speaking_callback(self, cb) -> None:
        self._speaking_callbacks.append(cb)

    def add_session_callback(self, cb) -> None:
        self._session_callbacks.append(cb)

    # --- housekeeping / diagnostics ---

    def tick(self) -> None:
        """Periodic housekeeping; call roughly once per second."""
        now = time.time()
        channel_hash_hex = self._session_channel

        if channel_hash_hex is not None:
            due = self._state_dirty and \
                now - self._last_state_sent >= VOICE_STATE_MIN_INTERVAL_SECS
            if due or now - self._last_state_sent >= self._state_refresh_secs:
                self._broadcast(MT_VOICE_STATE, channel_hash_hex)
                self._last_state_sent = now
                self._state_dirty = False

            if self._transport is not None:
                self._transport.tick()
                self._redial_and_reauthorize(channel_hash_hex, now)

        self._prune_rosters(now)
        self._update_speaking(now)

    def frame_stats(self) -> dict:
        """Transmit/receive counters plus per-sender receive quality.

        rx_quality per peer: received/lost/late frame counts, loss_pct, and
        smoothed inter-arrival jitter in ms (RFC 3550-style, using frame
        sequence numbers as the send clock). This is the backend signal for
        a per-peer connection-quality indicator.
        """
        with self._lock:
            quality = {}
            for peer_hex, q in self._rx_quality.items():
                total = q["received"] + q["lost"]
                quality[peer_hex] = {
                    "received": q["received"],
                    "lost": q["lost"],
                    "late": q["late"],
                    "jitter_ms": round(q["jitter_ms"], 2),
                    "loss_pct": round(100.0 * q["lost"] / total, 2)
                    if total else 0.0,
                }
            return {
                "tx_packets": self._tx_packets,
                "rx_frames": dict(self._rx_frames),
                "rx_quality": quality,
            }

    def audio_status(self) -> dict:
        if self._audio_pipeline is not None:
            return {"available": True, "reason": ""}
        return {"available": False,
                "reason": self._audio_error or "no audio pipeline"}

    # --- permission enforcement ---

    def _may_voice_self(self, channel_hash_hex: str) -> bool:
        channel = self._storage.get_channel(channel_hash_hex)
        if channel is None:
            return False
        perms = permissions_from_json(channel["permissions"])
        if is_open_join(perms):
            return True
        return self._storage.has_permission(
            channel_hash_hex, self._identity.hash_hex, VOICE_CHAT)

    def _peer_may_voice(self, channel_hash_hex: str, sender_hex: str) -> bool:
        """Core inbound enforcement: may this peer participate in voice?

        Unknown channels fail closed. Open-join channels have no member
        table to check against, so any authenticated sender is allowed —
        the same semantics send_message uses.
        """
        channel = self._storage.get_channel(channel_hash_hex)
        if channel is None:
            return False
        perms = permissions_from_json(channel["permissions"])
        if is_open_join(perms):
            return True
        if not self._storage.is_member(channel_hash_hex, sender_hex):
            return False
        return self._storage.has_permission(
            channel_hash_hex, sender_hex, VOICE_CHAT)

    def _authorize_link(self, peer_hex: str, channel_hash_hex: str) -> bool:
        """Transport authorize callback for inbound link handshakes."""
        if channel_hash_hex != self._session_channel:
            return False
        now = time.time()
        with self._lock:
            live = self._live_roster(channel_hash_hex, now)
        occupants = set(live) | {peer_hex}
        if len(occupants) > MAX_VOICE_PARTICIPANTS:
            return False
        return self._peer_may_voice(channel_hash_hex, peer_hex)

    # --- inbound signalling ---

    def _on_lxmf_message(self, message: LXMF.LXMessage):
        fields = message.fields or {}
        msg_type = fields.get(F_MSG_TYPE)
        if msg_type is None:
            return
        if isinstance(msg_type, bytes):
            msg_type = msg_type.decode(errors="replace")
        if msg_type not in (MT_VOICE_JOIN, MT_VOICE_LEAVE, MT_VOICE_STATE):
            return

        channel_hash_bytes = fields.get(F_CHANNEL_HASH)
        if not channel_hash_bytes:
            return
        channel_hash_hex = channel_hash_bytes.hex() \
            if isinstance(channel_hash_bytes, bytes) else str(channel_hash_bytes)

        sender_identity = RNS.Identity.recall(message.source_hash) \
            if message.source_hash else None
        sender_hex = sender_identity.hash.hex() if sender_identity else (
            message.source_hash.hex() if message.source_hash else "")
        if not sender_hex or sender_hex == self._identity.hash_hex:
            return

        now = time.time()
        timestamp = fields.get(F_TIMESTAMP)
        if not isinstance(timestamp, (int, float)) or \
                abs(now - timestamp) > VOICE_SIGNAL_MAX_AGE_SECS:
            RNS.log(
                f"TrenchChat [voice]: dropped stale {msg_type} from "
                f"{sender_hex[:12]}…",
                RNS.LOG_WARNING,
            )
            return

        if not self._peer_may_voice(channel_hash_hex, sender_hex):
            RNS.log(
                f"TrenchChat [voice]: rejected {msg_type} from "
                f"{sender_hex[:12]}… for {channel_hash_hex[:12]}…",
                RNS.LOG_WARNING,
            )
            return

        if msg_type == MT_VOICE_LEAVE:
            with self._lock:
                self._rosters.get(channel_hash_hex, {}).pop(sender_hex, None)
            if self._transport is not None and \
                    channel_hash_hex == self._session_channel:
                self._transport.disconnect(sender_hex)
                if self._audio_pipeline is not None:
                    try:
                        self._audio_pipeline.drop_peer(sender_hex)
                    except Exception as e:
                        RNS.log(
                            f"TrenchChat [voice]: pipeline drop error: {e}",
                            RNS.LOG_ERROR)
            self._notify_roster(channel_hash_hex)
            return

        # JOIN and STATE both upsert the sender's own entry — never anyone
        # else's, so a forged message can't assert third-party presence.
        muted = bool(fields.get(F_VOICE_MUTED, False))
        joined_at = fields.get(F_VOICE_JOINED_AT)
        if not isinstance(joined_at, (int, float)):
            joined_at = timestamp
        codec = fields.get(F_VOICE_CODEC, VOICE_CODEC_OPUS)
        if isinstance(codec, bytes):
            codec = codec.decode(errors="replace")

        with self._lock:
            self._upsert_entry(channel_hash_hex, sender_hex, muted=muted,
                               joined_at=float(joined_at), now=now,
                               codec=codec)

        if channel_hash_hex == self._session_channel:
            if msg_type == MT_VOICE_JOIN:
                self._send_state_to(sender_hex, channel_hash_hex)
            if self._transport is not None:
                self._transport.connect(sender_hex)

        self._notify_roster(channel_hash_hex)

    # --- frame plane hooks ---

    def _on_frames(self, peer_hex: str, seq: int, frames: list[bytes]):
        now = time.time()
        newly_speaking = False
        with self._lock:
            self._rx_frames[peer_hex] = \
                self._rx_frames.get(peer_hex, 0) + len(frames)
            self._last_frame_at[peer_hex] = now
            self._track_rx_quality(peer_hex, seq, len(frames), now)
            if not self._speaking.get(peer_hex, False):
                self._speaking[peer_hex] = True
                newly_speaking = True
        if newly_speaking and self._session_channel is not None:
            self._notify_speaking(self._session_channel, peer_hex, True)
        if self._audio_pipeline is not None:
            try:
                self._audio_pipeline.play(peer_hex, seq, frames)
            except Exception as e:
                RNS.log(f"TrenchChat [voice]: playback error: {e}",
                        RNS.LOG_ERROR)

    def _track_rx_quality(self, peer_hex: str, seq: int, count: int,
                          now: float):
        """Caller holds the lock. Frame seq numbers are the send clock:
        a jump past the expected next seq counts as loss (recredited if the
        packet later arrives late), and the deviation between arrival
        spacing and seq spacing feeds a smoothed jitter estimate."""
        q = self._rx_quality.get(peer_hex)
        if q is None:
            q = {"received": 0, "lost": 0, "late": 0, "jitter_ms": 0.0,
                 "next_seq": None, "last_seq": None, "last_arrival": 0.0}
            self._rx_quality[peer_hex] = q
        q["received"] += count

        if q["next_seq"] is not None:
            gap = seq_distance(seq, q["next_seq"])
            if gap > 0:
                q["lost"] += gap
            elif gap < 0:
                q["late"] += count
                q["lost"] = max(0, q["lost"] - count)
        if q["next_seq"] is None or \
                seq_distance(seq + count, q["next_seq"]) > 0:
            q["next_seq"] = (seq + count) % SEQ_MODULUS

        if q["last_seq"] is not None:
            seq_delta = seq_distance(seq, q["last_seq"])
            if seq_delta > 0:
                expected_secs = seq_delta * VOICE_FRAME_MS / 1000.0
                deviation_ms = abs(
                    (now - q["last_arrival"]) - expected_secs) * 1000.0
                q["jitter_ms"] += (deviation_ms - q["jitter_ms"]) / 16.0
        if q["last_seq"] is None or seq_distance(seq, q["last_seq"]) > 0:
            q["last_seq"] = seq
            q["last_arrival"] = now

    def _on_encoded(self, seq: int, frames: list[bytes]):
        """Encoded audio from the local pipeline, ready to transmit."""
        if self._transport is None or self._session_channel is None:
            return
        try:
            self._transport.send_frames(seq, frames)
            with self._lock:
                self._tx_packets += 1
        except Exception as e:
            RNS.log(f"TrenchChat [voice]: frame send error: {e}",
                    RNS.LOG_ERROR)

    def _on_speaking_self(self, speaking: bool):
        channel_hash_hex = self._session_channel
        if channel_hash_hex is None:
            return
        self_hex = self._identity.hash_hex
        with self._lock:
            if self._speaking.get(self_hex, False) == speaking:
                return
            self._speaking[self_hex] = speaking
        self._notify_speaking(channel_hash_hex, self_hex, speaking)

    def _on_peer_link_state(self, peer_hex: str, state: str):
        channel_hash_hex = self._session_channel
        if channel_hash_hex is not None:
            self._notify_roster(channel_hash_hex)

    # --- internals ---

    def _live_roster(self, channel_hash_hex: str, now: float) -> dict:
        """Roster entries not yet expired. Caller holds the lock."""
        roster = self._rosters.get(channel_hash_hex, {})
        return {
            peer_hex: entry for peer_hex, entry in roster.items()
            if now - entry["last_heard"] <= self._roster_ttl_secs
        }

    def _upsert_entry(self, channel_hash_hex: str, peer_hex: str, *,
                      muted: bool, joined_at: float, now: float,
                      codec: str = VOICE_CODEC_OPUS):
        roster = self._rosters.setdefault(channel_hash_hex, {})
        entry = roster.get(peer_hex)
        if entry is None:
            roster[peer_hex] = {"muted": muted, "joined_at": joined_at,
                                "last_heard": now, "codec": codec}
        else:
            entry["muted"] = muted
            entry["last_heard"] = now
            entry["codec"] = codec

    def _link_state_for(self, channel_hash_hex: str, peer_hex: str,
                        now: float) -> str:
        if peer_hex == self._identity.hash_hex:
            return LINK_SELF
        if self._transport is None or \
                channel_hash_hex != self._session_channel:
            return LINK_SIGNALLED
        state = self._transport.peer_state(peer_hex)
        if state in (LINK_STREAMING, LINK_CONNECTING, LINK_UNREACHABLE):
            return state
        return LINK_SIGNALLED

    def _redial_and_reauthorize(self, channel_hash_hex: str, now: float):
        with self._lock:
            live = set(self._live_roster(channel_hash_hex, now))
        live.discard(self._identity.hash_hex)

        for peer_hex in live:
            if self._transport.peer_state(peer_hex) != LINK_STREAMING:
                self._transport.connect(peer_hex)

        # A kick or demotion mid-call must cut the stream, not just the
        # roster: re-check every connected peer against current permissions.
        for peer_hex in self._transport.connected_peers():
            if not self._peer_may_voice(channel_hash_hex, peer_hex):
                RNS.log(
                    f"TrenchChat [voice]: disconnecting no-longer-authorized "
                    f"peer {peer_hex[:12]}…",
                    RNS.LOG_WARNING,
                )
                self._transport.disconnect(peer_hex)
                with self._lock:
                    self._rosters.get(channel_hash_hex, {}).pop(peer_hex, None)

    def _prune_rosters(self, now: float):
        changed: list[str] = []
        with self._lock:
            for channel_hash_hex, roster in self._rosters.items():
                expired = [
                    peer_hex for peer_hex, entry in roster.items()
                    if peer_hex != self._identity.hash_hex
                    and now - entry["last_heard"] > self._roster_ttl_secs
                    and not self._has_live_link(channel_hash_hex, peer_hex)
                ]
                for peer_hex in expired:
                    del roster[peer_hex]
                if expired:
                    changed.append(channel_hash_hex)
        for channel_hash_hex in changed:
            self._notify_roster(channel_hash_hex)

    def _has_live_link(self, channel_hash_hex: str, peer_hex: str) -> bool:
        if self._transport is None or \
                channel_hash_hex != self._session_channel:
            return False
        return self._transport.peer_state(peer_hex) == LINK_STREAMING

    def _update_speaking(self, now: float):
        stopped: list[str] = []
        with self._lock:
            for peer_hex, speaking in list(self._speaking.items()):
                if peer_hex == self._identity.hash_hex:
                    continue
                last = self._last_frame_at.get(peer_hex, 0.0)
                if speaking and now - last > SPEAKING_HOLD_SECS:
                    self._speaking[peer_hex] = False
                    stopped.append(peer_hex)
        channel_hash_hex = self._session_channel
        if channel_hash_hex is not None:
            for peer_hex in stopped:
                self._notify_speaking(channel_hash_hex, peer_hex, False)

    # --- audio pipeline ---

    def _start_audio(self):
        factory = self._audio_factory
        if factory is None:
            try:
                from trenchchat.core.audio import create_pipeline
                factory = create_pipeline
            except Exception as e:
                self._audio_error = f"audio unavailable: {e}"
                self._notify_session(SESSION_AUDIO_ERROR)
                return
        try:
            self._audio_pipeline = factory(
                self._config, self._on_encoded, self._on_speaking_self)
            if self._audio_pipeline is not None:
                self._audio_pipeline.set_muted(self._muted)
                self._audio_pipeline.start()
                self._audio_error = ""
                return
            self._audio_error = "no audio pipeline available"
        except Exception as e:
            RNS.log(f"TrenchChat [voice]: audio start failed: {e}",
                    RNS.LOG_ERROR)
            self._audio_pipeline = None
            self._audio_error = str(e)
        self._notify_session(SESSION_AUDIO_ERROR)

    def _stop_audio(self):
        pipeline = self._audio_pipeline
        self._audio_pipeline = None
        if pipeline is not None:
            try:
                pipeline.stop()
            except Exception as e:
                RNS.log(f"TrenchChat [voice]: audio stop error: {e}",
                        RNS.LOG_ERROR)

    # --- outbound signalling ---

    def _voice_fields(self, msg_type: str, channel_hash_hex: str) -> dict:
        fields = {
            F_MSG_TYPE: msg_type,
            F_CHANNEL_HASH: bytes.fromhex(channel_hash_hex),
            F_TIMESTAMP: time.time(),
        }
        if msg_type in (MT_VOICE_JOIN, MT_VOICE_STATE):
            fields[F_VOICE_MUTED] = self._muted
            fields[F_VOICE_JOINED_AT] = self._joined_at
            fields[F_VOICE_CODEC] = VOICE_CODEC_OPUS
        return fields

    def _broadcast(self, msg_type: str, channel_hash_hex: str):
        recipients = compute_channel_recipients(
            self._storage, self._subscription_mgr, channel_hash_hex,
            self._identity.hash_hex,
        )
        fields = self._voice_fields(msg_type, channel_hash_hex)
        for dest_hex in recipients:
            if dest_hex == self._identity.hash_hex:
                continue
            self._send_raw(dest_hex, dict(fields))

    def _send_state_to(self, dest_hex: str, channel_hash_hex: str):
        self._send_raw(dest_hex,
                       self._voice_fields(MT_VOICE_STATE, channel_hash_hex))

    def _send_raw(self, dest_hex: str, fields: dict):
        try:
            identity_hash = bytes.fromhex(dest_hex)
            delivery_dest_hash = RNS.Destination.hash(
                identity_hash, "lxmf", "delivery")
            dest_identity = RNS.Identity.recall(delivery_dest_hash)
            if dest_identity is None:
                RNS.Transport.request_path(delivery_dest_hash)
                return
            dest = RNS.Destination(
                dest_identity,
                RNS.Destination.OUT,
                RNS.Destination.SINGLE,
                "lxmf",
                "delivery",
            )
            lxm = LXMF.LXMessage(
                dest,
                self._router.delivery_destination,
                "",
                desired_method=LXMF.LXMessage.DIRECT,
            )
            lxm.fields = fields
            self._router.send(lxm)
        except Exception as e:
            RNS.log(f"TrenchChat [voice]: signalling send error: {e}",
                    RNS.LOG_WARNING)

    # --- callback dispatchers ---

    def _notify_roster(self, channel_hash_hex: str):
        for cb in self._roster_callbacks:
            try:
                cb(channel_hash_hex)
            except Exception as e:
                RNS.log(f"TrenchChat [voice]: roster callback error: {e}",
                        RNS.LOG_ERROR)

    def _notify_speaking(self, channel_hash_hex: str, peer_hex: str,
                         speaking: bool):
        for cb in self._speaking_callbacks:
            try:
                cb(channel_hash_hex, peer_hex, speaking)
            except Exception as e:
                RNS.log(f"TrenchChat [voice]: speaking callback error: {e}",
                        RNS.LOG_ERROR)

    def _notify_session(self, state: str):
        for cb in self._session_callbacks:
            try:
                cb(state)
            except Exception as e:
                RNS.log(f"TrenchChat [voice]: session callback error: {e}",
                        RNS.LOG_ERROR)
