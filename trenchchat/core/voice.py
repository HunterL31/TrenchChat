"""
Voice channel management using LXST for real-time audio streaming over Reticulum.

Architecture (star topology):
- One node acts as the host (either the channel owner or a delegated Voice Relay).
- Each participant establishes an RNS Link to the host's voice RNS Destination.
- The host runs an LXST Mixer per participant (N-1 mixing -- participants don't
  hear their own audio).
- Audio frames are sent over the Link via LXST Packetizer / LinkSource.

LXMF is used only for join/leave coordination, participant list broadcasts, and
relay control messages. Real-time audio and in-band signals travel over RNS Links.

Mic modes:
- PTT (push-to-talk): LineSource starts/stops when PTT key is held.
- VAD (voice activity detection): LineSource always runs; VadFilter gates frames.

The VoiceManager is reused by the headless relay daemon (host_only=True), which
skips local mic/speaker pipeline setup.
"""

import time
import threading
import hashlib

import RNS
import LXMF
import msgpack

from trenchchat import APP_NAME
from trenchchat.core.identity import Identity
from trenchchat.core.storage import Storage
from trenchchat.core.permissions import SPEAK, MANAGE_RELAY
from trenchchat.core.protocol import (
    F_CHANNEL_HASH, F_DISPLAY_NAME, F_MSG_TYPE,
    F_VOICE_DEST_HASH, F_VOICE_PARTICIPANTS, F_VOICE_CODEC_PROFILE, F_VOICE_SIGNAL,
    F_RELAY_TOKEN, F_RELAY_DEST_HASH, F_MEMBER_LIST_DOC,
    MT_VOICE_JOIN, MT_VOICE_LEAVE, MT_VOICE_STATE,
    MT_RELAY_ASSIGN, MT_RELAY_ACCEPT, MT_RELAY_REVOKE, MT_RELAY_MEMBER_UPDATE,
    VS_MUTE, VS_UNMUTE, VS_SPEAKING, VS_SILENT,
)

APP_ASPECT_VOICE = "voice"

# Default Opus voice profile for mesh-friendly bandwidth (~4.5kbps).
# Import lazily to avoid hard dependency when audio hardware is absent.
_OPUS_PROFILE_DEFAULT = 0x00   # Opus.PROFILE_VOICE_LOW

# How long to keep a silent participant slot alive before evicting them (seconds).
_PARTICIPANT_TIMEOUT_SECS = 30

# Relay token validity window in seconds.
_RELAY_TOKEN_VALIDITY_SECS = 300


def _make_relay_token(signing_identity: RNS.Identity, relay_dest_hash: bytes,
                      channel_hash_bytes: bytes, timestamp: float) -> bytes:
    """Sign a relay assignment token with the owner's Ed25519 key.

    Token payload: relay_dest_hash (16 bytes) || channel_hash_bytes (16 bytes)
                   || timestamp as big-endian int64 milliseconds
    """
    ts_ms = int(timestamp * 1000)
    payload = relay_dest_hash + channel_hash_bytes + ts_ms.to_bytes(8, "big")
    return signing_identity.sign(payload)


def _verify_relay_token(owner_identity: RNS.Identity, token: bytes,
                        relay_dest_hash: bytes, channel_hash_bytes: bytes,
                        timestamp: float) -> bool:
    """Verify a relay assignment token against the owner's public key.

    Returns True only if the signature is valid and the timestamp is within
    _RELAY_TOKEN_VALIDITY_SECS of now.
    """
    if abs(time.time() - timestamp) > _RELAY_TOKEN_VALIDITY_SECS:
        return False
    ts_ms = int(timestamp * 1000)
    payload = relay_dest_hash + channel_hash_bytes + ts_ms.to_bytes(8, "big")
    try:
        return owner_identity.validate(token, payload)
    except Exception:
        return False


class _ParticipantSession:
    """Host-side state for one connected participant."""

    def __init__(self, identity_hex: str, display_name: str, link: RNS.Link):
        self.identity_hex = identity_hex
        self.display_name = display_name
        self.link = link
        self.link_source = None     # LXST LinkSource (inbound audio from participant)
        self.mixer = None           # LXST Mixer (N-1 mixed audio for this participant)
        self.packetizer = None      # LXST Packetizer (outbound audio to participant)
        self.is_muted = False
        self.is_speaking = False
        self.joined_at = time.time()


