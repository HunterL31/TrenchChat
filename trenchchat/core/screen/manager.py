"""
Screen share sessions: the sharer's fan-out and the viewer's watch.

A share lives inside a voice session and travels over direct sessions only.
The sharer captures one monitor on a thread of its own, encodes each frame
once (core/screen/encoder.py) and sends every viewer the update that brings
it up to date, one or two in flight per viewer and never more: the viewer's
acknowledgement is the credit for the next, so a slow viewer gets fewer,
larger updates and a static screen sends nothing. A viewer holds the latest
bytes per tile for the client sockets that show them, each on its own credit.

Nothing here calls Router.send, whose retry falls back onto the mesh, and no
protocol field exists for any of it: the plane (network/ip/screen_plane.py)
is the only way out, and it goes nowhere without a session. That is the
whole of decision 1 in docs/screen-share-plan.md, and tests/test_screen.py
pins it.

Enforcement is at both ends, on the identity the session proved: a sharer
admits a viewer that is in its voice session and allowed to voice there; a
participant records a share only from a peer with screen_share on that
channel; and the once-a-second sweep drops whichever side stops qualifying.

Callbacks fire on background threads. The API layer marshals them.
"""

import threading
import time

import RNS

from trenchchat.core.cadence import Cadence
from trenchchat.core.permissions import (
    SCREEN_SHARE, is_open_join, permissions_from_json,
)
from trenchchat.core.screen import screen_available
from trenchchat.core.screen.capture import MonitorSource
from trenchchat.core.screen.encoder import (
    Consumer, DEFAULT_PRESET, TileEncoder, TileStore, next_update,
    preset_settings,
)
from trenchchat.core.voice import SESSION_LEFT
from trenchchat.network.base import PATH_DIRECT
from trenchchat.network.ip.screen_plane import (
    REASON_FORBIDDEN, REASON_FULL, REASON_NOT_IN_VOICE, REASON_NOT_SHARING,
)
from trenchchat.network.screen_wire import ScreenUpdate, TILE_SHIFT

# Updates a viewer may have unacknowledged before the sharer waits for it.
VIEWER_WINDOW = 2
VIEWER_ACK_TIMEOUT_SECS = 10.0
MAX_SCREEN_VIEWERS = 4

# How long start_share waits for the capture thread to open its source, and
# how long watch waits for the sharer's answer.
SOURCE_OPEN_TIMEOUT_SECS = 5.0
WATCH_TIMEOUT_SECS = 10.0

# A participant that refused a started is told again after this, since it may
# have joined the voice session or learned the sharer's role since.
TOLD_RETRY_SECS = 5.0

# A share recorded before its sharer's voice join has landed is kept this long
# before the roster is asked whether the sharer is really there.
HELD_GRACE_SECS = 10.0

REASON_NOT_IN_VOICE_SELF = "not_in_voice"
REASON_NO_PERMISSION = "no_permission"
REASON_ALREADY_SHARING = "already_sharing"
REASON_NO_DIRECT = "no_direct"
REASON_CAPTURE_UNAVAILABLE = "capture_unavailable"
REASON_NO_SHARE = "no_share"
REASON_NO_SESSION = "no_session"
REASON_NO_ANSWER = "no_answer"
REASON_SESSION_LOST = "session_lost"
REASON_STOPPED = "stopped"
REASON_VOICE_LEFT = "voice_left"

SESSION_STARTED = "started"
SESSION_STOPPED = "stopped"
SESSION_ERROR = "error"

SHARE_STARTED = "started"
SHARE_STOPPED = "stopped"


class _Viewer:
    """One peer this node is sending its screen to."""

    def __init__(self, peer_hex: str, max_width: int, max_height: int):
        self.peer_hex = peer_hex
        self.consumer = Consumer()
        self.credit = VIEWER_WINDOW
        self.last_ack = time.time()
        self.since = self.last_ack
        self.max_width = max_width
        self.max_height = max_height
        self.updates = 0
        self.bytes = 0

    def stats(self) -> dict:
        return {"peer": self.peer_hex, "since": self.since,
                "updates": self.updates, "bytes": self.bytes}


