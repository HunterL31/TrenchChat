"""
Live group voice sessions.

Every channel implicitly has one voice room. Signalling rides LXMF control
messages (join/leave/state) and is deliberately low-rate; audio frames never
touch LXMF, they flow over RNS Links managed by a VoiceTransport
(trenchchat/network/voice_transport.py) injected at construction.

Every signalling message asserts state about the sender only; nobody relays
third-party presence. A joiner learns the current occupants because each
participant, on receiving the join, unicasts one voice_state describing
itself. The roster is therefore an eventually-consistent presence hint;
established and identified links are the ground truth for who is heard.

Callbacks fire on background threads. UI consumers must marshal onto
their own main thread and gate the join control on
has_permission(channel, self, VOICE_CHAT).
"""

import threading
import time

import RNS

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
from trenchchat.config import VOICE_MIN_BITRATE
from trenchchat.network.base import (
    InboundMessage, PATH_DIRECT, PATH_RETICULUM, SendState,
)
from trenchchat.network.router import Router
from trenchchat.network.voice_transport import PEER_STREAMING
from trenchchat.network.voice_wire import (
    SEQ_MODULUS, VOICE_FRAME_MS, seq_distance,
)

VOICE_STATE_REFRESH_SECS = 60.0
AUDIO_RESTART_COOLDOWN_SECS = 5.0
VOICE_ROSTER_TTL_SECS = 180.0
VOICE_SIGNAL_MAX_AGE_SECS = 120.0
VOICE_STATE_MIN_INTERVAL_SECS = 2.0
MAX_VOICE_PARTICIPANTS = 8
SPEAKING_HOLD_SECS = 0.3
VOICE_CODEC_OPUS = "opus"
NOMINAL_FRAME_RATE_FPS = 1000.0 / VOICE_FRAME_MS
# Below this the arrival span is too short for a meaningful rate.
RATE_MIN_SPAN_SECS = 1.0

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
                 roster_ttl_secs: float = VOICE_ROSTER_TTL_SECS,
                 direct_transport=None):
        """
        transport: the mesh frame plane, over RNS Links.
        direct_transport: the frame plane a direct session carries, used for a
        pair whose path is direct and for no other.
        """
        self._identity = identity
        self._storage = storage
        self._router = router
        self._subscription_mgr = subscription_mgr
        self._config = config
        self._transport = transport
        self._direct = direct_transport
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
        # Whether this node's own join has gone out yet. Until it has, this
        # node says nothing about itself: a peer that hears a state first
        # records a discovered occupant, and the join it sends a moment later
        # is then read as a refresh and never announces the arrival.
        self._join_announced = False
        self._state_dirty = False
        self._audio_pipeline = None
        self._audio_error = ""
        self._last_audio_restart = 0.0

        self._tx_packets = 0
        self._rx_frames: dict[str, int] = {}
        self._rx_quality: dict[str, dict] = {}
        self._first_frame_at: dict[str, float] = {}
        self._last_frame_at: dict[str, float] = {}
        self._speaking: dict[str, bool] = {}

        self._roster_callbacks: list = []
        self._speaking_callbacks: list = []
        self._session_callbacks: list = []

        for plane in self._planes():
            plane.set_frame_callback(self._on_frames)
            plane.set_peer_state_callback(self._on_peer_link_state)
            plane.set_authorize_callback(self._authorize_link)

        router.add_delivery_callback(self._on_message)

    def _planes(self) -> list:
        """Every frame plane this node can carry voice over, mesh first."""
        planes = [] if self._transport is None else [self._transport]
        if self._direct is not None:
            planes.append(self._direct)
        return planes

    def _plane_for(self, peer_hex: str):
        """The plane this pair streams over, chosen by the path to the peer.

        Asked afresh wherever a pair is acted on, so a session that comes up or
        goes away moves the pair with it rather than stranding it on a plane
        that can no longer reach.
        """
        if self._direct is not None and \
                self._router.path_for(peer_hex) == PATH_DIRECT:
            return self._direct
        return self._transport if self._transport is not None else self._direct

    def _path_for(self, peer_hex: str) -> str:
        """Which path a frame to this peer would take."""
        if self._direct is not None and self._plane_for(peer_hex) is self._direct:
            return PATH_DIRECT
        return PATH_RETICULUM

    def _connected_peers(self) -> set[str]:
        """Every peer streaming on any plane."""
        peers: set[str] = set()
        for plane in self._planes():
            peers |= plane.connected_peers()
        return peers

    def session_bitrate(self) -> int:
        """What this session encodes at: the least any of its pairs affords.

        One encoder feeds every pair, so a session with a mesh pair in it may
        not encode past what the mesh carries, however fast the other pairs
        are. Decided when the pipeline starts, from the pairs known then: the
        pipeline is not rebuilt mid-call for a path that changed, because
        rebuilding it costs the call a gap and the codec conceals a slower
        pair better than silence conceals a restart.
        """
        configured = (self._config.voice_bitrate if self._config is not None
                      else VOICE_MIN_BITRATE)
        channel_hash_hex = self._session_channel
        if channel_hash_hex is None:
            return configured
        with self._lock:
            peers = set(self._live_roster(channel_hash_hex, time.time()))
        peers |= self._connected_peers()
        peers.discard(self._identity.hash_hex)
        budgets = [self._router.limits_for(peer).voice_bitrate_bps
                   for peer in peers]
        return max(VOICE_MIN_BITRATE, min([configured] + budgets))

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
            # Real occupancy is established links, never the signalled roster:
            # voice_join is unauthenticated, so a flood of forged ones would
            # otherwise fill the roster and lock every legit member out. Links
            # are what actually drive fan-out, which is why the cap exists;
            # _authorize_link enforces the same cap per inbound link.
            real_occupancy = self._connected_peers()
            if len(real_occupancy) >= MAX_VOICE_PARTICIPANTS:
                RNS.log(
                    f"TrenchChat [voice]: session for "
                    f"{channel_hash_hex[:12]}… is full",
                    RNS.LOG_WARNING,
                )
                return False
            self._session_channel = channel_hash_hex
            self._joined_at = now
            self._join_announced = False
            # Set here rather than after the broadcast below, so a tick
            # between the two does not find the refresh overdue.
            self._last_state_sent = now
            self._upsert_entry(channel_hash_hex, self._identity.hash_hex,
                               muted=self._muted, joined_at=now, now=now)
            peers = [p for p in roster if p != self._identity.hash_hex]

        for plane in self._planes():
            plane.start(channel_hash_hex)
        self._start_audio()
        self._play_cue(join=True)
        self._broadcast(MT_VOICE_JOIN, channel_hash_hex)
        self._join_announced = True
        for peer_hex in peers:
            self._connect_peer(peer_hex)

        self._notify_roster(channel_hash_hex)
        self._notify_session(SESSION_JOINED)
        return True

    def leave_voice(self) -> None:
        """Leave the current voice session; a no-op when not in one."""
        channel_hash_hex = self._session_channel
        if channel_hash_hex is None:
            return
        self._broadcast(MT_VOICE_LEAVE, channel_hash_hex)
        self._join_announced = False
        # Cleared before the transport stops: _authorize_link compares against
        # it, so a VP_HELLO arriving in between would be authorised against
        # the session we are leaving and repopulate the connection table after
        # stop() had emptied it.
        with self._lock:
            self._session_channel = None
        self._stop_audio()
        for plane in self._planes():
            plane.stop()
        with self._lock:
            roster = self._rosters.get(channel_hash_hex, {})
            roster.pop(self._identity.hash_hex, None)
            self._rx_frames.clear()
            self._rx_quality.clear()
            self._first_frame_at.clear()
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

    def restart_audio(self) -> None:
        """Rebuild the audio pipeline mid-session, after a device change,
        or when a stream died under an unplugged device. Device names are
        re-resolved on start, so a vanished device falls back to the system
        default. No-op outside a session."""
        if self._session_channel is None:
            return
        self._last_audio_restart = time.time()
        self._stop_audio()
        self._start_audio()

    # --- roster read model ---

    def _link_only_peers(self, channel_hash_hex: str, known: set[str]) -> list[dict]:
        """Entries for peers we are streaming with but never heard signalling.

        docs/voice.md makes established links the ground truth for who you
        actually hear, but the roster is built from the presence hint alone --
        so a peer that skips signalling is audible and invisible.
        """
        if channel_hash_hex != self._session_channel:
            return []
        return [
            {"identity_hash": peer_hex, "muted": False, "joined_at": 0.0,
             "link_state": self._plane_for(peer_hex).peer_state(peer_hex),
             "path": self._path_for(peer_hex),
             "speaking": self._speaking.get(peer_hex, False)}
            for peer_hex in sorted(self._connected_peers() - known)
        ]

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
                "path": (None if peer_hex == self._identity.hash_hex
                         else self._path_for(peer_hex)),
                "speaking": self._speaking.get(peer_hex, False),
            })
        result.extend(self._link_only_peers(
            channel_hash_hex, {r["identity_hash"] for r in result}))
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

            if self._planes():
                for plane in self._planes():
                    plane.tick()
                self._redial_and_reauthorize(channel_hash_hex, now)
            self._check_audio_health(now)

        self._prune_rosters(now)
        self._update_speaking(now)

    def _check_audio_health(self, now: float) -> None:
        """Rebuild the pipeline when a device stream has died mid-session
        (unplug); the rebuild re-resolves devices and falls back to the
        default. Cooldown-limited so a persistently failing device cannot
        thrash."""
        pipeline = self._audio_pipeline
        if pipeline is None:
            return
        probe = getattr(pipeline, "healthy", None)
        if probe is None or probe():
            return
        if now - self._last_audio_restart < AUDIO_RESTART_COOLDOWN_SECS:
            return
        RNS.log("TrenchChat [voice]: audio stream died (device unplugged?); "
                "rebuilding pipeline", RNS.LOG_WARNING)
        self.restart_audio()

    def frame_stats(self) -> dict:
        """Transmit/receive counters, per-sender receive quality, playout.

        rx_quality per peer: received/lost/late frame counts, loss_pct,
        smoothed inter-arrival jitter in ms (RFC 3550-style, using frame
        sequence numbers as the send clock), and rate_fps, frames per
        second of wall clock, None until a peer has been heard for
        RATE_MIN_SPAN_SECS. Everything but rate_fps is clocked by sequence
        number, so a uniformly slow sender scores clean on all of them
        while starving the listener's jitter buffer; rate_fps against
        NOMINAL_FRAME_RATE_FPS is what shows it.

        "playout" carries the pipeline's per-peer continuity counters
        (decoded/plc/starved), or {} for a pipeline that has none, and
        "paths" the path each pair's frames take, which is what makes one
        peer's quality comparable to another's in a mixed session.
        """
        with self._lock:
            quality = {}
            for peer_hex, q in self._rx_quality.items():
                total = q["received"] + q["lost"]
                span = self._last_frame_at.get(peer_hex, 0.0) - \
                    self._first_frame_at.get(peer_hex, 0.0)
                quality[peer_hex] = {
                    "received": q["received"],
                    "lost": q["lost"],
                    "late": q["late"],
                    "jitter_ms": round(q["jitter_ms"], 2),
                    "loss_pct": round(100.0 * q["lost"] / total, 2)
                    if total else 0.0,
                    "rate_fps": round(q["received"] / span, 1)
                    if span >= RATE_MIN_SPAN_SECS else None,
                }
            stats = {
                "tx_packets": self._tx_packets,
                "rx_frames": dict(self._rx_frames),
                "rx_quality": quality,
            }
        stats["playout"] = self._playout_stats()
        stats["paths"] = {peer_hex: self._path_for(peer_hex)
                          for peer_hex in self._connected_peers()}
        stats["bitrate_bps"] = self.session_bitrate()
        return stats

    def _playout_stats(self) -> dict:
        """The active pipeline's continuity counters; {} for one without."""
        reader = getattr(self._audio_pipeline, "playout_stats", None)
        if reader is None:
            return {}
        try:
            return reader()
        except Exception as e:
            RNS.log(f"TrenchChat [voice]: playout stats error: {e}",
                    RNS.LOG_DEBUG)
            return {}

    def audio_status(self) -> dict:
        """Pipeline availability plus per-direction device state.

        available means a pipeline exists at all; input_ok/output_ok say
        which halves are actually running, with the open failure recorded
        per direction. A pipeline without devices to report (the tone
        pipeline) counts as fully ok.
        """
        pipeline = self._audio_pipeline
        if pipeline is None:
            return {"available": False,
                    "reason": self._audio_error or "no audio pipeline",
                    "input_ok": False, "output_ok": False,
                    "input_error": "", "output_error": ""}
        status = {"available": True, "reason": "",
                  "input_ok": True, "output_ok": True,
                  "input_error": "", "output_error": ""}
        status.update(self._device_status(pipeline))
        return status

    @staticmethod
    def _device_status(pipeline) -> dict:
        reader = getattr(pipeline, "device_status", None)
        if reader is None:
            return {}
        try:
            return reader()
        except Exception as e:
            RNS.log(f"TrenchChat [voice]: device status error: {e}",
                    RNS.LOG_DEBUG)
            return {}

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
        table to check against, so any authenticated sender is allowed,
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
        """Transport authorize callback for inbound link handshakes.

        Occupancy counts established links as well as the signalled roster.
        The roster is built only from LXMF signalling, so a peer that dials
        in without ever sending voice_join is in neither it nor the count --
        and links are what actually drive fan-out, which is the whole reason
        the cap exists.
        """
        if channel_hash_hex != self._session_channel:
            return False
        now = time.time()
        with self._lock:
            live = self._live_roster(channel_hash_hex, now)
        occupants = set(live) | self._connected_peers() | {peer_hex}
        if len(occupants) > MAX_VOICE_PARTICIPANTS:
            RNS.log(
                f"TrenchChat [voice]: refusing {peer_hex[:12]}… — "
                f"{len(occupants)} would exceed {MAX_VOICE_PARTICIPANTS}",
                RNS.LOG_WARNING,
            )
            return False
        return self._peer_may_voice(channel_hash_hex, peer_hex)

    # --- inbound signalling ---

    def _on_message(self, message: InboundMessage):
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

        sender_hex = message.source_hex
        if not sender_hex or sender_hex == self._identity.hash_hex:
            return

        now = time.time()
        timestamp = fields.get(F_TIMESTAMP)
        if not isinstance(timestamp, (int, float)):
            RNS.log(
                f"TrenchChat [voice]: dropped {msg_type} from "
                f"{sender_hex[:12]}… — missing or invalid timestamp",
                RNS.LOG_WARNING,
            )
            return
        skew = now - timestamp
        if abs(skew) > VOICE_SIGNAL_MAX_AGE_SECS:
            # Distinguishable from packet loss for a clock-drifting mesh node:
            # name the skew and its direction so it is diagnosable.
            direction = "past" if skew > 0 else "future"
            RNS.log(
                f"TrenchChat [voice]: dropped {msg_type} from "
                f"{sender_hex[:12]}… — clock skew {abs(skew):.0f}s ({direction}) "
                f"exceeds {VOICE_SIGNAL_MAX_AGE_SECS:.0f}s; check clock sync",
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
                departed = self._rosters.get(channel_hash_hex, {}).pop(
                    sender_hex, None)
            if departed is not None and \
                    channel_hash_hex == self._session_channel:
                self._play_cue(join=False)
            if self._planes() and \
                    channel_hash_hex == self._session_channel:
                self._disconnect_peer(sender_hex)
                if self._audio_pipeline is not None:
                    try:
                        self._audio_pipeline.drop_peer(sender_hex)
                    except Exception as e:
                        RNS.log(
                            f"TrenchChat [voice]: pipeline drop error: {e}",
                            RNS.LOG_ERROR)
            self._notify_roster(channel_hash_hex)
            return

        # JOIN and STATE both upsert the sender's own entry, never anyone
        # else's, so a forged message can't assert third-party presence.
        muted = bool(fields.get(F_VOICE_MUTED, False))
        joined_at = fields.get(F_VOICE_JOINED_AT)
        if not isinstance(joined_at, (int, float)):
            joined_at = timestamp
        codec = fields.get(F_VOICE_CODEC, VOICE_CODEC_OPUS)
        if isinstance(codec, bytes):
            codec = codec.decode(errors="replace")

        with self._lock:
            # A cue only for a genuine newcomer: a JOIN re-broadcast for a
            # peer already on the roster, or occupants learned via their
            # STATE replies to our own join, must not blip.
            newcomer = sender_hex not in self._rosters.get(channel_hash_hex, {})
            self._upsert_entry(channel_hash_hex, sender_hex, muted=muted,
                               joined_at=float(joined_at), now=now,
                               codec=codec)

        if channel_hash_hex == self._session_channel:
            # Answering before our own join has gone out would reach them
            # first and make this node a discovered occupant instead of a
            # joiner; the join already on its way tells them the same thing.
            if msg_type == MT_VOICE_JOIN:
                if self._join_announced:
                    self._send_state_to(sender_hex, channel_hash_hex)
                if newcomer:
                    self._play_cue(join=True)
            if self._planes():
                self._connect_peer(sender_hex)

        self._notify_roster(channel_hash_hex)

    # --- frame plane hooks ---

    def _on_frames(self, peer_hex: str, seq: int, frames: list[bytes]):
        now = time.time()
        newly_speaking = False
        with self._lock:
            self._rx_frames[peer_hex] = \
                self._rx_frames.get(peer_hex, 0) + len(frames)
            self._first_frame_at.setdefault(peer_hex, now)
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
        """Encoded audio from the local pipeline, ready to transmit.

        Every plane is handed the same bundle and sends it to the pairs it
        carries, so a session mixes paths without the pipeline knowing there
        is more than one.
        """
        if self._session_channel is None:
            return
        for plane in self._planes():
            try:
                plane.send_frames(seq, frames)
            except Exception as e:
                RNS.log(f"TrenchChat [voice]: frame send error: {e}",
                        RNS.LOG_ERROR)
        with self._lock:
            self._tx_packets += 1

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
        # A jitter buffer and a native Opus decoder are allocated per sender
        # and were released only on a polite voice_leave, so a peer whose link
        # simply dropped, or who was disconnected for losing the permission,
        # left both behind for the rest of the session.
        if state != PEER_STREAMING and self._audio_pipeline is not None:
            try:
                self._audio_pipeline.drop_peer(peer_hex)
            except Exception as e:
                RNS.log(f"TrenchChat [voice]: releasing {peer_hex[:12]}… failed: {e}",
                        RNS.LOG_DEBUG)
            with self._lock:
                self._rx_frames.pop(peer_hex, None)
                self._rx_quality.pop(peer_hex, None)
                self._first_frame_at.pop(peer_hex, None)
                self._last_frame_at.pop(peer_hex, None)
                self._speaking.pop(peer_hex, None)
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
        if not self._planes() or channel_hash_hex != self._session_channel:
            return LINK_SIGNALLED
        state = self._plane_for(peer_hex).peer_state(peer_hex)
        if state in (LINK_STREAMING, LINK_CONNECTING, LINK_UNREACHABLE):
            return state
        return LINK_SIGNALLED

    def _redial_and_reauthorize(self, channel_hash_hex: str, now: float):
        with self._lock:
            live = set(self._live_roster(channel_hash_hex, now))
        live.discard(self._identity.hash_hex)

        for peer_hex in live:
            if self._plane_for(peer_hex).peer_state(peer_hex) != LINK_STREAMING:
                self._connect_peer(peer_hex)

        # A kick or demotion mid-call must cut the stream, not just the
        # roster: re-check every connected peer against current permissions.
        for peer_hex in self._connected_peers():
            if not self._peer_may_voice(channel_hash_hex, peer_hex):
                RNS.log(
                    f"TrenchChat [voice]: disconnecting no-longer-authorized "
                    f"peer {peer_hex[:12]}…",
                    RNS.LOG_WARNING,
                )
                self._disconnect_peer(peer_hex)
                with self._lock:
                    self._rosters.get(channel_hash_hex, {}).pop(peer_hex, None)

    def _prune_rosters(self, now: float):
        changed: list[str] = []
        stale_conns: list[str] = []
        session_departures = 0
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
                    stale_conns.append(peer_hex)
                if expired:
                    changed.append(channel_hash_hex)
                    if channel_hash_hex == self._session_channel:
                        session_departures += len(expired)
        # A timed-out peer left without saying so; same blip as a polite
        # leave, once per departed peer.
        for _ in range(session_departures):
            self._play_cue(join=False)
        # An expired roster entry with no live link is a peer we have stopped
        # hearing from: without this the connection stays, and every re-dial
        # of it is another mesh-wide path request.
        for peer_hex in stale_conns:
            try:
                self._disconnect_peer(peer_hex)
            except Exception as e:
                RNS.log(f"TrenchChat [voice]: disconnecting {peer_hex[:12]}… failed: {e}",
                        RNS.LOG_DEBUG)
        for channel_hash_hex in changed:
            self._notify_roster(channel_hash_hex)

    def _has_live_link(self, channel_hash_hex: str, peer_hex: str) -> bool:
        if not self._planes() or channel_hash_hex != self._session_channel:
            return False
        return any(plane.peer_state(peer_hex) == LINK_STREAMING
                   for plane in self._planes())

    def _connect_peer(self, peer_hex: str) -> None:
        """Bring one pair up on the plane its path calls for.

        A pair whose path changed is dropped from the plane it was on first:
        two planes streaming with one peer would double every frame.
        """
        plane = self._plane_for(peer_hex)
        for other in self._planes():
            if other is not plane and peer_hex in other.connected_peers():
                other.disconnect(peer_hex)
        plane.connect(peer_hex)

    def _disconnect_peer(self, peer_hex: str) -> None:
        """Stop streaming with one peer on whichever plane was carrying it."""
        for plane in self._planes():
            plane.disconnect(peer_hex)

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

    def _play_cue(self, *, join: bool) -> None:
        """Local join/leave blip, mixed into playout. Silent with
        voice.event_sounds off, without a pipeline, or on a pipeline that
        cannot play cues. Our own leave is silent by design: the pipeline
        stops immediately, so a cue would only ever be cut off."""
        if not getattr(self._config, "voice_event_sounds", True):
            return
        player = getattr(self._audio_pipeline, "play_cue", None)
        if player is None:
            return
        try:
            from trenchchat.core.audio.cues import join_cue, leave_cue
            player(join_cue() if join else leave_cue())
        except Exception as e:
            RNS.log(f"TrenchChat [voice]: cue error: {e}", RNS.LOG_DEBUG)

    def _start_audio(self):
        factory = self._audio_factory
        # The encoder the default pipeline is built with, decided here because
        # only this layer knows what the session's pairs afford. An injected
        # factory (a headless tester's tone pipeline) brings its own.
        extra: tuple = ()
        if factory is None:
            try:
                from trenchchat.core.audio import create_pipeline
                from trenchchat.core.audio.codec import OpusCodec
                factory = create_pipeline
                bitrate = self.session_bitrate()
                extra = (lambda: OpusCodec(bitrate=bitrate),)
            except Exception as e:
                self._audio_error = f"audio unavailable: {e}"
                self._notify_session(SESSION_AUDIO_ERROR)
                return
        try:
            self._audio_pipeline = factory(
                self._config, self._on_encoded, self._on_speaking_self, *extra)
            if self._audio_pipeline is not None:
                self._audio_pipeline.set_muted(self._muted)
                self._audio_pipeline.start()
                self._audio_error = ""
                status = self._device_status(self._audio_pipeline)
                if not status.get("input_ok", True) or \
                        not status.get("output_ok", True):
                    self._notify_session(SESSION_AUDIO_ERROR)
                return
            self._audio_error = self._unavailable_reason()
        except Exception as e:
            RNS.log(f"TrenchChat [voice]: audio start failed: {e}",
                    RNS.LOG_ERROR)
            self._audio_pipeline = None
            self._audio_error = str(e)
        self._notify_session(SESSION_AUDIO_ERROR)

    def _unavailable_reason(self) -> str:
        """Why the default factory built no pipeline: the failed import
        (sounddevice/numpy/opus), re-probed because create_pipeline only
        logs it, and the client needs it in audio_status()."""
        if self._audio_factory is None:
            try:
                from trenchchat.core.audio import audio_available
                available, reason = audio_available()
                if not available and reason:
                    return reason
            except Exception as e:
                RNS.log(f"TrenchChat [voice]: availability probe error: {e}",
                        RNS.LOG_DEBUG)
        return "no audio pipeline available"

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
        """Send one voice signalling message, asking for a path if there is none."""
        try:
            if self._router.send(dest_hex, fields) is SendState.NO_PATH:
                self._router.request_path(dest_hex)
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
