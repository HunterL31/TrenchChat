"""
Screen share plane over a direct session: one request operation, five actions.

The only carrier a screen share has. Everything rides REQ/RESP on the session
that is already up (transport.send_request), which returns None when there is
no session and retries nothing, so nothing here can ever fall back onto the
mesh; and there is no protocol field for any of it, so nothing here can be
packed into an LXMF message either.

    sharer -> participant   started, stopped   (what there is to watch)
    viewer -> sharer        watch, unwatch     (subscribe, with a size limit)
    sharer -> viewer        update             (one ScreenUpdate; the RESP is
                                                the acknowledgement that
                                                returns the sender's credit)

This module never touches Storage or core managers: who may share, who may
watch and what to do with an update are the injected callbacks, exactly as
the voice plane has it. What it does hold is every bound on the way in: the
action name, each field's type and range, the update's byte ceiling, and the
declared size of every image, checked before any decoder runs.
"""

import threading
import time

import RNS

from trenchchat.network.screen_wire import (
    MAX_SHARE_HEIGHT, MAX_SHARE_WIDTH, MAX_TILE_SHIFT, MAX_UPDATE_BYTES,
    MIN_TILE_SHIFT, ScreenUpdate, check_update_images, pack_update,
    unpack_update,
)

SCREEN_OP = "screen"

A_STARTED = "started"
A_STOPPED = "stopped"
A_WATCH = "watch"
A_UNWATCH = "unwatch"
A_UPDATE = "update"

# Payload keys, short because they travel on every update.
K_ACTION = "a"
K_CHANNEL = "c"
K_WIDTH = "w"
K_HEIGHT = "h"
K_TILE_SHIFT = "t"
K_FPS = "f"
K_DATA = "d"
K_REASON = "r"

# Refusal reasons a sharer or viewer answers with.
REASON_NOT_SHARING = "not_sharing"
REASON_NOT_IN_VOICE = "not_in_voice"
REASON_FULL = "full"
REASON_FORBIDDEN = "forbidden"
REASON_NOT_WATCHING = "not_watching"
REASON_MALFORMED = "malformed"
REASON_RATE_LIMITED = "rate_limited"
REASON_NO_SESSION = "no_session"

CHANNEL_HASH_BYTES = 16
MAX_FPS = 30

# Control actions one peer may send in a window; updates are bounded by the
# credit the viewer hands back rather than by a rate.
CONTROL_RATE_LIMIT = 60
CONTROL_RATE_WINDOW = 60.0