class _Share:
    """This node's outbound share, for its whole life."""

    def __init__(self, channel_hex: str, source, source_label: str,
                 preset: str, settings: dict):
        self.channel_hex = channel_hex
        self.source = source
        self.source_label = source_label
        self.preset = preset
        self.settings = settings
        self.fps = settings["fps"]
        self.encoder: TileEncoder | None = None
        self.store = TileStore()
        self.viewers: dict[str, _Viewer] = {}
        self.told: dict[str, tuple[bool, float]] = {}
        self.started_at = time.time()
        self.stop_event = threading.Event()
        self.ready = threading.Event()
        self.error = ""
        self.thread: threading.Thread | None = None

    @property
    def size(self) -> tuple[int, int]:
        return self.encoder.size if self.encoder is not None else (0, 0)


class _Held:
    """A share another peer told this node about."""

    def __init__(self, peer_hex: str, info: dict):
        self.peer_hex = peer_hex
        self.channel_hex = info["channel"]
        self.width = info["width"]
        self.height = info["height"]
        self.tile_shift = info["tile_shift"]
        self.fps = info["fps"]
        self.since = time.time()

    def as_dict(self) -> dict:
        return {"peer": self.peer_hex, "channel": self.channel_hex,
                "width": self.width, "height": self.height, "fps": self.fps,
                "since": self.since}