class VoiceManager:
    """Manages voice channel hosting and participation.

    When *host_only* is True (used by the relay daemon) local mic/speaker
    pipelines are never created.  The manager still handles link acceptance,
    mixing, and relay control messages.

    Call ``register_lxmf_handler`` after construction to wire up inbound LXMF
    message routing.
    """

    def __init__(self, identity: Identity, storage: Storage, router,
                 host_only: bool = False):
        self._identity = identity
        self._storage = storage
        self._router = router
        self._host_only = host_only

        # channel_hash_hex -> RNS.Destination (voice endpoint this node hosts)
        self._hosted_destinations: dict[str, RNS.Destination] = {}
        # channel_hash_hex -> dict[identity_hex -> _ParticipantSession]
        self._sessions: dict[str, dict[str, _ParticipantSession]] = {}

        # Participant-side state (only used in non-host_only mode)
        # channel_hash_hex -> dict with pipeline objects
        self._active_sessions: dict[str, dict] = {}
        self._mic_mode: dict[str, str] = {}    # channel_hash_hex -> "ptt" | "vad"
        self._ptt_active: dict[str, bool] = {}  # channel_hash_hex -> bool
        self._muted: dict[str, bool] = {}       # channel_hash_hex -> bool
        self._deafened: dict[str, bool] = {}    # channel_hash_hex -> bool

        self._lock = threading.RLock()
        self._participant_callbacks: list = []
        self._speaking_callbacks: list = []
        self._voice_state_callbacks: list = []

    # ------------------------------------------------------------------
    # LXMF message routing
    # ------------------------------------------------------------------

    def register_lxmf_handler(self, router) -> None:
        """Wire up LXMF inbound handler. Called once after all managers are created."""
        if hasattr(router, "add_delivery_callback"):
            router.add_delivery_callback(self._on_lxmf_message)
        elif hasattr(router, "add_message_handler"):
            router.add_message_handler(self._on_lxmf_message)

    def _on_lxmf_message(self, message: LXMF.LXMessage) -> bool:
        """Handle inbound LXMF messages relevant to voice/relay. Returns True if consumed."""
        fields = message.fields or {}
        msg_type = fields.get(F_MSG_TYPE)
        if isinstance(msg_type, bytes):
            msg_type = msg_type.decode(errors="replace")

        src = message.source_hash.hex()[:8] if message.source_hash else "unknown"
        RNS.log(
            f"TrenchChat [voice]: inbound LXMF msg_type={msg_type!r} from {src}",
            RNS.LOG_DEBUG,
        )

        if msg_type == MT_VOICE_JOIN:
            self._handle_voice_join(message, fields)
            return True
        if msg_type == MT_VOICE_LEAVE:
            self._handle_voice_leave(message, fields)
            return True
        if msg_type == MT_VOICE_STATE:
            self._handle_voice_state(message, fields)
            return True
        if msg_type == MT_RELAY_ASSIGN:
            self._handle_relay_assign(message, fields)
            return True
        if msg_type == MT_RELAY_ACCEPT:
            self._handle_relay_accept(message, fields)
            return True
        if msg_type == MT_RELAY_REVOKE:
            self._handle_relay_revoke(message, fields)
            return True
        if msg_type == MT_RELAY_MEMBER_UPDATE:
            self._handle_relay_member_update(message, fields)
            return True

        if msg_type is not None:
            RNS.log(
                f"TrenchChat [voice]: unhandled msg_type={msg_type!r} from {src}",
                RNS.LOG_DEBUG,
            )
        return False

    # ------------------------------------------------------------------
    # Host-side: creating voice destinations
    # ------------------------------------------------------------------

    def create_voice_destination(self, channel_hash_hex: str) -> str | None:
        """Create an RNS voice destination for a channel we own or host.

        Idempotent: if a destination is already registered for this channel,
        the existing one is returned without error.

        Returns the voice destination hash hex, or None on failure.
        """
        with self._lock:
            existing = self._hosted_destinations.get(channel_hash_hex)
        if existing is not None:
            return existing.hash.hex()

        try:
            dest = RNS.Destination(
                self._identity.rns_identity,
                RNS.Destination.IN,
                RNS.Destination.SINGLE,
                APP_NAME,
                APP_ASPECT_VOICE,
                channel_hash_hex,
            )
        except KeyError:
            # RNS raises KeyError if the destination is already registered in the
            # Transport (can happen if the process re-assigns the same channel).
            # Look it up from the registered destinations instead.
            expected_hash = RNS.Destination.hash(
                self._identity.rns_identity, APP_NAME, APP_ASPECT_VOICE, channel_hash_hex
            )
            for registered in RNS.Transport.destinations:
                if registered.hash == expected_hash:
                    dest = registered
                    break
            else:
                RNS.log(
                    f"TrenchChat [voice]: could not recover already-registered destination "
                    f"for channel {channel_hash_hex[:8]}",
                    RNS.LOG_ERROR,
                )
                return None

        dest.set_link_established_callback(
            lambda link: self._on_link_established(channel_hash_hex, link)
        )
        with self._lock:
            self._hosted_destinations[channel_hash_hex] = dest
            self._sessions[channel_hash_hex] = {}
        RNS.log(
            f"TrenchChat [voice]: hosting destination for channel {channel_hash_hex[:8]} "
            f"at {dest.hash.hex()[:8]}",
            RNS.LOG_NOTICE,
        )
        return dest.hash.hex()

    def restore_voice_destinations(self) -> None:
        """Re-create voice destinations for owned text/voice channels on startup.

        Called after storage is initialised to restore RNS destinations that
        would otherwise be lost across restarts.
        """
        for row in self._storage.get_voice_channels():
            hash_hex = row["hash"]
            if row["creator_hash"] == self._identity.hash_hex:
                relay = row["relay_dest_hash"]
                if relay:
                    # A relay is assigned — it hosts the voice destination; skip local hosting.
                    RNS.log(
                        f"TrenchChat [voice]: channel {hash_hex[:8]} has relay "
                        f"{relay[:8]}, skipping local host on restore",
                        RNS.LOG_NOTICE,
                    )
                else:
                    self.create_voice_destination(hash_hex)

    def teardown_voice_destination(self, channel_hash_hex: str) -> None:
        """Tear down the hosted voice destination for a channel.

        Disconnects all active participant links and removes the destination.
        """
        with self._lock:
            sessions = self._sessions.pop(channel_hash_hex, {})
            for session in sessions.values():
                self._teardown_participant_pipeline(session)
                try:
                    session.link.teardown()
                except Exception:
                    pass
            dest = self._hosted_destinations.pop(channel_hash_hex, None)
        if dest:
            RNS.log(
                f"TrenchChat [voice]: torn down voice destination for channel "
                f"{channel_hash_hex[:8]}",
                RNS.LOG_NOTICE,
            )

    # ------------------------------------------------------------------
    # Host-side: RNS Link handling
    # ------------------------------------------------------------------

    def _on_link_established(self, channel_hash_hex: str, link: RNS.Link) -> None:
        """Called when a participant establishes an RNS Link to our voice destination.

        Identity is not available at this point -- we register a remote_identified
        callback and defer the permission check until the client identifies itself.
        """
        RNS.log(
            f"TrenchChat [voice]: inbound RNS Link established on channel {channel_hash_hex[:8]}, "
            "waiting for remote identity",
            RNS.LOG_NOTICE,
        )
        link.set_remote_identified_callback(
            lambda lnk, identity: self._on_link_remote_identified(
                channel_hash_hex, lnk, identity
            )
        )

    def _on_link_remote_identified(self, channel_hash_hex: str,
                                   link: RNS.Link, remote_identity) -> None:
        """Called once the link initiator has identified themselves.

        This is the correct point to enforce SPEAK permission and set up the
        participant session.
        """
        identity_hex = remote_identity.hash.hex()
        RNS.log(
            f"TrenchChat [voice]: participant {identity_hex[:8]} identified on channel "
            f"{channel_hash_hex[:8]}, checking SPEAK permission",
            RNS.LOG_NOTICE,
        )

        # Core enforcement: check SPEAK permission.
        if not self._storage.has_permission(channel_hash_hex, identity_hex, SPEAK):
            RNS.log(
                f"TrenchChat [voice]: {identity_hex[:8]} lacks SPEAK permission on "
                f"{channel_hash_hex[:8]}, rejecting link",
                RNS.LOG_WARNING,
            )
            link.teardown()
            return

        display_name = self._storage.get_display_name_for_identity(identity_hex) or identity_hex[:8]
        session = _ParticipantSession(identity_hex, display_name, link)
        link.set_link_closed_callback(
            lambda lnk: self._on_link_closed(channel_hash_hex, identity_hex, lnk)
        )
        link.set_packet_callback(
            lambda data, pkt: self._on_link_packet_received(
                channel_hash_hex, identity_hex, data, pkt
            )
        )

        with self._lock:
            self._sessions.setdefault(channel_hash_hex, {})[identity_hex] = session

        self._setup_participant_pipeline(channel_hash_hex, session)
        self._broadcast_voice_state(channel_hash_hex)
        self._fire_participant_callbacks(channel_hash_hex)
        RNS.log(
            f"TrenchChat [voice]: {display_name} ({identity_hex[:8]}) joined voice on "
            f"channel {channel_hash_hex[:8]}",
            RNS.LOG_NOTICE,
        )

    def _on_link_closed(self, channel_hash_hex: str, identity_hex: str,
                        link: RNS.Link) -> None:
        """Called when a participant's link closes (they left or were disconnected)."""
        with self._lock:
            sessions = self._sessions.get(channel_hash_hex, {})
            session = sessions.pop(identity_hex, None)
        if session:
            self._teardown_participant_pipeline(session)
            self._rebuild_n1_mixers(channel_hash_hex)
            self._broadcast_voice_state(channel_hash_hex)
            self._fire_participant_callbacks(channel_hash_hex)
            RNS.log(
                f"TrenchChat [voice]: {identity_hex[:8]} left voice on channel "
                f"{channel_hash_hex[:8]}",
                RNS.LOG_NOTICE,
            )

    def _on_link_packet_received(self, channel_hash_hex: str, identity_hex: str,
                                 data: bytes, packet) -> None:
        """Route in-band voice signal packets (mute/unmute/speaking indicators)."""
        try:
            unpacked = msgpack.unpackb(data, raw=False)
            if isinstance(unpacked, dict) and F_VOICE_SIGNAL in unpacked:
                self._handle_voice_signal(
                    channel_hash_hex, identity_hex, unpacked[F_VOICE_SIGNAL]
                )
        except Exception:
            pass  # Not a signal packet; frame handled by LXST LinkSource directly

    def _handle_voice_signal(self, channel_hash_hex: str, identity_hex: str,
                              signal: int) -> None:
        """Update participant mute/speaking state from in-band signals."""
        with self._lock:
            session = self._sessions.get(channel_hash_hex, {}).get(identity_hex)
            if session is None:
                return
            if signal == VS_MUTE:
                session.is_muted = True
                session.is_speaking = False
            elif signal == VS_UNMUTE:
                session.is_muted = False
            elif signal == VS_SPEAKING:
                session.is_speaking = True
            elif signal == VS_SILENT:
                session.is_speaking = False
        self._fire_speaking_callbacks(channel_hash_hex, identity_hex)

    # ------------------------------------------------------------------
    # Host-side: LXST pipeline management
    # ------------------------------------------------------------------

    def _setup_participant_pipeline(self, channel_hash_hex: str,
                                    session: _ParticipantSession) -> None:
        """Wire up LXST LinkSource and N-1 Mixer for a newly connected participant."""
        try:
            import LXST
            from LXST.Network import LinkSource, Packetizer

            codec = LXST.Codecs.Opus(profile=_OPUS_PROFILE_DEFAULT)
            mixer = LXST.Mixer(target_frame_ms=40)
            packetizer = Packetizer(session.link)
            link_source = LinkSource(session.link, signalling_receiver=None, sink=mixer)

            session.link_source = link_source
            session.mixer = mixer
            session.packetizer = packetizer

            mixer.codec = LXST.Codecs.Opus(profile=_OPUS_PROFILE_DEFAULT)
            mixer.sink = packetizer

            link_source.start()
            mixer.start()

            # Feed existing participants' audio into this new participant's mixer
            # and feed this new participant's audio into all existing mixers (N-1).
            self._rebuild_n1_mixers(channel_hash_hex)

        except Exception as exc:
            RNS.log(
                f"TrenchChat [voice]: failed to set up participant pipeline: {exc}",
                RNS.LOG_ERROR,
            )

    def _teardown_participant_pipeline(self, session: _ParticipantSession) -> None:
        """Stop all LXST components for a participant."""
        try:
            if session.link_source:
                session.link_source.stop()
            if session.mixer:
                session.mixer.stop()
        except Exception as exc:
            RNS.log(
                f"TrenchChat [voice]: error during pipeline teardown: {exc}",
                RNS.LOG_ERROR,
            )

    def _rebuild_n1_mixers(self, channel_hash_hex: str) -> None:
        """Rebuild N-1 mixer inputs for all participants after any join/leave.

        Each participant's Mixer must contain all other participants' LinkSources
        as inputs, but NOT their own.
        """
        with self._lock:
            sessions = dict(self._sessions.get(channel_hash_hex, {}))

        for identity_hex, session in sessions.items():
            if session.mixer is None:
                continue
            # Clear existing sources from the mixer and re-add all except self.
            session.mixer.incoming_frames.clear()
            for other_hex, other_session in sessions.items():
                if other_hex != identity_hex and other_session.link_source is not None:
                    # Register the other participant's audio source with this mixer.
                    session.mixer.incoming_frames.setdefault(
                        other_session.link_source, __import__("collections").deque(maxlen=8)
                    )

    # ------------------------------------------------------------------
    # Host-side: broadcasting voice state
    # ------------------------------------------------------------------

    def _broadcast_voice_state(self, channel_hash_hex: str) -> None:
        """Send MT_VOICE_STATE to all channel subscribers with current participants."""
        with self._lock:
            sessions = dict(self._sessions.get(channel_hash_hex, {}))
            dest = self._hosted_destinations.get(channel_hash_hex)

        participant_list = list(sessions.keys())
        voice_dest_hash = dest.hash if dest else b""

        payload = msgpack.packb(participant_list, use_bin_type=True)

        # Broadcast to all members we know about.
        members = self._storage.get_members(channel_hash_hex)
        for member in members:
            member_hex = member["identity_hash"]
            if member_hex == self._identity.hash_hex:
                continue
            self._send_lxmf(member_hex, {
                F_MSG_TYPE: MT_VOICE_STATE,
                F_CHANNEL_HASH: bytes.fromhex(channel_hash_hex),
                F_VOICE_DEST_HASH: voice_dest_hash,
                F_VOICE_PARTICIPANTS: payload,
            })

    def _send_lxmf(self, dest_identity_hex: str, fields: dict) -> None:
        """Send an LXMF control message to a peer by identity hex.

        Tries two recall strategies before giving up:
        1. Recall by delivery destination hash (works after the peer announces lxmf.delivery).
        2. Recall by identity hash (works after the peer announces any destination using the
           same identity, e.g. trenchchat.relay or trenchchat.channel).
        """
        msg_type = fields.get(F_MSG_TYPE, "?")
        RNS.log(
            f"TrenchChat [voice]: _send_lxmf msg_type={msg_type!r} to {dest_identity_hex[:8]}",
            RNS.LOG_DEBUG,
        )
        delivery_dest_hash = RNS.Destination.hash(
            bytes.fromhex(dest_identity_hex), "lxmf", "delivery"
        )
        dest_identity = RNS.Identity.recall(delivery_dest_hash)
        RNS.log(
            f"TrenchChat [voice]: _send_lxmf recall by delivery_dest "
            f"{delivery_dest_hash.hex()[:8]} -> {'found' if dest_identity else 'miss'}",
            RNS.LOG_DEBUG,
        )
        if dest_identity is None:
            # Fallback: search by raw identity hash (works when the peer has announced any
            # destination using the same identity, even if not lxmf.delivery specifically).
            dest_identity = RNS.Identity.recall(
                bytes.fromhex(dest_identity_hex), from_identity_hash=True
            )
            RNS.log(
                f"TrenchChat [voice]: _send_lxmf recall by identity_hash "
                f"{dest_identity_hex[:8]} -> {'found' if dest_identity else 'miss'}",
                RNS.LOG_DEBUG,
            )
        if dest_identity is None:
            RNS.log(
                f"TrenchChat [voice]: identity unknown for {dest_identity_hex[:8]}, "
                "requesting path",
                RNS.LOG_WARNING,
            )
            try:
                RNS.Transport.request_path(delivery_dest_hash)
            except Exception:
                pass
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
            desired_method=LXMF.LXMessage.OPPORTUNISTIC,
        )
        lxm.fields = fields
        self._router.send(lxm)
        RNS.log(
            f"TrenchChat [voice]: _send_lxmf queued msg_type={msg_type!r} "
            f"to {dest_identity_hex[:8]}",
            RNS.LOG_DEBUG,
        )

    # ------------------------------------------------------------------
    # Host-side: handling inbound LXMF voice join/leave
    # ------------------------------------------------------------------

    def _handle_voice_join(self, message: LXMF.LXMessage, fields: dict) -> None:
        """Handle MT_VOICE_JOIN from a participant wanting to join voice."""
        channel_hash_bytes = fields.get(F_CHANNEL_HASH)
        if not channel_hash_bytes:
            RNS.log("TrenchChat [voice]: _handle_voice_join missing F_CHANNEL_HASH", RNS.LOG_WARNING)
            return
        channel_hash_hex = (
            channel_hash_bytes.hex() if isinstance(channel_hash_bytes, bytes)
            else channel_hash_bytes
        )

        sender_identity = RNS.Identity.recall(message.source_hash) if message.source_hash else None
        sender_hex = sender_identity.hash.hex() if sender_identity else (
            message.source_hash.hex() if message.source_hash else ""
        )
        RNS.log(
            f"TrenchChat [voice]: _handle_voice_join channel={channel_hash_hex[:8]} "
            f"from sender={sender_hex[:8]} is_hosting={self.is_hosting(channel_hash_hex)}",
            RNS.LOG_NOTICE,
        )

        if not sender_hex:
            return

        # Core enforcement: check SPEAK permission.
        if not self._storage.has_permission(channel_hash_hex, sender_hex, SPEAK):
            RNS.log(
                f"TrenchChat [voice]: {sender_hex[:8]} lacks SPEAK on "
                f"{channel_hash_hex[:8]}, ignoring voice_join",
                RNS.LOG_WARNING,
            )
            return

        with self._lock:
            dest = self._hosted_destinations.get(channel_hash_hex)
        if dest is None:
            # We are not hosting this channel. Ignore.
            return

        # Reply with current voice state so the participant knows where to connect.
        self._send_voice_state_to(channel_hash_hex, sender_hex)

    def _send_voice_state_to(self, channel_hash_hex: str, dest_hex: str) -> None:
        """Send the current MT_VOICE_STATE for a channel directly to one peer."""
        with self._lock:
            sessions = dict(self._sessions.get(channel_hash_hex, {}))
            dest = self._hosted_destinations.get(channel_hash_hex)

        participant_list = list(sessions.keys())
        voice_dest_hash = dest.hash if dest else b""
        payload = msgpack.packb(participant_list, use_bin_type=True)

        self._send_lxmf(dest_hex, {
            F_MSG_TYPE: MT_VOICE_STATE,
            F_CHANNEL_HASH: bytes.fromhex(channel_hash_hex),
            F_VOICE_DEST_HASH: voice_dest_hash,
            F_VOICE_PARTICIPANTS: payload,
        })

    def _handle_voice_leave(self, message: LXMF.LXMessage, fields: dict) -> None:
        """Handle MT_VOICE_LEAVE from a participant (graceful exit notification)."""
        # The link teardown callback handles cleanup; this is just informational.
        RNS.log("TrenchChat [voice]: received voice_leave (link teardown handles cleanup)",
                RNS.LOG_DEBUG)

    def _handle_voice_state(self, message: LXMF.LXMessage, fields: dict) -> None:
        """Handle MT_VOICE_STATE received as a participant (from host or relay)."""
        channel_hash_bytes = fields.get(F_CHANNEL_HASH)
        voice_dest_hash_bytes = fields.get(F_VOICE_DEST_HASH)
        participants_blob = fields.get(F_VOICE_PARTICIPANTS)

        if not channel_hash_bytes:
            RNS.log("TrenchChat [voice]: _handle_voice_state missing F_CHANNEL_HASH", RNS.LOG_WARNING)
            return
        channel_hash_hex = (
            channel_hash_bytes.hex() if isinstance(channel_hash_bytes, bytes)
            else channel_hash_bytes
        )

        participants: list[str] = []
        if participants_blob:
            try:
                participants = msgpack.unpackb(participants_blob, raw=False)
            except Exception:
                pass

        voice_dest_hex: str | None = None
        if isinstance(voice_dest_hash_bytes, bytes) and voice_dest_hash_bytes:
            voice_dest_hex = voice_dest_hash_bytes.hex()

        RNS.log(
            f"TrenchChat [voice]: _handle_voice_state channel={channel_hash_hex[:8]} "
            f"voice_dest={voice_dest_hex[:8] if voice_dest_hex else 'none'} "
            f"participants={len(participants)} host_only={self._host_only}",
            RNS.LOG_NOTICE,
        )

        # If we sent MT_VOICE_JOIN and are not yet connected, use the voice
        # destination from this state message to establish the RNS Link now.
        with self._lock:
            already_connected = channel_hash_hex in self._active_sessions
        RNS.log(
            f"TrenchChat [voice]: _handle_voice_state already_connected={already_connected}",
            RNS.LOG_DEBUG,
        )
        if not already_connected and voice_dest_hex and not self._host_only:
            RNS.log(
                f"TrenchChat [voice]: _handle_voice_state triggering _connect_to_voice_dest "
                f"for channel {channel_hash_hex[:8]}",
                RNS.LOG_NOTICE,
            )
            threading.Thread(
                target=self._connect_to_voice_dest,
                args=(channel_hash_hex, voice_dest_hex),
                daemon=True,
            ).start()

        for cb in self._voice_state_callbacks:
            try:
                cb(channel_hash_hex, voice_dest_hex, participants)
            except Exception as exc:
                RNS.log(f"TrenchChat [voice]: voice_state callback error: {exc}", RNS.LOG_ERROR)

    # ------------------------------------------------------------------
    # Participant-side: joining and leaving
    # ------------------------------------------------------------------

    def join_voice(self, channel_hash_hex: str,
                   voice_dest_hash_hex: str | None = None) -> None:
        """Join the voice channel by connecting to the host's voice destination.

        *voice_dest_hash_hex* is the hash of the voice RNS Destination to connect to
        (obtained from MT_VOICE_STATE or from storage relay_dest_hash).  If None,
        we first send MT_VOICE_JOIN to the channel owner to request the voice state.
        """
        RNS.log(
            f"TrenchChat [voice]: join_voice called for channel {channel_hash_hex[:8]} "
            f"host_only={self._host_only}",
            RNS.LOG_DEBUG,
        )

        if self._host_only:
            RNS.log("TrenchChat [voice]: join_voice skipped — host_only mode", RNS.LOG_DEBUG)
            return

        channel = self._storage.get_channel(channel_hash_hex)
        if channel is None:
            RNS.log(f"TrenchChat [voice]: unknown channel {channel_hash_hex[:8]}", RNS.LOG_WARNING)
            return

        with self._lock:
            if channel_hash_hex in self._active_sessions:
                RNS.log(
                    f"TrenchChat [voice]: join_voice skipped — already in session for "
                    f"channel {channel_hash_hex[:8]}",
                    RNS.LOG_DEBUG,
                )
                return  # Already joined.

        if voice_dest_hash_hex is None:
            # relay_dest_hash stores the relay's identity hex (not a voice dest hash).
            # Whether relayed or owner-hosted, send MT_VOICE_JOIN to whoever is hosting
            # to get MT_VOICE_STATE back with the actual voice RNS destination hash.
            relay_identity_hex = (
                channel["relay_dest_hash"]
                if "relay_dest_hash" in channel.keys()
                else None
            )
            host_identity_hex = relay_identity_hex or channel["creator_hash"]
            RNS.log(
                f"TrenchChat [voice]: join_voice sending MT_VOICE_JOIN for channel "
                f"{channel_hash_hex[:8]} to host {host_identity_hex[:8] if host_identity_hex else 'none'} "
                f"(relay={relay_identity_hex[:8] if relay_identity_hex else 'none'}, "
                f"owner={channel['creator_hash'][:8]})",
                RNS.LOG_NOTICE,
            )
            self._send_lxmf(host_identity_hex, {
                F_MSG_TYPE: MT_VOICE_JOIN,
                F_CHANNEL_HASH: bytes.fromhex(channel_hash_hex),
                F_DISPLAY_NAME: self._identity.display_name,
            })
            # Connection will proceed when MT_VOICE_STATE is received.
            return

        RNS.log(
            f"TrenchChat [voice]: join_voice connecting directly to voice dest "
            f"{voice_dest_hash_hex[:8]} for channel {channel_hash_hex[:8]}",
            RNS.LOG_NOTICE,
        )
        self._connect_to_voice_dest(channel_hash_hex, voice_dest_hash_hex)

    def _connect_to_voice_dest(self, channel_hash_hex: str,
                                voice_dest_hex: str) -> None:
        """Establish an RNS Link to a voice destination and set up local pipelines."""
        RNS.log(
            f"TrenchChat [voice]: _connect_to_voice_dest channel={channel_hash_hex[:8]} "
            f"voice_dest={voice_dest_hex[:8]}",
            RNS.LOG_NOTICE,
        )
        try:
            voice_dest_hash = bytes.fromhex(voice_dest_hex)
            dest_identity = RNS.Identity.recall(voice_dest_hash)
            RNS.log(
                f"TrenchChat [voice]: _connect_to_voice_dest identity recall for "
                f"{voice_dest_hex[:8]} -> {'found' if dest_identity else 'miss'}",
                RNS.LOG_NOTICE,
            )
            if dest_identity is None:
                RNS.Transport.request_path(voice_dest_hash)
                RNS.log(
                    f"TrenchChat [voice]: path to voice destination {voice_dest_hex[:8]} "
                    "unknown, requested path — retry joining in a moment",
                    RNS.LOG_WARNING,
                )
                return

            dest = RNS.Destination(
                dest_identity,
                RNS.Destination.OUT,
                RNS.Destination.SINGLE,
                APP_NAME,
                APP_ASPECT_VOICE,
                channel_hash_hex,
            )
            RNS.log(
                f"TrenchChat [voice]: opening RNS Link to voice dest {dest.hash.hex()[:8]} "
                f"for channel {channel_hash_hex[:8]}",
                RNS.LOG_NOTICE,
            )
            link = RNS.Link(dest)
            link.set_link_established_callback(
                lambda lnk: self._on_participant_link_established(
                    channel_hash_hex, lnk
                )
            )
            link.set_link_closed_callback(
                lambda lnk: self._on_participant_link_closed(channel_hash_hex, lnk)
            )

            with self._lock:
                self._active_sessions[channel_hash_hex] = {
                    "link": link,
                    "voice_dest_hex": voice_dest_hex,
                    "outbound_pipeline": None,
                    "inbound_pipeline": None,
                    "vad_filter": None,
                }
            RNS.log(
                f"TrenchChat [voice]: RNS Link initiated to {voice_dest_hex[:8]}, "
                "waiting for link_established callback",
                RNS.LOG_NOTICE,
            )

        except Exception as exc:
            import traceback
            RNS.log(
                f"TrenchChat [voice]: error connecting to voice destination: {exc}\n"
                f"{traceback.format_exc()}",
                RNS.LOG_ERROR,
            )

    def _on_participant_link_established(self, channel_hash_hex: str,
                                         link: RNS.Link) -> None:
        """Link to host is established -- identify ourselves then start audio pipelines."""
        RNS.log(
            f"TrenchChat [voice]: RNS Link established to host for channel "
            f"{channel_hash_hex[:8]}, identifying and setting up audio pipelines",
            RNS.LOG_NOTICE,
        )
        # Identify ourselves so the host can verify SPEAK permission.
        link.identify(self._identity.rns_identity)

        try:
            import LXST
            from LXST.Network import Packetizer, LinkSource

            packetizer = Packetizer(link)
            line_sink = LXST.Sinks.LineSink()

            mic_mode = self._mic_mode.get(channel_hash_hex, "ptt")
            vad_filter = None

            if mic_mode == "vad":
                from trenchchat.core.vad import VadFilter
                vad_filter = VadFilter()
                vad_filter.add_speaking_callback(
                    lambda speaking: self._on_vad_speaking_changed(
                        channel_hash_hex, speaking
                    )
                )
                line_source = LXST.Sources.LineSource(filters=[vad_filter])
            else:
                line_source = LXST.Sources.LineSource()

            # Use LXST.Pipeline to wire source → codec → sink correctly.
            # Pipeline sets codec.source = source so the encoder knows the
            # input sample rate for resampling.
            outbound_pipeline = LXST.Pipeline(
                source=line_source,
                codec=LXST.Codecs.Opus(profile=_OPUS_PROFILE_DEFAULT),
                sink=packetizer,
            )

            link_source = LinkSource(link, signalling_receiver=None, sink=line_sink)
            inbound_pipeline = LXST.Pipeline(
                source=link_source,
                codec=LXST.Codecs.Opus(profile=_OPUS_PROFILE_DEFAULT),
                sink=line_sink,
            )

            if self._muted.get(channel_hash_hex, False):
                line_source_started = False
            elif mic_mode == "vad":
                outbound_pipeline.start()
                line_source_started = True
            else:
                # PTT mode: pipeline starts only when PTT key is pressed.
                line_source_started = False

            link_source.start()

            with self._lock:
                session = self._active_sessions.get(channel_hash_hex, {})
                session.update({
                    "link_source": link_source,
                    "line_source": line_source,
                    "line_sink": line_sink,
                    "outbound_pipeline": outbound_pipeline,
                    "inbound_pipeline": inbound_pipeline,
                    "packetizer": packetizer,
                    "vad_filter": vad_filter,
                    "line_source_started": line_source_started,
                })

            RNS.log(
                f"TrenchChat [voice]: joined voice on channel {channel_hash_hex[:8]} "
                f"(mode: {mic_mode})",
                RNS.LOG_NOTICE,
            )
            for cb in self._participant_callbacks:
                try:
                    cb(channel_hash_hex)
                except Exception as exc:
                    RNS.log(f"TrenchChat [voice]: participant callback error: {exc}",
                            RNS.LOG_ERROR)

        except Exception as exc:
            import traceback
            RNS.log(
                f"TrenchChat [voice]: failed to set up participant pipelines: {exc}\n"
                f"{traceback.format_exc()}",
                RNS.LOG_ERROR,
            )

    def _on_participant_link_closed(self, channel_hash_hex: str, link: RNS.Link) -> None:
        """Clean up local pipelines when our link to the host closes."""
        self._teardown_participant_session(channel_hash_hex)
        for cb in self._participant_callbacks:
            try:
                cb(channel_hash_hex)
            except Exception as exc:
                RNS.log(f"TrenchChat [voice]: participant callback error: {exc}", RNS.LOG_ERROR)
        RNS.log(
            f"TrenchChat [voice]: disconnected from voice on channel {channel_hash_hex[:8]}",
            RNS.LOG_NOTICE,
        )

    def leave_voice(self, channel_hash_hex: str) -> None:
        """Leave an active voice session."""
        if self._host_only:
            return
        with self._lock:
            session = self._active_sessions.get(channel_hash_hex)
        if not session:
            return

        # Inform the host gracefully.
        channel = self._storage.get_channel(channel_hash_hex)
        if channel:
            owner_hex = channel["creator_hash"]
            self._send_lxmf(owner_hex, {
                F_MSG_TYPE: MT_VOICE_LEAVE,
                F_CHANNEL_HASH: bytes.fromhex(channel_hash_hex),
            })

        link = session.get("link")
        if link:
            try:
                link.teardown()
            except Exception:
                pass

        self._teardown_participant_session(channel_hash_hex)

    def _teardown_participant_session(self, channel_hash_hex: str) -> None:
        """Stop and remove all local pipeline objects for a participant session."""
        with self._lock:
            session = self._active_sessions.pop(channel_hash_hex, None)
        if not session:
            return
        for key in ("line_source", "link_source", "inbound_pipeline"):
            obj = session.get(key)
            if obj:
                try:
                    obj.stop()
                except Exception:
                    pass
        vad = session.get("vad_filter")
        if vad:
            vad.reset()

    # ------------------------------------------------------------------
    # Participant-side: PTT and VAD controls
    # ------------------------------------------------------------------

    def set_ptt_active(self, channel_hash_hex: str, active: bool) -> None:
        """Start or stop transmitting audio based on PTT key state."""
        if self._host_only:
            return
        with self._lock:
            self._ptt_active[channel_hash_hex] = active
            session = self._active_sessions.get(channel_hash_hex, {})
            mic_mode = self._mic_mode.get(channel_hash_hex, "ptt")
            if mic_mode != "ptt":
                return
            line_source = session.get("line_source")
            muted = self._muted.get(channel_hash_hex, False)

        if line_source is None or muted:
            return

        if active and not session.get("line_source_started", False):
            line_source.start()
            session["line_source_started"] = True
            self._send_voice_signal(channel_hash_hex, VS_SPEAKING)
        elif not active and session.get("line_source_started", False):
            line_source.stop()
            session["line_source_started"] = False
            self._send_voice_signal(channel_hash_hex, VS_SILENT)

    def set_muted(self, channel_hash_hex: str, muted: bool) -> None:
        """Mute or unmute the local microphone for a voice session."""
        if self._host_only:
            return
        with self._lock:
            self._muted[channel_hash_hex] = muted
            session = self._active_sessions.get(channel_hash_hex, {})
            line_source = session.get("line_source")

        signal = VS_MUTE if muted else VS_UNMUTE
        self._send_voice_signal(channel_hash_hex, signal)

        if line_source is None:
            return
        if muted and session.get("line_source_started", False):
            line_source.stop()
            session["line_source_started"] = False
        elif not muted:
            mic_mode = self._mic_mode.get(channel_hash_hex, "ptt")
            if mic_mode == "vad" and not session.get("line_source_started", False):
                line_source.start()
                session["line_source_started"] = True

    def set_deafened(self, channel_hash_hex: str, deafened: bool) -> None:
        """Mute or unmute local speaker output for a voice session."""
        if self._host_only:
            return
        with self._lock:
            self._deafened[channel_hash_hex] = deafened
            session = self._active_sessions.get(channel_hash_hex, {})
            line_sink = session.get("line_sink")

        if line_sink is None:
            return
        if deafened:
            line_sink.mute(True) if hasattr(line_sink, "mute") else None
        else:
            line_sink.mute(False) if hasattr(line_sink, "mute") else None

    def set_mic_mode(self, channel_hash_hex: str, mode: str) -> None:
        """Set mic mode for a channel. mode is 'ptt' or 'vad'.

        This takes effect on the next join_voice call.  Changing mode mid-session
        requires leaving and rejoining the voice channel.
        """
        if mode not in ("ptt", "vad"):
            raise ValueError(f"Invalid mic mode: {mode!r}")
        with self._lock:
            self._mic_mode[channel_hash_hex] = mode

    def _on_vad_speaking_changed(self, channel_hash_hex: str, speaking: bool) -> None:
        """Called by the VadFilter when speech state changes."""
        signal = VS_SPEAKING if speaking else VS_SILENT
        self._send_voice_signal(channel_hash_hex, signal)
        self._fire_speaking_callbacks(channel_hash_hex, self._identity.hash_hex)

    def _send_voice_signal(self, channel_hash_hex: str, signal: int) -> None:
        """Send an in-band signal over the RNS Link to the host."""
        with self._lock:
            session = self._active_sessions.get(channel_hash_hex, {})
            link = session.get("link")
        if link and link.status == RNS.Link.ACTIVE:
            try:
                data = msgpack.packb({F_VOICE_SIGNAL: signal}, use_bin_type=True)
                packet = RNS.Packet(link, data, create_receipt=False)
                packet.send()
            except Exception as exc:
                RNS.log(f"TrenchChat [voice]: error sending voice signal: {exc}", RNS.LOG_DEBUG)

    # ------------------------------------------------------------------
    # Relay assignment (owner-side)
    # ------------------------------------------------------------------

    def assign_relay(self, channel_hash_hex: str, relay_identity_hex: str) -> None:
        """Delegate voice hosting for a channel to a Voice Relay node.

        Core enforcement: the local identity must have MANAGE_RELAY permission.
        Sends MT_RELAY_ASSIGN with a signed token to the relay's LXMF address.
        """
        if not self._storage.has_permission(channel_hash_hex, self._identity.hash_hex,
                                             MANAGE_RELAY):
            RNS.log(
                "TrenchChat [voice]: assign_relay called without MANAGE_RELAY permission",
                RNS.LOG_WARNING,
            )
            return

        relay_delivery_hash = RNS.Destination.hash(
            bytes.fromhex(relay_identity_hex), "lxmf", "delivery"
        )
        timestamp = time.time()
        token = _make_relay_token(
            self._identity.rns_identity,
            relay_delivery_hash,
            bytes.fromhex(channel_hash_hex),
            timestamp,
        )

        member_list_row = self._storage.get_member_list_version(channel_hash_hex)
        member_list_doc = member_list_row["document_blob"] if member_list_row else b""

        self._send_lxmf(relay_identity_hex, {
            F_MSG_TYPE: MT_RELAY_ASSIGN,
            F_CHANNEL_HASH: bytes.fromhex(channel_hash_hex),
            F_RELAY_DEST_HASH: relay_delivery_hash,
            F_RELAY_TOKEN: token,
            0x03: timestamp,          # re-use F_TIMESTAMP slot for token ts
            F_MEMBER_LIST_DOC: member_list_doc,
        })
        RNS.log(
            f"TrenchChat [voice]: sent relay_assign for channel {channel_hash_hex[:8]} "
            f"to relay {relay_identity_hex[:8]}",
            RNS.LOG_NOTICE,
        )

    def revoke_relay(self, channel_hash_hex: str) -> None:
        """Revoke the current relay assignment for a channel.

        Core enforcement: requires MANAGE_RELAY permission.
        """
        if not self._storage.has_permission(channel_hash_hex, self._identity.hash_hex,
                                             MANAGE_RELAY):
            RNS.log(
                "TrenchChat [voice]: revoke_relay called without MANAGE_RELAY permission",
                RNS.LOG_WARNING,
            )
            return

        channel = self._storage.get_channel(channel_hash_hex)
        if channel is None:
            return
        relay_dest_hash = channel["relay_dest_hash"] if "relay_dest_hash" in channel.keys() else None
        if not relay_dest_hash:
            return

        # We need the relay's identity hex to send to.  Derive from the stored dest hash.
        relay_identity_hex = relay_dest_hash  # stored as identity hex by MT_RELAY_ACCEPT handler

        timestamp = time.time()
        relay_delivery_hash = RNS.Destination.hash(
            bytes.fromhex(relay_identity_hex), "lxmf", "delivery"
        )
        token = _make_relay_token(
            self._identity.rns_identity,
            relay_delivery_hash,
            bytes.fromhex(channel_hash_hex),
            timestamp,
        )

        self._send_lxmf(relay_identity_hex, {
            F_MSG_TYPE: MT_RELAY_REVOKE,
            F_CHANNEL_HASH: bytes.fromhex(channel_hash_hex),
            F_RELAY_TOKEN: token,
            0x03: timestamp,
        })
        self._storage.set_channel_relay(channel_hash_hex, None)
        RNS.log(
            f"TrenchChat [voice]: revoked relay for channel {channel_hash_hex[:8]}",
            RNS.LOG_NOTICE,
        )

    def push_member_list_to_relay(self, channel_hash_hex: str) -> None:
        """Send the current signed member list to the relay for a channel."""
        channel = self._storage.get_channel(channel_hash_hex)
        if channel is None:
            return
        relay_dest_hash = channel["relay_dest_hash"] if "relay_dest_hash" in channel.keys() else None
        if not relay_dest_hash:
            return

        member_list_row = self._storage.get_member_list_version(channel_hash_hex)
        if not member_list_row:
            return

        self._send_lxmf(relay_dest_hash, {
            F_MSG_TYPE: MT_RELAY_MEMBER_UPDATE,
            F_CHANNEL_HASH: bytes.fromhex(channel_hash_hex),
            F_MEMBER_LIST_DOC: member_list_row["document_blob"],
        })

    # ------------------------------------------------------------------
    # Relay-side: handling assignment, acceptance, revocation
    # ------------------------------------------------------------------

    def _handle_relay_assign(self, message: LXMF.LXMessage, fields: dict) -> None:
        """Handle MT_RELAY_ASSIGN (relay daemon receives this from a channel owner)."""
        channel_hash_bytes = fields.get(F_CHANNEL_HASH)
        token = fields.get(F_RELAY_TOKEN)
        relay_dest_hash_bytes = fields.get(F_RELAY_DEST_HASH)
        timestamp = fields.get(0x03, 0.0)
        member_list_doc = fields.get(F_MEMBER_LIST_DOC, b"")

        if not channel_hash_bytes or not token or not relay_dest_hash_bytes:
            RNS.log("TrenchChat [voice]: relay_assign missing required fields", RNS.LOG_WARNING)
            return

        channel_hash_hex = (
            channel_hash_bytes.hex() if isinstance(channel_hash_bytes, bytes)
            else channel_hash_bytes
        )

        sender_identity = RNS.Identity.recall(message.source_hash) if message.source_hash else None
        if sender_identity is None:
            RNS.log("TrenchChat [voice]: relay_assign sender identity unknown", RNS.LOG_WARNING)
            return

        relay_dest_hash = (
            relay_dest_hash_bytes if isinstance(relay_dest_hash_bytes, bytes)
            else bytes.fromhex(relay_dest_hash_bytes)
        )

        # Verify the token was signed by the sender (claimed owner).
        if not _verify_relay_token(sender_identity, token, relay_dest_hash,
                                    bytes.fromhex(channel_hash_hex), float(timestamp)):
            RNS.log(
                "TrenchChat [voice]: relay_assign token verification failed, rejecting",
                RNS.LOG_WARNING,
            )
            return

        owner_hex = sender_identity.hash.hex()

        # Store channel config in relay storage if available.
        if hasattr(self._storage, "upsert_relay_channel"):
            self._storage.upsert_relay_channel(
                channel_hash=channel_hash_hex,
                owner_hash=owner_hex,
                member_list_doc=member_list_doc,
            )

        # Create voice destination and reply with its hash.
        voice_dest_hex = self.create_voice_destination(channel_hash_hex)
        if voice_dest_hex is None:
            RNS.log("TrenchChat [voice]: relay failed to create voice destination", RNS.LOG_ERROR)
            return

        self._send_lxmf(owner_hex, {
            F_MSG_TYPE: MT_RELAY_ACCEPT,
            F_CHANNEL_HASH: bytes.fromhex(channel_hash_hex),
            F_VOICE_DEST_HASH: bytes.fromhex(voice_dest_hex),
        })
        RNS.log(
            f"TrenchChat [voice]: relay accepted channel {channel_hash_hex[:8]} "
            f"from owner {owner_hex[:8]}",
            RNS.LOG_NOTICE,
        )

    def _handle_relay_accept(self, message: LXMF.LXMessage, fields: dict) -> None:
        """Handle MT_RELAY_ACCEPT (owner receives this after assigning a relay)."""
        channel_hash_bytes = fields.get(F_CHANNEL_HASH)
        voice_dest_hash_bytes = fields.get(F_VOICE_DEST_HASH)

        if not channel_hash_bytes or not voice_dest_hash_bytes:
            return

        channel_hash_hex = (
            channel_hash_bytes.hex() if isinstance(channel_hash_bytes, bytes)
            else channel_hash_bytes
        )
        sender_identity = RNS.Identity.recall(message.source_hash) if message.source_hash else None
        if sender_identity is not None:
            sender_hex = sender_identity.hash.hex()
        elif message.source_hash:
            # Identity not yet in routing table — use the source hash hex as a
            # placeholder so relay_dest_hash is non-empty and restore_voice_destinations
            # won't spin up a local host on next startup.
            sender_hex = message.source_hash.hex()
            RNS.log(
                f"TrenchChat [voice]: relay identity unknown for source "
                f"{sender_hex[:8]}, storing source hash as relay marker",
                RNS.LOG_WARNING,
            )
        else:
            sender_hex = ""

        if not sender_hex:
            RNS.log(
                "TrenchChat [voice]: relay_accept has no source hash, cannot record relay",
                RNS.LOG_WARNING,
            )
            return

        # Store the relay's identity hex so we can revoke later and push member updates.
        self._storage.set_channel_relay(channel_hash_hex, sender_hex)

        # Tear down any local voice destination (relay is now hosting).
        self.teardown_voice_destination(channel_hash_hex)

        RNS.log(
            f"TrenchChat [voice]: relay accepted for channel {channel_hash_hex[:8]}, "
            f"relay identity {sender_hex[:8]}",
            RNS.LOG_NOTICE,
        )
        # Broadcast the new voice state to channel members.
        voice_dest_hash = (
            voice_dest_hash_bytes if isinstance(voice_dest_hash_bytes, bytes)
            else bytes.fromhex(voice_dest_hash_bytes)
        )
        members = self._storage.get_members(channel_hash_hex)
        for member in members:
            member_hex = member["identity_hash"]
            if member_hex == self._identity.hash_hex:
                continue
            self._send_lxmf(member_hex, {
                F_MSG_TYPE: MT_VOICE_STATE,
                F_CHANNEL_HASH: bytes.fromhex(channel_hash_hex),
                F_VOICE_DEST_HASH: voice_dest_hash,
                F_VOICE_PARTICIPANTS: msgpack.packb([], use_bin_type=True),
            })

    def _handle_relay_revoke(self, message: LXMF.LXMessage, fields: dict) -> None:
        """Handle MT_RELAY_REVOKE (relay receives this from the channel owner)."""
        channel_hash_bytes = fields.get(F_CHANNEL_HASH)
        token = fields.get(F_RELAY_TOKEN)
        timestamp = fields.get(0x03, 0.0)

        if not channel_hash_bytes or not token:
            return

        channel_hash_hex = (
            channel_hash_bytes.hex() if isinstance(channel_hash_bytes, bytes)
            else channel_hash_bytes
        )

        sender_identity = RNS.Identity.recall(message.source_hash) if message.source_hash else None
        if sender_identity is None:
            return

        delivery_hash = RNS.Destination.hash(
            self._identity.rns_identity.hash, "lxmf", "delivery"
        )
        if not _verify_relay_token(sender_identity, token, delivery_hash,
                                    bytes.fromhex(channel_hash_hex), float(timestamp)):
            RNS.log("TrenchChat [voice]: relay_revoke token invalid, ignoring", RNS.LOG_WARNING)
            return

        self.teardown_voice_destination(channel_hash_hex)
        if hasattr(self._storage, "delete_relay_channel"):
            self._storage.delete_relay_channel(channel_hash_hex)
        RNS.log(
            f"TrenchChat [voice]: relay revoked for channel {channel_hash_hex[:8]}",
            RNS.LOG_NOTICE,
        )

    def _handle_relay_member_update(self, message: LXMF.LXMessage, fields: dict) -> None:
        """Handle MT_RELAY_MEMBER_UPDATE (relay receives updated member list from owner)."""
        channel_hash_bytes = fields.get(F_CHANNEL_HASH)
        member_list_doc = fields.get(F_MEMBER_LIST_DOC, b"")

        if not channel_hash_bytes:
            return
        channel_hash_hex = (
            channel_hash_bytes.hex() if isinstance(channel_hash_bytes, bytes)
            else channel_hash_bytes
        )

        if hasattr(self._storage, "update_relay_member_list"):
            self._storage.update_relay_member_list(channel_hash_hex, member_list_doc)
            RNS.log(
                f"TrenchChat [voice]: updated relay member list for channel {channel_hash_hex[:8]}",
                RNS.LOG_NOTICE,
            )

    # ------------------------------------------------------------------
    # State queries
    # ------------------------------------------------------------------

    def is_in_voice(self, channel_hash_hex: str) -> bool:
        """Return True if the local user is currently in a voice session."""
        with self._lock:
            return channel_hash_hex in self._active_sessions

    def is_hosting(self, channel_hash_hex: str) -> bool:
        """Return True if this node is hosting the voice destination for a channel."""
        with self._lock:
            return channel_hash_hex in self._hosted_destinations

    def get_participants(self, channel_hash_hex: str) -> list[dict]:
        """Return current participant state for a hosted channel."""
        with self._lock:
            sessions = dict(self._sessions.get(channel_hash_hex, {}))
        return [
            {
                "identity_hex": s.identity_hex,
                "display_name": s.display_name,
                "is_muted": s.is_muted,
                "is_speaking": s.is_speaking,
                "joined_at": s.joined_at,
            }
            for s in sessions.values()
        ]

    def get_mic_mode(self, channel_hash_hex: str) -> str:
        """Return the current mic mode ('ptt' or 'vad') for a channel."""
        return self._mic_mode.get(channel_hash_hex, "ptt")

    def is_muted(self, channel_hash_hex: str) -> bool:
        """Return True if the local mic is muted for a channel."""
        return self._muted.get(channel_hash_hex, False)

    def is_deafened(self, channel_hash_hex: str) -> bool:
        """Return True if local speaker output is muted for a channel."""
        return self._deafened.get(channel_hash_hex, False)

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def add_participant_changed_callback(self, callback) -> None:
        """Register callback(channel_hash_hex) fired when participant list changes."""
        if callback not in self._participant_callbacks:
            self._participant_callbacks.append(callback)

    def remove_participant_changed_callback(self, callback) -> None:
        if callback in self._participant_callbacks:
            self._participant_callbacks.remove(callback)

    def add_speaking_changed_callback(self, callback) -> None:
        """Register callback(channel_hash_hex, identity_hex) fired on speaking state change."""
        if callback not in self._speaking_callbacks:
            self._speaking_callbacks.append(callback)

    def remove_speaking_changed_callback(self, callback) -> None:
        if callback in self._speaking_callbacks:
            self._speaking_callbacks.remove(callback)

    def add_voice_state_callback(self, callback) -> None:
        """Register callback(channel_hash_hex, voice_dest_hex, participants) for MT_VOICE_STATE."""
        if callback not in self._voice_state_callbacks:
            self._voice_state_callbacks.append(callback)

    def remove_voice_state_callback(self, callback) -> None:
        if callback in self._voice_state_callbacks:
            self._voice_state_callbacks.remove(callback)

    def _fire_participant_callbacks(self, channel_hash_hex: str) -> None:
        for cb in self._participant_callbacks:
            try:
                cb(channel_hash_hex)
            except Exception as exc:
                RNS.log(f"TrenchChat [voice]: participant callback error: {exc}", RNS.LOG_ERROR)

    def _fire_speaking_callbacks(self, channel_hash_hex: str, identity_hex: str) -> None:
        for cb in self._speaking_callbacks:
            try:
                cb(channel_hash_hex, identity_hex)
            except Exception as exc:
                RNS.log(f"TrenchChat [voice]: speaking callback error: {exc}", RNS.LOG_ERROR)