class IPScreenTransport:
    """The screen plane carried by direct sessions."""

    def __init__(self, transport):
        """transport: the IPTransport whose sessions carry the requests."""
        self._transport = transport
        self._lock = threading.Lock()
        self._control_times: dict[str, list[float]] = {}
        self._on_started = None
        self._on_stopped = None
        self._on_watch = None
        self._on_unwatch = None
        self._on_update = None
        transport.set_request_handler(SCREEN_OP, self._on_request)

    def stop(self) -> None:
        """Stop answering; the transport outlives the plane."""
        self._transport.set_request_handler(SCREEN_OP, None)

    # --- callbacks the manager installs ---

    def set_started_callback(self, cb) -> None:
        """cb(peer_hex, info) -> bool; info has channel, width, height,
        tile_shift, fps. False refuses the share."""
        self._on_started = cb

    def set_stopped_callback(self, cb) -> None:
        """cb(peer_hex, channel_hex)."""
        self._on_stopped = cb

    def set_watch_callback(self, cb) -> None:
        """cb(peer_hex, channel_hex, max_width, max_height) -> reason or None."""
        self._on_watch = cb

    def set_unwatch_callback(self, cb) -> None:
        """cb(peer_hex)."""
        self._on_unwatch = cb

    def set_update_callback(self, cb) -> None:
        """cb(peer_hex, update) -> bool; True acknowledges and returns credit."""
        self._on_update = cb

    # --- reachability ---

    def can_reach(self, peer_hex: str) -> bool:
        """Whether a session with this peer is up right now."""
        return bool(self._transport.can_reach(peer_hex))

    # --- outbound ---

    def send_started(self, peer_hex: str, channel_hex: str, width: int,
                     height: int, tile_shift: int, fps: int,
                     on_result=None) -> bool:
        return self._request(peer_hex, {
            K_ACTION: A_STARTED, K_CHANNEL: bytes.fromhex(channel_hex),
            K_WIDTH: int(width), K_HEIGHT: int(height),
            K_TILE_SHIFT: int(tile_shift), K_FPS: int(fps),
        }, on_result)

    def send_stopped(self, peer_hex: str, channel_hex: str,
                     on_result=None) -> bool:
        return self._request(peer_hex, {
            K_ACTION: A_STOPPED, K_CHANNEL: bytes.fromhex(channel_hex),
        }, on_result)

    def send_watch(self, peer_hex: str, channel_hex: str, max_width: int,
                   max_height: int, on_result=None) -> bool:
        return self._request(peer_hex, {
            K_ACTION: A_WATCH, K_CHANNEL: bytes.fromhex(channel_hex),
            K_WIDTH: int(max_width), K_HEIGHT: int(max_height),
        }, on_result)

    def send_unwatch(self, peer_hex: str, on_result=None) -> bool:
        return self._request(peer_hex, {K_ACTION: A_UNWATCH}, on_result)

    def send_update(self, peer_hex: str, update: ScreenUpdate,
                    on_result=None) -> bool:
        """Put one update on a viewer's session. on_result(ok, payload) is the
        acknowledgement; False without a session or for an update over a limit."""
        try:
            data = pack_update(update)
        except ValueError as e:
            RNS.log(f"TrenchChat [screen]: refusing to send an update: {e}",
                    RNS.LOG_WARNING)
            return False
        return self._request(peer_hex, {K_ACTION: A_UPDATE, K_DATA: data},
                             on_result)

    def _request(self, peer_hex: str, payload: dict, on_result) -> bool:
        def _result(ok: bool, body: dict) -> None:
            if on_result is not None:
                on_result(bool(ok), body if isinstance(body, dict) else {})

        return self._transport.send_request(peer_hex, SCREEN_OP, payload,
                                            _result) is not None

    # --- inbound ---

    def _on_request(self, peer_hex: str, payload: dict) -> tuple[bool, dict]:
        """One request off a session, on a worker thread, from an authenticated
        peer. Every field is a claim until checked here."""
        action = payload.get(K_ACTION)
        if not isinstance(action, str):
            return False, {K_REASON: REASON_MALFORMED}
        if action == A_UPDATE:
            return self._handle_update(peer_hex, payload)
        if not self._allow_control(peer_hex, time.time()):
            RNS.log(f"TrenchChat [screen]: rate-limited {action} from "
                    f"{peer_hex[:12]}…", RNS.LOG_WARNING)
            return False, {K_REASON: REASON_RATE_LIMITED}
        if action == A_STARTED:
            return self._handle_started(peer_hex, payload)
        if action == A_STOPPED:
            channel_hex = _channel_hex(payload)
            if channel_hex is None:
                return False, {K_REASON: REASON_MALFORMED}
            if self._on_stopped is not None:
                self._on_stopped(peer_hex, channel_hex)
            return True, {}
        if action == A_WATCH:
            return self._handle_watch(peer_hex, payload)
        if action == A_UNWATCH:
            if self._on_unwatch is not None:
                self._on_unwatch(peer_hex)
            return True, {}
        return False, {K_REASON: REASON_MALFORMED}

    def _handle_started(self, peer_hex: str, payload: dict) -> tuple[bool, dict]:
        channel_hex = _channel_hex(payload)
        width = _bounded_int(payload.get(K_WIDTH), 1, MAX_SHARE_WIDTH)
        height = _bounded_int(payload.get(K_HEIGHT), 1, MAX_SHARE_HEIGHT)
        tile_shift = _bounded_int(payload.get(K_TILE_SHIFT), MIN_TILE_SHIFT,
                                  MAX_TILE_SHIFT)
        fps = _bounded_int(payload.get(K_FPS), 1, MAX_FPS)
        if None in (channel_hex, width, height, tile_shift, fps):
            RNS.log(f"TrenchChat [screen]: malformed started from "
                    f"{peer_hex[:12]}…", RNS.LOG_WARNING)
            return False, {K_REASON: REASON_MALFORMED}
        info = {"channel": channel_hex, "width": width, "height": height,
                "tile_shift": tile_shift, "fps": fps}
        if self._on_started is None or not self._on_started(peer_hex, info):
            return False, {K_REASON: REASON_FORBIDDEN}
        return True, {}

    def _handle_watch(self, peer_hex: str, payload: dict) -> tuple[bool, dict]:
        channel_hex = _channel_hex(payload)
        max_width = _bounded_int(payload.get(K_WIDTH), 1, MAX_SHARE_WIDTH)
        max_height = _bounded_int(payload.get(K_HEIGHT), 1, MAX_SHARE_HEIGHT)
        if None in (channel_hex, max_width, max_height):
            return False, {K_REASON: REASON_MALFORMED}
        if self._on_watch is None:
            return False, {K_REASON: REASON_NOT_SHARING}
        reason = self._on_watch(peer_hex, channel_hex, max_width, max_height)
        if reason is not None:
            RNS.log(f"TrenchChat [screen]: refusing {peer_hex[:12]}… as a "
                    f"viewer: {reason}", RNS.LOG_NOTICE)
            return False, {K_REASON: str(reason)}
        return True, {}

    def _handle_update(self, peer_hex: str, payload: dict) -> tuple[bool, dict]:
        data = payload.get(K_DATA)
        if not isinstance(data, bytes):
            return False, {K_REASON: REASON_MALFORMED}
        try:
            update = unpack_update(data, MAX_UPDATE_BYTES)
            check_update_images(update)
        except ValueError as e:
            RNS.log(f"TrenchChat [screen]: refusing an update from "
                    f"{peer_hex[:12]}…: {e}", RNS.LOG_WARNING)
            return False, {K_REASON: REASON_MALFORMED}
        if self._on_update is None or not self._on_update(peer_hex, update):
            return False, {K_REASON: REASON_NOT_WATCHING}
        return True, {}

    def _allow_control(self, peer_hex: str, now: float) -> bool:
        """The same per-peer ceiling the voice planes keep on packets: a
        session proves who is sending, not how much work they may cause."""
        with self._lock:
            times = self._control_times.setdefault(peer_hex, [])
            times[:] = [t for t in times if now - t < CONTROL_RATE_WINDOW]
            if len(times) >= CONTROL_RATE_LIMIT:
                return False
            times.append(now)
            return True


def _channel_hex(payload: dict) -> str | None:
    channel = payload.get(K_CHANNEL)
    if not isinstance(channel, bytes) or len(channel) != CHANNEL_HASH_BYTES:
        return None
    return channel.hex()


def _bounded_int(value, low: int, high: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if not low <= value <= high:
        return None
    return value