class ScreenShareManager:
    """Sharer and viewer halves of screen share, over the direct plane only."""

    def __init__(self, identity, storage, router, voice_mgr, *, plane=None,
                 source_factory=None, capture_probe=None):
        """
        plane: the IPScreenTransport over this node's direct sessions, or None
        when direct connections are off, in which case nothing can be shared
        or watched and every call says so.
        source_factory(monitor) -> ScreenSource; the default reads a monitor
        through mss. capture_probe() -> (ok, reason) says whether it could.
        """
        self._identity = identity
        self._storage = storage
        self._router = router
        self._voice = voice_mgr
        self._plane = plane
        self._source_factory = source_factory or MonitorSource
        self._capture_probe = capture_probe or screen_available
        self._lock = threading.RLock()

        self._share: _Share | None = None
        self._held: dict[str, _Held] = {}
        self._watching: str | None = None
        self._watch_pending: str | None = None
        self._watch_result: tuple[bool, str] | None = None
        self._watch_event = threading.Event()
        self._store = TileStore()
        self._clients: dict[int, Consumer] = {}
        self._next_client = 0
        self._watch_updates = 0
        self._watch_bytes = 0
        self._watch_since = 0.0

        self._share_callbacks: list = []
        self._session_callbacks: list = []
        self._viewers_callbacks: list = []
        self._update_callbacks: list = []
        self._watch_callbacks: list = []

        if plane is not None:
            plane.set_started_callback(self._on_started)
            plane.set_stopped_callback(self._on_stopped)
            plane.set_watch_callback(self._on_watch)
            plane.set_unwatch_callback(self._on_unwatch)
            plane.set_update_callback(self._on_update)
        router.add_path_changed_callback(self._on_path_changed)
        voice_mgr.add_session_callback(self._on_voice_session)
        voice_mgr.add_roster_callback(self._on_voice_roster)

    # --- permissions: the core layer ---

    def may_share(self, channel_hash_hex: str, peer_hex: str) -> bool:
        """Whether a peer may share its screen in this channel's voice session.

        Unknown channels fail closed; open-join channels have no member table,
        so any authenticated sender is allowed, the reading voice_chat has.
        """
        channel = self._storage.get_channel(channel_hash_hex)
        if channel is None:
            return False
        perms = permissions_from_json(channel["permissions"])
        if is_open_join(perms):
            return True
        if not self._storage.is_member(channel_hash_hex, peer_hex):
            return False
        return self._storage.has_permission(channel_hash_hex, peer_hex,
                                            SCREEN_SHARE)

    # --- the sharer ---

    def start_share(self, channel_hash_hex: str, *, monitor: int = 1,
                    preset: str = DEFAULT_PRESET, fps: int | None = None,
                    source=None, source_label: str = "") -> str | None:
        """Share a monitor with the channel's voice session. The reason it
        could not start, or None once frames are flowing to the encoder."""
        if self._plane is None:
            return REASON_NO_DIRECT
        if self._voice.current_channel != channel_hash_hex:
            return REASON_NOT_IN_VOICE_SELF
        if not self.may_share(channel_hash_hex, self._identity.hash_hex):
            return REASON_NO_PERMISSION
        if source is None:
            available, reason = self._capture_probe()
            if not available:
                return REASON_CAPTURE_UNAVAILABLE
            source = self._source_factory(monitor)
            source_label = source_label or f"Monitor {monitor}"
        with self._lock:
            if self._share is not None:
                return REASON_ALREADY_SHARING
            share = _Share(channel_hash_hex, source, source_label, preset,
                           preset_settings(preset, fps))
            self._share = share
        share.thread = threading.Thread(target=self._capture_loop, args=(share,),
                                        daemon=True, name="screen-capture")
        share.thread.start()
        if not share.ready.wait(SOURCE_OPEN_TIMEOUT_SECS) or share.error:
            with self._lock:
                if self._share is share:
                    self._share = None
            share.stop_event.set()
            RNS.log(f"TrenchChat [screen]: capture did not start: "
                    f"{share.error or 'timed out'}", RNS.LOG_WARNING)
            self._notify_session(SESSION_ERROR, share.error or "capture timed out")
            return REASON_CAPTURE_UNAVAILABLE
        RNS.log(f"TrenchChat [screen]: sharing {source_label} at "
                f"{share.size[0]}x{share.size[1]}, {share.fps} fps",
                RNS.LOG_NOTICE)
        self._notify_session(SESSION_STARTED, "")
        self._tell_participants(share)
        return None

    def stop_share(self, reason: str = "") -> bool:
        """End this node's share, telling everyone it told. False if none."""
        with self._lock:
            share = self._share
            self._share = None
        if share is None:
            return False
        share.stop_event.set()
        for peer_hex, (told, _at) in list(share.told.items()):
            if told and self._plane is not None:
                self._plane.send_stopped(peer_hex, share.channel_hex)
        if share.thread is not None and \
                share.thread is not threading.current_thread():
            share.thread.join(timeout=2.0)
        RNS.log(f"TrenchChat [screen]: share ended"
                f"{': ' + reason if reason else ''}", RNS.LOG_NOTICE)
        self._notify_viewers(0)
        self._notify_session(SESSION_STOPPED, reason)
        return True

    def sharing(self) -> dict | None:
        """What this node is sharing, for the client."""
        with self._lock:
            share = self._share
            if share is None:
                return None
            width, height = share.size
            return {
                "channel": share.channel_hex,
                "source": share.source_label,
                "preset": share.preset,
                "fps": share.fps,
                "width": width,
                "height": height,
                "since": share.started_at,
                "viewers": [v.stats() for v in share.viewers.values()],
                "encoder": share.encoder.stats() if share.encoder else {},
            }

    def _capture_loop(self, share: _Share) -> None:
        """The capture thread: open, grab, encode, fan out, until stopped."""
        try:
            width, height = share.source.open()
            share.encoder = TileEncoder(
                width, height, max_width=share.settings["max_width"],
                max_height=share.settings["max_height"])
        except Exception as e:
            share.error = f"capture failed to open: {e}"
            share.ready.set()
            return
        share.ready.set()
        cadence = Cadence(1.0 / share.fps)
        try:
            while not share.stop_event.is_set():
                cadence.wait()
                if share.stop_event.is_set():
                    break
                frame = share.source.grab()
                update = share.encoder.encode(frame)
                if update is None:
                    continue
                with self._lock:
                    share.store.apply(update)
                    for viewer in share.viewers.values():
                        viewer.consumer.note(update)
                self._pump(share)
        except Exception as e:
            RNS.log(f"TrenchChat [screen]: capture stopped: {e}", RNS.LOG_ERROR)
            share.error = str(e)
            with self._lock:
                still = self._share is share
            if still:
                self.stop_share(f"capture failed: {e}")
                self._notify_session(SESSION_ERROR, str(e))
        finally:
            try:
                share.source.close()
            except Exception as e:
                RNS.log(f"TrenchChat [screen]: closing the source: {e}",
                        RNS.LOG_DEBUG)

    def _pump(self, share: _Share) -> None:
        """Send every viewer with credit the update that brings it up to date."""
        while True:
            with self._lock:
                if self._share is not share:
                    return
                pending = []
                for viewer in share.viewers.values():
                    if viewer.credit <= 0 or not viewer.consumer.behind:
                        continue
                    update = next_update(share.store, viewer.consumer)
                    if update is None:
                        continue
                    viewer.credit -= 1
                    viewer.updates += 1
                    viewer.bytes += update.payload_bytes
                    pending.append((viewer, update))
            if not pending:
                return
            for viewer, update in pending:
                self._send_update(share, viewer, update)

    def _send_update(self, share: _Share, viewer: _Viewer,
                     update: ScreenUpdate) -> None:
        def _acked(ok: bool, _body: dict) -> None:
            with self._lock:
                if self._share is not share or \
                        share.viewers.get(viewer.peer_hex) is not viewer:
                    return
                viewer.credit += 1
                viewer.last_ack = time.time()
                if not ok:
                    share.viewers.pop(viewer.peer_hex, None)
            if not ok:
                RNS.log(f"TrenchChat [screen]: {viewer.peer_hex[:12]}… refused "
                        f"an update; dropped as a viewer", RNS.LOG_NOTICE)
                self._notify_viewers(len(share.viewers))
                return
            self._pump(share)

        if not self._plane.send_update(viewer.peer_hex, update, _acked):
            with self._lock:
                share.viewers.pop(viewer.peer_hex, None)
            self._notify_viewers(len(share.viewers))

    def _tell_participants(self, share: _Share) -> None:
        """Send started to every direct participant not yet told."""
        if self._plane is None:
            return
        now = time.time()
        peers = self._voice.participants(share.channel_hex)
        for peer_hex in peers:
            if self._router.path_for(peer_hex) != PATH_DIRECT:
                continue
            with self._lock:
                if self._share is not share:
                    return
                told, at = share.told.get(peer_hex, (False, 0.0))
                if told or now - at < TOLD_RETRY_SECS:
                    continue
                share.told[peer_hex] = (False, now)
            width, height = share.size

            def _told(ok: bool, body: dict, peer=peer_hex) -> None:
                with self._lock:
                    if self._share is share:
                        share.told[peer] = (bool(ok), time.time())
                if not ok:
                    RNS.log(f"TrenchChat [screen]: {peer[:12]}… did not take the "
                            f"share: {body.get('r', 'no answer')}", RNS.LOG_DEBUG)

            self._plane.send_started(peer_hex, share.channel_hex, width, height,
                                     TILE_SHIFT, share.fps, _told)

    # --- the viewer ---

    def watch(self, peer_hex: str, *, max_width: int | None = None,
              max_height: int | None = None) -> str | None:
        """Watch a peer's share. The reason it could not start, or None."""
        if self._plane is None:
            return REASON_NO_DIRECT
        with self._lock:
            held = self._held.get(peer_hex)
            if held is None:
                return REASON_NO_SHARE
            if self._watching is not None and self._watching != peer_hex:
                self.unwatch()
            if self._watching == peer_hex:
                return None
            if self._voice.current_channel != held.channel_hex:
                return REASON_NOT_IN_VOICE_SELF
            if not self._plane.can_reach(peer_hex):
                return REASON_NO_SESSION
            self._watch_pending = peer_hex
            self._watch_result = None
            self._watch_event.clear()
            self._store.clear()
            self._watch_updates = 0
            self._watch_bytes = 0
            for consumer in self._clients.values():
                consumer.needs_full = True
                consumer.dirty.clear()
        width = max_width or held.width
        height = max_height or held.height

        def _answered(ok: bool, body: dict) -> None:
            with self._lock:
                if self._watch_pending != peer_hex:
                    return
                self._watch_result = (ok, str(body.get("r", "")))
                self._watch_event.set()

        if not self._plane.send_watch(peer_hex, held.channel_hex, width, height,
                                      _answered):
            with self._lock:
                self._watch_pending = None
            return REASON_NO_SESSION
        self._watch_event.wait(WATCH_TIMEOUT_SECS)
        with self._lock:
            result = self._watch_result
            self._watch_pending = None
            if result is None:
                return REASON_NO_ANSWER
            ok, reason = result
            if not ok:
                return reason or REASON_FORBIDDEN
            self._watching = peer_hex
            self._watch_since = time.time()
        self._notify_watch(peer_hex, "")
        return None

    def unwatch(self, reason: str = "") -> bool:
        """Stop watching. False if not watching."""
        with self._lock:
            peer_hex = self._watching
            self._watching = None
            self._store.clear()
        if peer_hex is None:
            return False
        if self._plane is not None and not reason:
            self._plane.send_unwatch(peer_hex)
        self._notify_watch(None, reason)
        return True

    def watching(self) -> dict | None:
        """The share this node is watching, for the client."""
        with self._lock:
            peer_hex = self._watching
            held = self._held.get(peer_hex) if peer_hex else None
            if peer_hex is None:
                return None
            return {"peer": peer_hex,
                    "channel": held.channel_hex if held else "",
                    "width": self._store.width, "height": self._store.height,
                    "since": self._watch_since, "updates": self._watch_updates,
                    "bytes": self._watch_bytes}

    def held_shares(self) -> list[dict]:
        """Every share this node has been told about and still holds."""
        with self._lock:
            return [held.as_dict() for held in self._held.values()]

    def new_client(self) -> int:
        """Register one client socket showing the watched share."""
        with self._lock:
            self._next_client += 1
            self._clients[self._next_client] = Consumer()
            return self._next_client

    def next_for_client(self, client_id: int) -> ScreenUpdate | None:
        """The update that brings one client up to the watched share's state."""
        with self._lock:
            consumer = self._clients.get(client_id)
            if consumer is None or self._watching is None or self._store.empty:
                return None
            return next_update(self._store, consumer)

    def drop_client(self, client_id: int) -> None:
        with self._lock:
            self._clients.pop(client_id, None)

    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    # --- plane callbacks: the core enforcement layer ---

    def _on_started(self, peer_hex: str, info: dict) -> bool:
        channel_hex = info["channel"]
        if not self.may_share(channel_hex, peer_hex):
            RNS.log(f"TrenchChat [screen]: dropping a share from "
                    f"{peer_hex[:12]}…: not allowed on {channel_hex[:12]}…",
                    RNS.LOG_WARNING)
            return False
        with self._lock:
            self._held[peer_hex] = _Held(peer_hex, info)
        self._notify_share(peer_hex, channel_hex, SHARE_STARTED)
        return True

    def _on_stopped(self, peer_hex: str, channel_hex: str) -> None:
        self._drop_held(peer_hex, REASON_STOPPED, channel_hex=channel_hex)

    def _on_watch(self, peer_hex: str, channel_hex: str, max_width: int,
                  max_height: int) -> str | None:
        with self._lock:
            share = self._share
            if share is None or share.channel_hex != channel_hex:
                return REASON_NOT_SHARING
        if not self._voice.is_participant(channel_hex, peer_hex):
            return REASON_NOT_IN_VOICE
        if not self._voice.may_voice(channel_hex, peer_hex):
            return REASON_FORBIDDEN
        with self._lock:
            if self._share is not share:
                return REASON_NOT_SHARING
            if peer_hex not in share.viewers and \
                    len(share.viewers) >= MAX_SCREEN_VIEWERS:
                return REASON_FULL
            share.viewers[peer_hex] = _Viewer(peer_hex, max_width, max_height)
            count = len(share.viewers)
        self._notify_viewers(count)
        self._pump(share)
        return None

    def _on_unwatch(self, peer_hex: str) -> None:
        with self._lock:
            share = self._share
            if share is None or share.viewers.pop(peer_hex, None) is None:
                return
            count = len(share.viewers)
        self._notify_viewers(count)

    def _on_update(self, peer_hex: str, update: ScreenUpdate) -> bool:
        # The sharer sends the first update from inside its watch handler, so
        # it can arrive before the answer that makes this node a watcher.
        with self._lock:
            if peer_hex not in (self._watching, self._watch_pending):
                return False
            self._store.apply(update)
            self._watch_updates += 1
            self._watch_bytes += update.payload_bytes
            for consumer in self._clients.values():
                consumer.note(update)
        self._notify_update()
        return True

    # --- what changes under a share ---

    def _on_path_changed(self, peer_hex: str, path: str) -> None:
        if path == PATH_DIRECT:
            with self._lock:
                share = self._share
            if share is not None:
                self._tell_participants(share)
            return
        with self._lock:
            share = self._share
            if share is not None:
                dropped = share.viewers.pop(peer_hex, None) is not None
                share.told.pop(peer_hex, None)
                count = len(share.viewers)
            else:
                dropped = False
        if dropped:
            self._notify_viewers(count)
        self._drop_held(peer_hex, REASON_SESSION_LOST)

    def _on_voice_session(self, state: str) -> None:
        if state != SESSION_LEFT:
            return
        self.stop_share(REASON_VOICE_LEFT)
        self.unwatch(REASON_VOICE_LEFT)

    def _on_voice_roster(self, channel_hex: str) -> None:
        now = time.time()
        with self._lock:
            stale = [held.peer_hex for held in self._held.values()
                     if held.channel_hex == channel_hex
                     and now - held.since > HELD_GRACE_SECS]
        for peer_hex in stale:
            if not self._voice.is_participant(channel_hex, peer_hex):
                self._drop_held(peer_hex, REASON_VOICE_LEFT)

    def _drop_held(self, peer_hex: str, reason: str,
                   channel_hex: str | None = None) -> None:
        with self._lock:
            held = self._held.get(peer_hex)
            if held is None or (channel_hex is not None
                                and held.channel_hex != channel_hex):
                return
            del self._held[peer_hex]
            watching = self._watching == peer_hex
        RNS.log(f"TrenchChat [screen]: dropped {peer_hex[:12]}…'s share: {reason}",
                RNS.LOG_DEBUG)
        if watching:
            self.unwatch(reason)
        self._notify_share(peer_hex, held.channel_hex, SHARE_STOPPED)

    def tick(self) -> None:
        """Once a second: the re-authorisation sweep on both sides."""
        with self._lock:
            share = self._share
        if share is not None:
            self._sweep_share(share)
        self._sweep_held()

    def _sweep_share(self, share: _Share) -> None:
        channel_hex = share.channel_hex
        if self._voice.current_channel != channel_hex:
            self.stop_share(REASON_VOICE_LEFT)
            return
        if not self.may_share(channel_hex, self._identity.hash_hex):
            self.stop_share(REASON_NO_PERMISSION)
            return
        now = time.time()
        with self._lock:
            viewers = list(share.viewers.values())
        gone = []
        for viewer in viewers:
            if now - viewer.last_ack > VIEWER_ACK_TIMEOUT_SECS and \
                    viewer.credit < VIEWER_WINDOW:
                gone.append((viewer, "stopped acknowledging"))
            elif self._router.path_for(viewer.peer_hex) != PATH_DIRECT:
                gone.append((viewer, "no direct session"))
            elif not self._voice.is_participant(channel_hex, viewer.peer_hex):
                gone.append((viewer, "left voice"))
            elif not self._voice.may_voice(channel_hex, viewer.peer_hex):
                gone.append((viewer, "no longer authorised"))
        if gone:
            with self._lock:
                for viewer, why in gone:
                    if share.viewers.get(viewer.peer_hex) is viewer:
                        del share.viewers[viewer.peer_hex]
                        RNS.log(f"TrenchChat [screen]: dropped viewer "
                                f"{viewer.peer_hex[:12]}…: {why}", RNS.LOG_NOTICE)
                count = len(share.viewers)
            self._notify_viewers(count)
        self._tell_participants(share)

    def _sweep_held(self) -> None:
        with self._lock:
            held = list(self._held.values())
        for entry in held:
            if not self.may_share(entry.channel_hex, entry.peer_hex):
                self._drop_held(entry.peer_hex, REASON_FORBIDDEN)
            elif self._router.path_for(entry.peer_hex) != PATH_DIRECT:
                self._drop_held(entry.peer_hex, REASON_SESSION_LOST)

    # --- diagnostics ---

    def status(self) -> dict:
        available, reason = (self._capture_probe() if self._plane is not None
                             else (False, "direct connections are off"))
        return {
            "available": {"ok": bool(available), "reason": reason},
            "sharing": self.sharing(),
            "watching": self.watching(),
            "shares": self.held_shares(),
        }

    def stop(self) -> None:
        self.stop_share("stopping")
        self.unwatch("stopping")
        if self._plane is not None:
            self._plane.stop()

    # --- callbacks ---

    def add_share_callback(self, cb) -> None:
        """cb(peer_hex, channel_hex, state): a peer's share started or stopped."""
        self._share_callbacks.append(cb)

    def add_session_callback(self, cb) -> None:
        """cb(state, reason): this node's own share started, stopped or failed."""
        self._session_callbacks.append(cb)

    def add_viewers_callback(self, cb) -> None:
        """cb(count): how many peers are watching this node's share."""
        self._viewers_callbacks.append(cb)

    def add_update_callback(self, cb) -> None:
        """cb(): the watched share has new bytes for the client sockets."""
        self._update_callbacks.append(cb)

    def add_watch_callback(self, cb) -> None:
        """cb(peer_hex or None, reason): what this node watches changed."""
        self._watch_callbacks.append(cb)

    def remove_update_callback(self, cb) -> None:
        if cb in self._update_callbacks:
            self._update_callbacks.remove(cb)

    def remove_watch_callback(self, cb) -> None:
        if cb in self._watch_callbacks:
            self._watch_callbacks.remove(cb)

    def _notify_share(self, peer_hex: str, channel_hex: str, state: str) -> None:
        self._fire(self._share_callbacks, peer_hex, channel_hex, state)

    def _notify_session(self, state: str, reason: str) -> None:
        self._fire(self._session_callbacks, state, reason)

    def _notify_viewers(self, count: int) -> None:
        self._fire(self._viewers_callbacks, count)

    def _notify_update(self) -> None:
        self._fire(self._update_callbacks)

    def _notify_watch(self, peer_hex: str | None, reason: str) -> None:
        self._fire(self._watch_callbacks, peer_hex, reason)

    @staticmethod
    def _fire(callbacks: list, *args) -> None:
        for cb in callbacks:
            try:
                cb(*args)
            except Exception as e:
                RNS.log(f"TrenchChat [screen]: callback error: {e}", RNS.LOG_ERROR)
