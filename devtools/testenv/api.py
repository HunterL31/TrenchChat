"""
FastAPI wrapper around one tester's Backend.

Every mutating endpoint here calls trenchchat.core.actions for
multi-step sequences (create channel, send message, edit permissions, ...)
and the core managers directly for single-call actions (send_invite,
send_join_request, ...). There is no reimplementation of core logic here --
see trenchchat/core/actions.py.

This means a bug (or a fix) exercised through this API is exercising the
same code path a real client would hit.

The "--- link control ---" group below is the one exception: it has no
actions.py counterpart because it isn't application logic at all -- it's
dev-harness process control (dropping/restoring this tester's own network
link so the UI can simulate going offline), so it calls Backend.go_offline
/go_online directly.
"""

import asyncio
import base64
import json
import secrets
from contextlib import asynccontextmanager
from typing import Any

import RNS
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from starlette.routing import Match, Mount

from trenchchat.core import actions
from trenchchat.core.image import is_gif, prepare_image
from trenchchat.core.avatar import compress_avatar
from trenchchat.core.discovery import list_discovered_interfaces, pin_discovered_interface
from trenchchat.core.interfaces_config import (
    DuplicateInterfaceError, EDITABLE_TYPES, InterfaceConfigError,
    apply_suggested_defaults, build_interface_config_dict, delete_interface,
    get_missing_suggested_defaults, load_discovery_settings,
    load_interfaces_config, missing_required_field, write_discovery_settings,
    write_interface,
)
from trenchchat.core.naming import NameInUseError
from trenchchat.core.permissions import (
    ALL_PERMISSIONS, CREATE_CHANNEL, INVITE, KICK, MANAGE_CHANNEL, MANAGE_ROLES,
    ROLE_ADMIN, ROLE_MEMBER, PRESET_OPEN, PRESET_PRIVATE, SEND_MESSAGE, VOICE_CHAT,
    is_open_join, offered_permissions, permissions_from_json,
)
from trenchchat.core.presence import resolve_display_name
from trenchchat.core.link_quality import (
    LinkQuality, quality_label, score_path,
)

from backend_core import Backend


class CreateChannelRequest(BaseModel):
    name: str
    description: str = ""
    access: str = "public"  # "public" | "invite"


class CreateServerRequest(BaseModel):
    name: str
    description: str = ""


class CreateServerChannelRequest(BaseModel):
    name: str
    description: str = ""


class SetDisplayNameRequest(BaseModel):
    display_name: str


class SetAvatarRequest(BaseModel):
    image_data_b64: str


class AddReactionRequest(BaseModel):
    emoji_hash: str


class ImportEmojiRequest(BaseModel):
    name: str
    image_data_b64: str


class AddFriendRequest(BaseModel):
    identity_hash: str
    nickname: str = ""
    note: str = ""


class AddLxmfAddressRequest(BaseModel):
    lxmf_hash: str
    nickname: str = ""
    note: str = ""


class UpdateFriendRequest(BaseModel):
    nickname: str | None = None
    note: str | None = None


class FriendRequestRequest(BaseModel):
    identity_hash: str
    note: str = ""
    nickname: str = ""


class AcceptFriendRequest(BaseModel):
    nickname: str = ""


class PinPropagationNodeRequest(BaseModel):
    node_hash: str = ""
class NomadBrowseRequest(BaseModel):
    url: str
    current_node: str | None = None
    data: dict[str, str] | None = None
    refresh: bool = False


class NomadFetchRequest(BaseModel):
    node_hash: str
    path: str = "/page/index.mu"


class NomadIdentifyRequest(BaseModel):
    node_hash: str
    enabled: bool


class NomadBookmarkRequest(BaseModel):
    node_hash: str
    path: str
    label: str = ""


class NomadBookmarkDeleteRequest(BaseModel):
    # The path contains '/', so deletion takes a body rather than a URL segment.
    node_hash: str
    path: str


class NomadHostingRequest(BaseModel):
    enabled: bool | None = None
    node_name: str | None = None


class SendMessageRequest(BaseModel):
    content: str
    reply_to: str | None = None
    image_data_b64: str | None = None


class UpdatePermissionsRequest(BaseModel):
    admin: list[str] = []
    member: list[str] = []


class InviteRequest(BaseModel):
    peer_hash_hex: str


class JoinRequestRequest(BaseModel):
    channel_hash_hex: str
    token_hex: str
    expiry: float
    admin_hash_hex: str


class UpdateRolesRequest(BaseModel):
    remove_members: list[str] = []
    add_admins: list[str] = []
    remove_admins: list[str] = []


class SettingsUpdateRequest(BaseModel):
    propagation_enabled: bool | None = None
    propagation_node_name: str | None = None
    propagation_storage_limit_mb: int | None = None


class SetUiThemeRequest(BaseModel):
    theme: dict


class SaveUiThemeRequest(BaseModel):
    name: str
    theme: dict


class DeleteUiThemeRequest(BaseModel):
    name: str


class CreateInterfaceRequest(BaseModel):
    name: str
    type: str
    enabled: bool = True
    type_values: dict[str, str] = {}
    common_values: dict[str, str] = {}


class UpdateInterfaceRequest(BaseModel):
    type: str
    enabled: bool = True
    type_values: dict[str, str] = {}
    common_values: dict[str, str] = {}


class DiscoverySettingsRequest(BaseModel):
    discover_interfaces: bool
    autoconnect_discovered_interfaces: int = 0
    required_discovery_value: int | None = None


class PinDiscoveredRequest(BaseModel):
    discovery_hash: str


class VoiceMuteRequest(BaseModel):
    muted: bool


class VoiceToneRequest(BaseModel):
    enabled: bool


class VoiceDevicesRequest(BaseModel):
    input_device: str | None = None
    output_device: str | None = None


def _channel_to_dict(row) -> dict[str, Any]:
    keys = row.keys()
    return {
        "hash": row["hash"],
        "name": row["name"],
        "description": row["description"],
        "creator_hash": row["creator_hash"],
        "open_join": is_open_join(permissions_from_json(row["permissions"])),
        "created_at": row["created_at"],
        "server_hash": row["server_hash"] if "server_hash" in keys else None,
    }


def _server_to_dict(row) -> dict[str, Any]:
    return {
        "hash": row["hash"],
        "name": row["name"],
        "description": row["description"],
        "creator_hash": row["creator_hash"],
        "created_at": row["created_at"],
    }


def _reactions_summary(storage, message_id: str, self_hex: str) -> list[dict[str, Any]]:
    """Group raw reaction rows into one entry per emoji: {emoji_hash, count, reacted_by_me}."""
    by_emoji: dict[str, dict[str, Any]] = {}
    for r in storage.get_reactions(message_id):
        entry = by_emoji.setdefault(
            r["emoji_hash"], {"emoji_hash": r["emoji_hash"], "count": 0, "reacted_by_me": False}
        )
        entry["count"] += 1
        if r["reactor_hash"] == self_hex:
            entry["reacted_by_me"] = True
    return list(by_emoji.values())


def _message_to_dict(row, reactions: list[dict[str, Any]] | None = None,
                     delivery_state: str | None = None) -> dict[str, Any]:
    return {
        "message_id": row["message_id"],
        "sender_hash": row["sender_hash"],
        "sender_name": row["sender_name"],
        "content": row["content"],
        "timestamp": row["timestamp"],
        "reply_to": row["reply_to"],
        "has_image": bool(row["image_data"]) if "image_data" in row.keys() else False,
        "image_stripped": bool(row["image_stripped"]) if "image_stripped" in row.keys() else False,
        "reactions": reactions or [],
        # Only our own outbound messages carry a delivery state; None for
        # everyone else's (and for our own once it ages out of the tracker).
        "delivery_state": delivery_state,
    }


class EventBus:
    """Fan-out for backend callbacks (fired on RNS/LXMF background threads)
    to any connected WebSocket clients (running in the asyncio event loop)."""

    def __init__(self):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._clients: set[WebSocket] = set()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def register(self, ws: WebSocket) -> None:
        self._clients.add(ws)

    def unregister(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    def emit(self, event_type: str, **payload) -> None:
        """Safe to call from any thread."""
        if self._loop is None:
            return
        message = json.dumps({"type": event_type, **payload})
        for ws in list(self._clients):
            asyncio.run_coroutine_threadsafe(self._safe_send(ws, message), self._loop)

    async def _safe_send(self, ws: WebSocket, message: str) -> None:
        try:
            await ws.send_text(message)
        except Exception:
            self._clients.discard(ws)


TOKEN_HEADER = "x-tc-token"
TOKEN_QUERY_PARAM = "token"

# Largest request body accepted. Every upload endpoint takes base64 in JSON and
# the limits below it are per-image, so without this a token holder can hand
# the process an arbitrarily large string to decode.
MAX_REQUEST_BYTES = 4 * 1024 * 1024

# Host values a browser may reach this backend on. A page cannot set Host, but
# DNS rebinding gives it one of its own choosing, which the socket's
# same-origin test then derives its answer from -- so a name outside this set
# is refused rather than trusted.
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")


def generate_token() -> str:
    """A fresh API token for one backend process."""
    return secrets.token_urlsafe(32)


def _presented_token(headers, query_params) -> str:
    """The token a request carries, from any of the three accepted places.

    The query parameter exists because a browser can set headers on neither a
    WebSocket handshake nor an <img> src, and both need to authenticate.
    """
    authorization = headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[len("bearer "):].strip()
    return headers.get(TOKEN_HEADER) or query_params.get(TOKEN_QUERY_PARAM) or ""


def _token_ok(expected: str, presented: str) -> bool:
    return bool(presented) and secrets.compare_digest(expected, presented)


def _host_allowed(host: str, allowed_origins: list[str]) -> bool:
    """True if the Host header names somewhere this backend is served.

    Bare hostname or host:port, matched against loopback and whatever the
    launcher explicitly allowed. An empty Host is a non-browser client.
    """
    if not host:
        return True
    name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    if name in _LOCAL_HOSTS or host in _LOCAL_HOSTS:
        return True
    return any(origin.split("://", 1)[-1].rsplit(":", 1)[0] == name
               for origin in allowed_origins if "://" in origin)


def _identity_hashes(values: list[str]) -> list[bytes] | None:
    """Decode identity hex from a request body.

    Raises ValueError on anything that is not identity hex, so a malformed
    request is answered as a bad request rather than a server error.
    """
    out = []
    for value in values:
        raw = bytes.fromhex(value)
        if len(raw) != 16:
            raise ValueError(f"{value!r} is not an identity hash")
        out.append(raw)
    return out or None


def _origin_allowed(origin: str, host: str, allowed_origins: list[str]) -> bool:
    """True if a browser Origin may open a socket to this backend.

    Non-browser clients send no Origin at all; the token is their only gate.
    """
    if not origin:
        return True
    if origin in allowed_origins:
        return True
    return host != "" and origin in (f"http://{host}", f"https://{host}")


def create_app(backend: Backend, *, token: str | None = None,
               allowed_origins: list[str] | None = None) -> FastAPI:
    """Build the API app.

    Every API route requires the token, presented as an `Authorization:
    Bearer` header, an `X-TC-Token` header, or a `?token=` query parameter.
    Without it this surface would be an unauthenticated remote control for
    the identity it serves: any process that can reach the port -- or any web
    page the user visits, since it is reachable cross-origin -- could send
    messages as them and read their whole history.

    Paths served by a mount (the built web client, added by the caller) are
    public: they are static assets, and the client has to load before it can
    present a token.

    allowed_origins lists extra browser origins permitted to call this
    backend cross-origin; same-origin callers always pass. The dev
    orchestrator needs it because its page and the tester APIs differ by port.
    """
    api_token = token or generate_token()
    origins = list(allowed_origins or [])
    bus = EventBus()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        bus.bind_loop(asyncio.get_running_loop())
        yield
        # uvicorn runs this on SIGINT/SIGTERM, so both entry points quit
        # gracefully: orchestrator.py's "kill tester", and Ctrl+C on
        # serve_profile.py, which is a real client and really is going away.
        backend.announce_offline()
        backend.close()

    app = FastAPI(title=f"TrenchChat tester: {backend.config.display_name}",
                  lifespan=lifespan)
    app.state.api_token = api_token

    @app.exception_handler(ValueError)
    async def _bad_request(request, exc: ValueError):
        """Malformed input is the caller's fault, not a server failure.

        Several endpoints decode hex or base64 straight out of a request body;
        letting that raise turned a bad request into a 500.
        """
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    def _is_api_request(request) -> bool:
        for route in app.router.routes:
            if isinstance(route, Mount):
                continue
            if route.matches(request.scope)[0] is not Match.NONE:
                return True
        return False

    @app.middleware("http")
    async def _require_token(request, call_next):
        if request.method == "OPTIONS" or not _is_api_request(request):
            return await call_next(request)
        if not _host_allowed(request.headers.get("host", ""), origins):
            # A page cannot set Host, but DNS rebinding hands it one it chose,
            # and the socket's same-origin test reads its answer back out of
            # that header. The token is then the only control left standing.
            return JSONResponse({"error": "unrecognised Host"}, status_code=421)
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_REQUEST_BYTES:
            return JSONResponse({"error": "request body too large"},
                                status_code=413)
        presented = _presented_token(request.headers, request.query_params)
        if not _token_ok(api_token, presented):
            return JSONResponse({"error": "invalid or missing API token"},
                                status_code=401)
        return await call_next(request)

    # Added after the token middleware so it wraps it: a preflight must be
    # answerable without credentials, or the browser never sends the real
    # request. allow_origins is an explicit list, never "*" -- with no
    # credentials to protect, a wildcard would let any page read every
    # response.
    app.add_middleware(
        CORSMiddleware, allow_origins=origins, allow_methods=["*"],
        allow_headers=["*"],
    )

    # --- wire backend callbacks (RNS/LXMF background threads) to the bus ---

    def _delivery_state_for(row) -> str | None:
        """The delivery state of a row, but only for our own outbound messages."""
        if row is None or row["sender_hash"] != backend.identity.hash_hex:
            return None
        return backend.messaging.get_delivery_state(row["message_id"])

    def _on_message(channel_hash_hex: str, message_id: str):
        msgs = backend.storage.get_messages(channel_hash_hex)
        row = next((m for m in msgs if m["message_id"] == message_id), None)
        reactions = _reactions_summary(backend.storage, message_id, backend.identity.hash_hex) if row else None
        bus.emit("message", channel_hash=channel_hash_hex,
                message=_message_to_dict(row, reactions, _delivery_state_for(row)) if row else None)

    def _on_delivery_status(channel_hash_hex: str, message_id: str,
                            delivery_state: str):
        bus.emit("delivery_status", channel_hash=channel_hash_hex,
                 message_id=message_id, delivery_state=delivery_state)

    # Pending invites, exactly like MainWindow._pending_invites -- nothing
    # is sent to the network until the user explicitly accepts or declines.
    # Seeded from storage so a restart doesn't lose an invite awaiting a
    # decision.
    pending_invites: list[dict[str, Any]] = []
    for inv in backend.invite_mgr.list_pending_invites():
        pending_invites.append({
            "channel_hash_hex": inv["channel_hash_hex"], "channel_name": inv["channel_name"],
            "token_hex": inv["token"].hex(), "expiry": inv["expiry"],
            "admin_hex": inv["admin_hash_hex"],
            "scope_kind": backend.invite_mgr.invite_scope_kind(inv["channel_hash_hex"]),
        })
    # Held member list documents ride in the same list under a null token.
    # They are the other half of "you have been invited", and without them a
    # peer added by an admin it holds no anchor for has nothing to accept.
    for held in backend.invite_mgr.list_pending_memberships():
        pending_invites.append({
            "channel_hash_hex": held["channel_hash"], "channel_name": held["channel_name"],
            "token_hex": None, "expiry": 0.0,
            "admin_hex": held["admin_hash"],
            "scope_kind": backend.invite_mgr.invite_scope_kind(held["channel_hash"]),
        })

    def _on_invite(channel_hash_hex, channel_name, token, expiry, admin_hex):
        # A re-invite to the same channel refreshes the pending entry (new
        # token/expiry) instead of stacking a second one alongside it.
        #
        # A null token means an admin added us directly: the member list
        # document is already held, so accepting confirms it rather than
        # sending a join request. Calling .hex() on that used to raise inside
        # the manager's callback guard, losing the event as well as the entry.
        pending_invites[:] = [
            i for i in pending_invites if i["channel_hash_hex"] != channel_hash_hex
        ]
        pending_invites.append({
            "channel_hash_hex": channel_hash_hex, "channel_name": channel_name,
            "token_hex": token.hex() if token else None,
            "expiry": expiry, "admin_hex": admin_hex,
            "scope_kind": backend.invite_mgr.invite_scope_kind(channel_hash_hex),
        })
        bus.emit("invite_received", channel_hash=channel_hash_hex, channel_name=channel_name)

    def _on_channel_joined(channel_hash_hex, channel_name):
        bus.emit("channel_joined", channel_hash=channel_hash_hex, channel_name=channel_name)

    def _on_server_joined(server_hash_hex, server_name):
        bus.emit("server_joined", server_hash=server_hash_hex, server_name=server_name)

    def _on_member_list_updated(channel_hash_hex):
        bus.emit("member_list_updated", channel_hash=channel_hash_hex)

    def _on_channel_discovered(channel_hash_hex, channel_name):
        bus.emit("channel_discovered", channel_hash=channel_hash_hex, channel_name=channel_name)

    def _on_presence_changed(peer_hash_hex: str, is_online: bool):
        bus.emit("presence", identity_hash=peer_hash_hex, is_online=is_online)

    def _on_avatar_changed(identity_hash_hex: str):
        # Carry the version so a running client can bust its avatar cache
        # (fetch /peers/{hash}/avatar?v=version) instead of serving a stale
        # image from the old URL. None means the avatar was removed.
        if identity_hash_hex == backend.identity.hash_hex:
            version = backend.config.avatar_version
        else:
            row = backend.storage.get_peer_avatar(identity_hash_hex)
            version = row["avatar_version"] if row else None
        bus.emit("avatar_updated", identity_hash=identity_hash_hex,
                 avatar_version=version)

    def _on_friend_updated(identity_hash_hex: str):
        bus.emit("friend_updated", identity_hash=identity_hash_hex)

    def _on_friend_request(identity_hash_hex: str, display_name: str, note: str):
        # Attacker-controlled text from someone with no relationship to us yet.
        # It is capped on the way in (friends.py) and is data, not a command.
        bus.emit("friend_request", identity_hash=identity_hash_hex,
                 display_name=display_name, note=note)

    def _on_propagation_node(node_hex: str):
        bus.emit("propagation_node", node_hash=node_hex)

    def _on_directory_updated(identity_hash_hex: str, display_name: str):
        # A peer's display name changed (via their announce, or a message they
        # sent), so a running client can refresh /directory without a reload.
        bus.emit("directory_updated", identity_hash=identity_hash_hex,
                 display_name=display_name)

    def _on_reaction_changed(channel_hash_hex: str, message_id: str):
        bus.emit("reaction_updated", channel_hash=channel_hash_hex, message_id=message_id)

    def _on_emoji_received(emoji_hash: str):
        bus.emit("emoji_received", emoji_hash=emoji_hash)

    def _on_sync_status(channel_hash_hex: str):
        bus.emit("sync_status", channel_hash=channel_hash_hex,
                 status=backend.sync_mgr.status.get_status(channel_hash_hex))

    def _on_link_status(is_online: bool):
        bus.emit("net_status", online=is_online)

    def _on_network_map_changed():
        # No payload: the map is large and the client re-reads /network/map.
        bus.emit("network_map_changed")

    def _on_voice_roster(channel_hash_hex: str):
        bus.emit("voice_roster", channel_hash=channel_hash_hex)

    def _on_voice_speaking(channel_hash_hex: str, peer_hex: str, speaking: bool):
        bus.emit("voice_speaking", channel_hash=channel_hash_hex,
                 identity_hash=peer_hex, speaking=speaking)

    def _on_voice_session(state: str):
        bus.emit("voice_session", state=state)

    backend.messaging.add_message_callback(_on_message)
    backend.messaging.add_delivery_status_callback(_on_delivery_status)
    backend.invite_mgr.add_invite_callback(_on_invite)
    backend.invite_mgr.add_channel_joined_callback(_on_channel_joined)
    backend.invite_mgr.add_server_joined_callback(_on_server_joined)
    backend.invite_mgr.add_member_list_callback(_on_member_list_updated)
    backend.channel_mgr.add_channel_discovered_callback(_on_channel_discovered)
    backend.presence_mgr.add_presence_callback(_on_presence_changed)
    backend.avatar_mgr.add_avatar_callback(_on_avatar_changed)
    backend.friends_mgr.add_friends_callback(_on_friend_updated)
    backend.friends_mgr.add_request_callback(_on_friend_request)
    backend.propagation_nodes.add_selection_callback(_on_propagation_node)
    backend.user_directory.add_directory_callback(_on_directory_updated)
    backend.reaction_mgr.add_reaction_callback(_on_reaction_changed)
    backend.reaction_mgr.add_emoji_callback(_on_emoji_received)
    backend.sync_mgr.status.add_status_callback(_on_sync_status)
    backend.add_link_callback(_on_link_status)
    backend.network_monitor.add_change_callback(_on_network_map_changed)
    def _on_nomad_node(node_hash_hex: str, display_name: str):
        bus.emit("nomad_node", node_hash=node_hash_hex,
                 display_name=display_name)

    def _on_nomad_fetch(fetch_id: str, node_hash_hex: str, path: str,
                        status: str, progress: float, reason):
        # Page content deliberately travels over REST after the "done"
        # event, not in the WS frame -- pages can be 512 KiB.
        bus.emit("nomad_fetch", fetch_id=fetch_id, node_hash=node_hash_hex,
                 path=path, status=status, progress=progress, reason=reason)

    backend.voice_mgr.add_roster_callback(_on_voice_roster)
    backend.voice_mgr.add_speaking_callback(_on_voice_speaking)
    backend.voice_mgr.add_session_callback(_on_voice_session)
    backend.node_browser.add_node_callback(_on_nomad_node)
    backend.node_browser.add_fetch_callback(_on_nomad_fetch)

    # --- identity ---

    @app.get("/me")
    def get_me():
        return {
            "hash_hex": backend.identity.hash_hex,
            "display_name": backend.config.display_name,
        }

    @app.get("/version")
    def get_version():
        """This build's version and how it compares to the last one to run.

        transition is one of first_run, unknown, same, upgrade, downgrade,
        sidegrade -- what the installer that produced this build did to the
        profile (see trenchchat/version.py).
        """
        return backend.version_state.as_dict()

    @app.post("/me/display_name")
    def set_display_name(req: SetDisplayNameRequest):
        # Same multi-step action the real Settings dialog drives: set the name
        # and re-announce both destinations so peers update their recall and
        # directory promptly, rather than waiting for the next heartbeat.
        actions.set_display_name(backend.router, req.display_name)
        return {"ok": True}

    @app.get("/settings")
    def get_settings():
        return actions.read_settings(backend.config)

    @app.post("/settings")
    def update_settings(req: SettingsUpdateRequest):
        # Same entry point the Settings dialog's _on_accept uses, minus
        # display_name/avatar which have their own endpoints above.
        updates = req.model_dump(exclude_unset=True)
        try:
            actions.apply_settings(backend.config, backend.router, updates)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return {"ok": True, "settings": actions.read_settings(backend.config)}

    @app.get("/ui_theme")
    def get_ui_theme():
        return {"theme": actions.read_ui_theme(backend.config)}

    @app.post("/ui_theme")
    def set_ui_theme(req: SetUiThemeRequest):
        actions.set_ui_theme(backend.config, req.theme)
        # Theme changes come from a client rather than the mesh, so nothing
        # else would tell this profile's other open clients about them.
        bus.emit("ui_theme", theme=actions.read_ui_theme(backend.config))
        return {"ok": True}

    @app.get("/ui_theme_library")
    def get_ui_theme_library():
        return {"themes": actions.read_ui_theme_library(backend.config)}

    def _emit_theme_library() -> None:
        bus.emit("ui_theme_library",
                 themes=actions.read_ui_theme_library(backend.config))

    def _delete_ui_theme(name: str):
        if not actions.delete_ui_theme_from_library(backend.config, name):
            return JSONResponse(
                {"ok": False, "error": "no such theme"}, status_code=404,
            )
        _emit_theme_library()
        return {"ok": True}

    @app.post("/ui_theme_library")
    def save_ui_theme_to_library(req: SaveUiThemeRequest):
        try:
            actions.save_ui_theme_to_library(backend.config, req.name, req.theme)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        _emit_theme_library()
        return {"ok": True}

    # A name containing '/' cannot be addressed as a path segment -- even
    # percent-encoded, the router splits on it -- so deleting goes through a
    # body. The path form stays for older clients.
    @app.post("/ui_theme_library/delete")
    def delete_ui_theme_by_body(req: DeleteUiThemeRequest):
        return _delete_ui_theme(req.name)

    @app.delete("/ui_theme_library/{name}")
    def delete_ui_theme_from_library(name: str):
        return _delete_ui_theme(name)

    @app.get("/peers/{peer_hash}/presence")
    def get_peer_presence(peer_hash: str):
        return {
            "identity_hash": peer_hash,
            "is_online": backend.presence_mgr.is_online(peer_hash),
        }

    @app.get("/network/map")
    def get_network_map():
        from trenchchat.core.network_map import gather_network_data
        return gather_network_data(backend.rns, backend.identity.hash_hex,
                                   backend.storage, backend.user_directory,
                                   presence=backend.presence_mgr,
                                   propagation=backend.propagation_nodes,
                                   nomad=backend.node_browser)

    @app.get("/bandwidth")
    def get_bandwidth():
        # Windowed transfer rates over all Reticulum interfaces; the monitor
        # itself lives in core (trenchchat/core/bandwidth.py) and samples on
        # the backend's bandwidth-sampler thread plus once per request here.
        return backend.bandwidth.rates()

    @app.get("/reticulum/interfaces")
    def get_interfaces():
        # Same data source InterfacesWidget.load_interfaces() merges: the
        # configured [interfaces] section plus live rns.get_interface_stats().
        cfg_interfaces = load_interfaces_config(backend.rns_config_path)
        try:
            stats_result = backend.rns.get_interface_stats()
            stats_list = stats_result.get("interfaces", []) if stats_result else []
        except Exception:
            stats_list = []
        stats_by_name: dict[str, dict] = {}
        for iface in stats_list:
            name = iface.get("name", "")
            short = iface.get("short_name", name)
            stats_by_name[name] = iface
            stats_by_name[short] = iface

        result = []
        for name, cfg in cfg_interfaces.items():
            stats = stats_by_name.get(name, {})
            enabled_str = cfg.get("enabled", cfg.get("interface_enabled", "Yes"))
            iface_type = cfg.get("type", "Unknown")
            result.append({
                "name": name,
                "type": iface_type,
                "enabled": enabled_str.lower() in ("yes", "true", "1"),
                "editable": iface_type in EDITABLE_TYPES,
                "status": stats.get("status"),
                "rxb": stats.get("rxb"),
                "txb": stats.get("txb"),
                # The raw config section, so an edit dialog can show current
                # values -- a remote client can't read the config file
                # directly. ConfigObj parses comma values into lists; flatten
                # those back to the string form the editor writes.
                "config": {
                    k: (", ".join(str(x) for x in v) if isinstance(v, list) else str(v))
                    for k, v in cfg.items()
                },
            })
        return result

    @app.post("/reticulum/interfaces")
    def create_interface(req: CreateInterfaceRequest):
        # Same write path InterfacesWidget._on_add -> _write_interface uses;
        # see trenchchat/core/interfaces_config.py for the shared logic.
        name = req.name.strip()
        if not name:
            return JSONResponse(
                {"ok": False, "error": "interface name is required"}, status_code=400,
            )
        if req.type not in EDITABLE_TYPES:
            return JSONResponse(
                {"ok": False, "error": f"'{req.type}' is not an editable interface type"},
                status_code=400,
            )
        missing = missing_required_field(req.type, req.type_values)
        if missing is not None:
            return JSONResponse(
                {"ok": False, "error": f"'{missing}' is required"}, status_code=400,
            )

        try:
            cfg = build_interface_config_dict(
                name, req.type, req.enabled, req.type_values, req.common_values,
            )
        except InterfaceConfigError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        try:
            write_interface(backend.rns_config_path, name, cfg, is_new=True)
        except DuplicateInterfaceError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=409)
        except InterfaceConfigError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
        return {"ok": True, "name": name, "restart_required": True}

    @app.put("/reticulum/interfaces/{name}")
    def update_interface(name: str, req: UpdateInterfaceRequest):
        # Same write path InterfacesWidget._on_edit -> _write_interface uses.
        cfg_interfaces = load_interfaces_config(backend.rns_config_path)
        existing = cfg_interfaces.get(name)
        if existing is None:
            return JSONResponse(
                {"ok": False, "error": "no such interface"}, status_code=404,
            )
        existing_type = existing.get("type", "")
        if existing_type not in EDITABLE_TYPES:
            return JSONResponse(
                {"ok": False, "error": f"interfaces of type '{existing_type}' "
                                       "cannot be edited"},
                status_code=403,
            )
        if req.type not in EDITABLE_TYPES:
            return JSONResponse(
                {"ok": False, "error": f"'{req.type}' is not an editable interface type"},
                status_code=400,
            )
        missing = missing_required_field(req.type, req.type_values)
        if missing is not None:
            return JSONResponse(
                {"ok": False, "error": f"'{missing}' is required"}, status_code=400,
            )

        try:
            cfg = build_interface_config_dict(
                name, req.type, req.enabled, req.type_values, req.common_values,
            )
        except InterfaceConfigError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        try:
            write_interface(backend.rns_config_path, name, cfg, is_new=False)
        except InterfaceConfigError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
        return {"ok": True, "name": name, "restart_required": True}

    @app.delete("/reticulum/interfaces/{name}")
    def remove_interface(name: str):
        # Same write path InterfacesWidget._on_delete uses.
        try:
            deleted = delete_interface(backend.rns_config_path, name)
        except InterfaceConfigError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
        if not deleted:
            return JSONResponse(
                {"ok": False, "error": "no such interface"}, status_code=404,
            )
        return {"ok": True, "restart_required": True}

    @app.get("/reticulum/discovery")
    def get_discovery():
        # The [reticulum]-section discovery settings plus everything the
        # running RNS instance has discovered on the mesh so far.
        return {
            "settings": load_discovery_settings(backend.rns_config_path),
            "interfaces": list_discovered_interfaces(),
        }

    @app.put("/reticulum/discovery")
    def put_discovery(req: DiscoverySettingsRequest):
        try:
            write_discovery_settings(
                backend.rns_config_path, req.discover_interfaces,
                req.autoconnect_discovered_interfaces, req.required_discovery_value,
            )
        except InterfaceConfigError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
        return {"ok": True, "restart_required": True}

    @app.post("/reticulum/discovery/pin")
    def pin_discovered(req: PinDiscoveredRequest):
        # The section written is built server-side from the discovered entry,
        # so a client can only pin what was actually announced on the mesh.
        try:
            name = pin_discovered_interface(backend.rns_config_path, req.discovery_hash)
        except InterfaceConfigError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return {"ok": True, "name": name, "restart_required": True}

    @app.get("/reticulum/interfaces_suggested")
    def get_suggested_defaults():
        missing = get_missing_suggested_defaults(backend.rns_config_path)
        return {"missing": {
            name: {"target_host": cfg.get("target_host", ""),
                   "target_port": cfg.get("target_port", "")}
            for name, cfg in missing.items()
        }}

    @app.post("/reticulum/interfaces_suggested")
    def add_suggested_defaults():
        # Write the missing bootstrap seeds and enable
        # discovery+autoconnect.
        try:
            added = apply_suggested_defaults(backend.rns_config_path)
        except InterfaceConfigError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
        return {"ok": True, "added": added, "restart_required": True}

    @app.get("/directory")
    def search_directory(q: str = "", scope: str = actions.DIRECTORY_SCOPE_ALL):
        results = backend.user_directory.search(q)
        seen = {r["identity_hash"] for r in results}
        # The announce-fed directory is in-memory, so it starts empty after a
        # restart and transports suppress announce replays. Fall back to
        # path-table peers with recallable identities -- the same peers the
        # network map shows -- so invite lookup keeps working.
        try:
            path_table = backend.rns.get_path_table()
        except Exception:
            path_table = []
        needle = q.strip().lower()
        for entry in path_table:
            dest = entry.get("hash")
            if not isinstance(dest, bytes):
                continue
            identity = RNS.Identity.recall(dest)
            if identity is None:
                continue
            if RNS.Destination.hash(identity.hash, "lxmf", "delivery") != dest:
                continue
            peer_hex = identity.hash.hex()
            if peer_hex == backend.identity.hash_hex or peer_hex in seen:
                continue
            name = resolve_display_name(peer_hex, backend.identity.hash_hex,
                                        backend.storage)
            if needle and needle not in name.lower() and needle not in peer_hex:
                continue
            seen.add(peer_hex)
            results.append({"identity_hash": peer_hex, "display_name": name})
        results.sort(key=lambda r: (r["display_name"].lower(), r["identity_hash"]))
        results = _scoped_directory(results, scope)
        for r in results:
            r["is_online"] = backend.presence_mgr.is_online(r["identity_hash"])
        return results

    def _scoped_directory(results: list[dict], scope: str) -> list[dict]:
        if scope == actions.DIRECTORY_SCOPE_FRIENDS:
            friend_hashes = {f["identity_hash"] for f in backend.friends_mgr.get_friends()}
            shared_hashes: set[str] = set()
        elif scope == actions.DIRECTORY_SCOPE_SHARED:
            friend_hashes = set()
            shared_hashes = actions.shared_channel_peers(backend.storage,
                                                         backend.identity.hash_hex)
        else:
            return results
        return actions.filter_directory_scope(results, scope,
                                              friend_hashes=friend_hashes,
                                              shared_hashes=shared_hashes)

    @app.get("/me/avatar")
    def get_own_avatar():
        data = backend.avatar_mgr.get_own_avatar()
        if data is None:
            return {"avatar_data_b64": None}
        return {"avatar_data_b64": base64.b64encode(data).decode()}

    @app.post("/me/avatar")
    def set_avatar(req: SetAvatarRequest):
        # Same call the real Settings dialog makes after cropping; compress_avatar()
        # does the center-crop + resize + size validation, so the client only
        # needs to hand over the raw picked file.
        try:
            raw = base64.b64decode(req.image_data_b64)
            jpeg = compress_avatar(raw)
            backend.avatar_mgr.set_avatar(jpeg, backend.subscription_mgr.get_subscribers)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except RuntimeError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=429)
        return {"ok": True}

    @app.delete("/me/avatar")
    def remove_avatar():
        try:
            backend.avatar_mgr.remove_avatar(backend.subscription_mgr.get_subscribers)
        except RuntimeError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=429)
        return {"ok": True}

    @app.get("/peers/{peer_hash}/avatar")
    def get_peer_avatar(peer_hash: str):
        row = backend.storage.get_peer_avatar(peer_hash)
        if row is None:
            return {"avatar_data_b64": None}
        return {
            "avatar_data_b64": base64.b64encode(row["avatar_data"]).decode(),
            "avatar_version": row["avatar_version"],
        }

    # --- friends (local-only saved contacts) ---

    @app.get("/friends")
    def list_friends():
        return actions.friends_with_pages(backend.friends_mgr,
                                          backend.node_browser)

    @app.post("/friends")
    def add_friend(req: AddFriendRequest):
        ok = backend.friends_mgr.add_friend(req.identity_hash, req.nickname, req.note)
        if not ok:
            return JSONResponse(
                {"ok": False, "error": "invalid identity hash, or self"}, status_code=400,
            )
        return {"ok": True}

    @app.put("/friends/{identity_hash}")
    def update_friend(identity_hash: str, req: UpdateFriendRequest):
        updates = req.model_dump(exclude_unset=True)
        ok = backend.friends_mgr.update_friend(identity_hash, **updates)
        return {"ok": ok}

    @app.delete("/friends/{identity_hash}")
    def remove_friend(identity_hash: str):
        backend.friends_mgr.remove_friend(identity_hash)
        return {"ok": True}

    # --- friend requests (the handshake that makes a friendship mutual) ---

    @app.get("/friends/requests")
    def list_friend_requests():
        return backend.friends_mgr.get_pending_requests()

    @app.post("/friends/lxmf")
    def add_lxmf_address(req: AddLxmfAddressRequest):
        result = backend.friends_mgr.add_lxmf_address(
            req.lxmf_hash, req.nickname, req.note)
        if result["state"] == "invalid":
            return JSONResponse(
                {"ok": False, "error": "invalid LXMF address"}, status_code=400)
        return {"ok": True, **result}

    @app.post("/friends/requests")
    def send_friend_request(req: FriendRequestRequest):
        ok = actions.send_friend_request(
            backend.friends_mgr, req.identity_hash, note=req.note,
            nickname=req.nickname,
        )
        if not ok:
            return JSONResponse(
                {"ok": False, "error": "invalid identity hash, or self"},
                status_code=400,
            )
        return {"ok": True}

    @app.post("/friends/requests/{identity_hash}/accept")
    def accept_friend_request(identity_hash: str, req: AcceptFriendRequest):
        ok = actions.accept_friend_request(backend.friends_mgr, identity_hash,
                                           nickname=req.nickname)
        return {"ok": ok}

    @app.post("/friends/requests/{identity_hash}/decline")
    def decline_friend_request(identity_hash: str):
        return {"ok": actions.decline_friend_request(backend.friends_mgr, identity_hash)}

    @app.delete("/friends/requests/{identity_hash}")
    def cancel_friend_request(identity_hash: str):
        return {"ok": backend.friends_mgr.cancel_friend_request(identity_hash)}

    # --- direct messages ---
    #
    # A conversation's messages are read through the ordinary
    # /channels/{hash}/messages endpoints using its conversation hash: they are
    # the same rows in the same table, so there is no second message pipeline.

    @app.get("/dms")
    def list_dms():
        return backend.direct_mgr.conversations()

    @app.post("/dms/{peer_hash}")
    def open_dm(peer_hash: str):
        conversation = actions.open_dm(backend.direct_mgr, peer_hash)
        if conversation is None:
            return JSONResponse(
                {"ok": False, "error": "not an accepted friend"}, status_code=403,
            )
        return {"hash": conversation}

    @app.post("/dms/{peer_hash}/messages")
    def send_dm(peer_hash: str, req: SendMessageRequest):
        image_data, error = _decode_attachment(req.image_data_b64)
        if error is not None:
            return error
        msg_id = actions.send_direct_message(
            backend.direct_mgr, backend.messaging, peer_hash, req.content,
            reply_to=req.reply_to, image_data=image_data,
        )
        if msg_id is None:
            return JSONResponse(
                {"ok": False, "error": "not an accepted friend"}, status_code=403,
            )
        conversation = backend.direct_mgr.conversation_hash(peer_hash)
        _on_message(conversation, msg_id)
        return {"ok": True, "hash": conversation, "message_id": msg_id}

    @app.post("/dms/{conversation_hash}/read")
    def mark_dm_read(conversation_hash: str):
        return {"ok": backend.direct_mgr.mark_read(conversation_hash)}

    @app.delete("/dms/{conversation_hash}")
    def delete_dm(conversation_hash: str):
        return {"ok": backend.direct_mgr.delete_conversation(conversation_hash)}

    # --- propagation node (how offline direct messages get through) ---

    @app.get("/propagation")
    def get_propagation():
        return {
            "selected": backend.propagation_nodes.selected,
            "pinned": backend.propagation_nodes.pinned,
            "nodes": backend.propagation_nodes.known_nodes(),
            "sync_state": backend.router.propagation_sync_state(),
        }

    @app.post("/propagation/node")
    def pin_propagation_node(req: PinPropagationNodeRequest):
        if not backend.propagation_nodes.pin(req.node_hash):
            return JSONResponse(
                {"ok": False, "error": "node_hash must be hex"}, status_code=400,
            )
        return {"ok": True, "selected": backend.propagation_nodes.selected}

    @app.post("/propagation/sync")
    def collect_propagated():
        started = backend.collect_propagated()
        if not started:
            return JSONResponse(
                {"ok": False, "error": "no propagation node available"},
                status_code=409,
            )
        return {"ok": True}

    # --- nomad network page browsing ---

    @app.get("/nomad/nodes")
    def list_nomad_nodes():
        return [
            {
                "node_hash": row["node_hash"],
                "display_name": row["display_name"],
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
            }
            for row in backend.node_browser.known_nodes()
        ]

    @app.post("/nomad/browse")
    def nomad_browse(req: NomadBrowseRequest):
        result = actions.browse_nomad_url(
            backend.node_browser, req.url, current_node_hex=req.current_node,
            request_data=req.data, refresh=req.refresh)
        return {"ok": True, **result}

    @app.post("/nomad/fetch")
    def nomad_fetch(req: NomadFetchRequest):
        if req.path.startswith("/file/"):
            fetch_id = backend.node_browser.fetch_file(req.node_hash, req.path)
            kind = "file"
        else:
            fetch_id = backend.node_browser.fetch_page(req.node_hash, req.path)
            kind = "page"
        return {"ok": True, "fetch_id": fetch_id, "node_hash": req.node_hash,
                "path": req.path, "kind": kind}

    @app.get("/nomad/fetch/{fetch_id}")
    def get_nomad_fetch(fetch_id: str):
        status = backend.node_browser.fetch_status(fetch_id)
        if status is None:
            return JSONResponse(
                {"ok": False, "error": "unknown fetch", "reason": "unknown"},
                status_code=404,
            )
        return {"ok": True, **status}

    @app.get("/nomad/page/{node_hash}")
    def get_nomad_page(node_hash: str, path: str = "/page/index.mu"):
        row = backend.node_browser.get_cached_page(node_hash, path)
        if row is None:
            return JSONResponse(
                {"ok": False, "error": "not cached", "reason": "not_cached"},
                status_code=404,
            )
        return {
            "ok": True,
            "content_b64": base64.b64encode(bytes(row["content"])).decode("ascii"),
            "fetched_at": row["fetched_at"],
        }

    @app.get("/nomad/file/{node_hash}")
    def get_nomad_file(node_hash: str, path: str):
        row = backend.node_browser.get_cached_file(node_hash, path)
        if row is None:
            return JSONResponse(
                {"ok": False, "error": "not cached", "reason": "not_cached"},
                status_code=404,
            )
        # The name the node gave the file, when it gave one; the path's
        # basename is only the fallback.
        basename = row["filename"] or path.rsplit("/", 1)[-1] or "download"
        safe_name = "".join(
            c for c in basename if c.isprintable() and c not in '"\\')[:128]
        return Response(
            content=bytes(row["content"]),
            media_type="application/octet-stream",
            # Peer bytes served verbatim: force download, forbid sniffing.
            headers={
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition":
                    f'attachment; filename="{safe_name or "download"}"',
            },
        )

    @app.get("/nomad/identify/{node_hash}")
    def get_nomad_identify(node_hash: str):
        return backend.node_browser.identify_status(node_hash)

    @app.post("/nomad/identify")
    def set_nomad_identify(req: NomadIdentifyRequest):
        status = actions.set_node_identify(
            backend.node_browser, req.node_hash, req.enabled)
        return {"ok": True, **status}

    @app.get("/nomad/bookmarks")
    def list_nomad_bookmarks():
        return [
            {
                "node_hash": row["node_hash"],
                "path": row["path"],
                "label": row["label"],
                "added_at": row["added_at"],
            }
            for row in backend.node_browser.bookmarks()
        ]

    @app.post("/nomad/bookmarks")
    def add_nomad_bookmark(req: NomadBookmarkRequest):
        backend.node_browser.add_bookmark(req.node_hash, req.path, req.label)
        return {"ok": True}

    @app.post("/nomad/bookmarks/delete")
    def delete_nomad_bookmark(req: NomadBookmarkDeleteRequest):
        return {"ok": backend.node_browser.remove_bookmark(req.node_hash,
                                                           req.path)}

    @app.get("/nomad/hosting")
    def get_nomad_hosting():
        return backend.node_browser.hosting_status()

    @app.post("/nomad/hosting")
    def set_nomad_hosting(req: NomadHostingRequest):
        status = actions.set_node_hosting(
            backend.node_browser, enabled=req.enabled, node_name=req.node_name)
        return {"ok": True, **status}

    @app.post("/nomad/hosting/refresh")
    def refresh_nomad_hosting():
        return {"ok": True, **backend.node_browser.refresh_hosted_pages()}

    # --- servers ---

    @app.get("/servers")
    def list_servers():
        return [_server_to_dict(s) for s in backend.server_mgr.list_servers()]

    @app.post("/servers")
    def create_server(req: CreateServerRequest):
        try:
            server_hash = actions.create_server(
                backend.server_mgr, backend.invite_mgr,
                name=req.name, description=req.description,
            )
        except NameInUseError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=409)
        return {"hash": server_hash}

    @app.get("/servers/{server_hash}/channels")
    def list_server_channels(server_hash: str):
        return [_channel_to_dict(c)
                for c in backend.storage.get_server_channels(server_hash)]

    @app.post("/servers/{server_hash}/channels")
    def create_server_channel(server_hash: str, req: CreateServerChannelRequest):
        # Outbound guard: returns None when the caller lacks CREATE_CHANNEL.
        # The core layer re-checks on both the publishing and receiving side.
        try:
            ch_hash = actions.create_channel_in_server(
                backend.storage, backend.channel_mgr, backend.invite_mgr,
                server_hash, backend.identity.hash_hex,
                name=req.name, description=req.description,
            )
        except NameInUseError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=409)
        if ch_hash is None:
            return JSONResponse(
                status_code=403,
                content={"error": f"missing {CREATE_CHANNEL} on this server"},
            )
        return {"hash": ch_hash}

    @app.get("/servers/{server_hash}/members")
    def list_server_members(server_hash: str):
        return [dict(row) for row in backend.storage.get_members(server_hash)]

    @app.get("/servers/{server_hash}/my_permissions")
    def my_server_permissions(server_hash: str):
        my_hex = backend.identity.hash_hex
        has = backend.storage.has_permission
        return {
            "kick": has(server_hash, my_hex, KICK),
            "manage_roles": has(server_hash, my_hex, MANAGE_ROLES),
            "manage_channel": has(server_hash, my_hex, MANAGE_CHANNEL),
            "create_channel": has(server_hash, my_hex, CREATE_CHANNEL),
            "invite": has(server_hash, my_hex, INVITE),
        }

    @app.get("/servers/{server_hash}/permissions")
    def get_server_permissions(server_hash: str):
        perms = backend.storage.get_server_permissions(server_hash)
        return {
            "all_permissions": list(ALL_PERMISSIONS),
            # What each role may actually be granted here. A grant outside
            # this set is dropped by the core on read and on write, so a
            # client offering it would show a control that does nothing.
            "grantable": {
                ROLE_ADMIN: list(offered_permissions(perms, ROLE_ADMIN)),
                ROLE_MEMBER: list(offered_permissions(perms, ROLE_MEMBER)),
            },
            "admin": perms.get(ROLE_ADMIN, []),
            "member": perms.get(ROLE_MEMBER, []),
        }

    @app.post("/servers/{server_hash}/permissions")
    def update_server_permissions(server_hash: str, req: UpdatePermissionsRequest):
        current = backend.storage.get_server_permissions(server_hash)
        new_perms = dict(current)
        new_perms[ROLE_ADMIN] = req.admin
        new_perms[ROLE_MEMBER] = req.member
        return {"ok": actions.edit_server_permissions(
            backend.storage, backend.invite_mgr, server_hash,
            backend.identity.hash_hex, new_perms,
        )}

    @app.post("/servers/{server_hash}/roles")
    def update_server_roles(server_hash: str, req: UpdateRolesRequest):
        # update_membership needs no server-specific variant: has_permission
        # resolves a scope hash the same way for a server as for a channel.
        return {"ok": actions.update_membership(
            backend.storage, backend.invite_mgr, server_hash,
            backend.identity.hash_hex,
            remove_members=_identity_hashes(req.remove_members),
            add_admins=_identity_hashes(req.add_admins),
            remove_admins=_identity_hashes(req.remove_admins),
        )}

    @app.post("/servers/{server_hash}/invite")
    def invite_to_server(server_hash: str, req: InviteRequest):
        backend.invite_mgr.send_invite(server_hash, req.peer_hash_hex)
        return {"ok": True}

    @app.post("/servers/{server_hash}/leave")
    def leave_server(server_hash: str):
        return {"ok": actions.leave_server(
            backend.storage, backend.subscription_mgr, server_hash,
            backend.identity.hash_hex)}

    # --- channels ---

    @app.get("/channels")
    def list_channels():
        # Channels this tester has actually joined -- both join paths
        # (join_public_channel for open-join, the invite/accept flow for
        # invite-only) call storage.subscribe(), and create_channel()
        # subscribes the owner too, so is_subscribed is a reliable "am I
        # part of this channel" signal across every channel type.
        # Channels inside a server are reached through /servers/{h}/channels.
        return [_channel_to_dict(c) for c in backend.storage.get_standalone_channels()
               if backend.storage.is_subscribed(c["hash"])]

    @app.get("/channels/discovered")
    def list_discovered_channels():
        # Channels heard via a real-time announce (see
        # ChannelAnnounceHandler / channel_mgr._on_channel_discovered) but
        # never joined. Only ever open-join channels in practice --
        # announce_channel() refuses to announce anything invite-only
        # regardless of the discoverable flag, so they never reach local
        # storage this way.
        return [_channel_to_dict(c) for c in backend.storage.get_standalone_channels()
               if not backend.storage.is_subscribed(c["hash"])]

    @app.get("/channels/unread")
    def channel_unread_counts():
        # Per-channel unread, the channel counterpart of /dms' unread field.
        # Conversations are excluded by construction: they have no
        # subscriptions row (see docs/direct-messages.md).
        return {"counts": backend.storage.get_unread_counts(backend.identity.hash_hex)}

    @app.post("/channels/{channel_hash}/read")
    def mark_channel_read(channel_hash: str):
        return {"ok": backend.storage.mark_channel_read(channel_hash)}

    @app.post("/channels")
    def create_channel(req: CreateChannelRequest):
        permissions = PRESET_OPEN if req.access == "public" else PRESET_PRIVATE
        try:
            ch_hash = actions.create_channel(
                backend.channel_mgr, backend.invite_mgr,
                name=req.name, description=req.description, permissions=permissions,
            )
        except NameInUseError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=409)
        return {"hash": ch_hash}

    @app.post("/channels/{channel_hash}/join")
    def join_channel(channel_hash: str):
        ok = actions.join_public_channel(backend.storage, backend.subscription_mgr, channel_hash)
        return {"ok": ok}

    @app.post("/channels/{channel_hash}/leave")
    def leave_channel(channel_hash: str):
        ok = actions.leave_channel(backend.storage, backend.subscription_mgr, channel_hash)
        return {"ok": ok}

    @app.get("/channels/{channel_hash}/members")
    def list_members(channel_hash: str):
        return [dict(row) for row in backend.storage.get_members(channel_hash)]

    @app.get("/channels/{channel_hash}/subscribers")
    def list_subscribers(channel_hash: str):
        # Who this tester believes is on an open-join channel, which is what
        # compute_channel_recipients() addresses a send to. The owner builds
        # this from inbound MT_SUBSCRIBE; everyone else holds whatever the
        # owner last broadcast, so the two views can legitimately differ.
        return sorted(backend.subscription_mgr.get_subscribers(channel_hash))

    def _peer_link_quality(peer_hex: str) -> tuple[LinkQuality, int | None]:
        """This peer's link quality and the hop count it was scored from."""
        if peer_hex == backend.identity.hash_hex:
            return LinkQuality.EXCELLENT, 0
        try:
            delivery = RNS.Destination.hash(bytes.fromhex(peer_hex), "lxmf", "delivery")
            for entry in backend.rns.get_path_table():
                dest_h = entry.get("hash")
                if isinstance(dest_h, bytes) and dest_h == delivery:
                    via = entry.get("via")
                    via_hex = via.hex() if isinstance(via, bytes) else None
                    hops = entry.get("hops", 0)
                    return score_path(delivery.hex(), hops, via_hex), hops
        except Exception:
            pass
        return LinkQuality.UNKNOWN, None

    @app.get("/channels/{channel_hash}/presence")
    def channel_presence(channel_hash: str):
        # Roster source follows the channel kind: subscribers for open-join
        # (no members table), members for invite-only. Same shape either way.
        return [
            {
                "identity_hash": peer_hex,
                "display_name": resolve_display_name(
                    peer_hex, backend.identity.hash_hex, backend.storage,
                    backend.config),
                "is_online": backend.presence_mgr.is_online(peer_hex),
                "last_seen": backend.presence_mgr.last_seen_at(peer_hex),
            }
            for peer_hex in actions.channel_roster_hexes(
                backend.storage, backend.subscription_mgr, channel_hash)
        ]

    @app.get("/channels/{channel_hash}/link_quality")
    def channel_link_quality(channel_hash: str):
        """This node's link quality to each other member of the channel.

        The local identity is left out: a link to yourself always scores
        EXCELLENT, and a reading that includes it says nothing about how
        well this node reaches the channel.
        """
        entries = []
        for peer_hex in actions.channel_roster_hexes(
                backend.storage, backend.subscription_mgr, channel_hash):
            if peer_hex == backend.identity.hash_hex:
                continue
            quality, hops = _peer_link_quality(peer_hex)
            entries.append({
                "identity_hash": peer_hex,
                "display_name": resolve_display_name(
                    peer_hex, backend.identity.hash_hex, backend.storage,
                    backend.config),
                "quality": int(quality),
                "quality_label": quality_label(quality),
                "hops": hops,
            })
        return entries

    @app.get("/channels/{channel_hash}/sync_status")
    def get_sync_status(channel_hash: str):
        # Same tracker the GUI will read: who we asked for history, who
        # answered, and whether anything is still missing.
        status = backend.sync_mgr.status.get_status(channel_hash)
        for peer in status["peers"]:
            peer["display_name"] = resolve_display_name(
                peer["identity_hash"], backend.identity.hash_hex,
                backend.storage, backend.config,
            )
        return status

    @app.get("/channels/{channel_hash}/my_permissions")
    def my_permissions(channel_hash: str):
        # Lets the UI gate kick/promote/demote controls -- server-side
        # enforcement in actions.update_membership is the real boundary
        # regardless.
        my_hex = backend.identity.hash_hex
        # send_message mirrors messaging._on_lxmf_message: open-join channels
        # accept anyone, so it is effectively true; otherwise it is the role
        # check the delivery path applies.
        channel = backend.storage.get_channel(channel_hash)
        perms = permissions_from_json(channel["permissions"]) if channel else {}
        send_message = True
        if channel and not is_open_join(perms):
            send_message = backend.storage.has_permission(channel_hash, my_hex, SEND_MESSAGE)
        return {
            "send_message": send_message,
            "invite": backend.storage.has_permission(channel_hash, my_hex, INVITE),
            "kick": backend.storage.has_permission(channel_hash, my_hex, KICK),
            "manage_roles": backend.storage.has_permission(channel_hash, my_hex, MANAGE_ROLES),
            "manage_channel": backend.storage.has_permission(channel_hash, my_hex, MANAGE_CHANNEL),
            "voice_chat": backend.storage.has_permission(channel_hash, my_hex, VOICE_CHAT),
        }

    @app.get("/channels/{channel_hash}/permissions")
    def get_permissions(channel_hash: str):
        perms = backend.storage.get_channel_permissions(channel_hash)
        return {
            "all_permissions": list(ALL_PERMISSIONS),
            # What each role may actually be granted here. A grant outside
            # this set is dropped by the core on read and on write, so a
            # client offering it would show a control that does nothing.
            "grantable": {
                ROLE_ADMIN: list(offered_permissions(perms, ROLE_ADMIN)),
                ROLE_MEMBER: list(offered_permissions(perms, ROLE_MEMBER)),
            },
            "admin": perms.get(ROLE_ADMIN, []),
            "member": perms.get(ROLE_MEMBER, []),
        }

    @app.post("/channels/{channel_hash}/permissions")
    def update_permissions(channel_hash: str, req: UpdatePermissionsRequest):
        # edit_channel_permissions re-checks MANAGE_CHANNEL itself and
        # no-ops if the caller lacks it, same as the client's pre-flight
        # gate.
        row = backend.storage.get_channel(channel_hash)
        if row is not None and row["server_hash"]:
            # A per-channel override would be silently clobbered by the
            # mirror on the server's next accepted document.
            return JSONResponse(
                status_code=409,
                content={"error": "permissions are managed at the server level"},
            )
        current = backend.storage.get_channel_permissions(channel_hash)
        new_perms = dict(current)
        new_perms[ROLE_ADMIN] = req.admin
        new_perms[ROLE_MEMBER] = req.member
        ok = actions.edit_channel_permissions(
            backend.storage, backend.invite_mgr, channel_hash,
            backend.identity.hash_hex, new_perms,
        )
        return {"ok": ok}

    @app.post("/channels/{channel_hash}/roles")
    def update_roles(channel_hash: str, req: UpdateRolesRequest):
        # update_membership() re-applies the KICK/MANAGE_ROLES gate itself,
        # so an unauthorized request here is silently dropped server-side
        # even if a caller bypasses the UI gate above. Its return value
        # distinguishes that silent drop from an actual change, so this
        # response doesn't claim success for a request that had no effect.
        applied = actions.update_membership(
            backend.storage, backend.invite_mgr, channel_hash, backend.identity.hash_hex,
            remove_members=_identity_hashes(req.remove_members),
            add_admins=_identity_hashes(req.add_admins),
            remove_admins=_identity_hashes(req.remove_admins),
        )
        return {"ok": applied}

    # --- invites ---

    @app.post("/channels/{channel_hash}/invite")
    def send_invite(channel_hash: str, req: InviteRequest):
        # Same single-call entry point InviteDialog -> _on_invite_member uses.
        backend.invite_mgr.send_invite(channel_hash, req.peer_hash_hex)
        return {"ok": True}

    @app.get("/invites")
    def list_invites():
        return pending_invites

    @app.post("/invites/{channel_hash}/accept")
    def accept_invite(channel_hash: str):
        # Split on a null token: a held document is confirmed locally, a
        # token invite sends a join request and waits for the document to
        # come back.
        match = next((i for i in pending_invites if i["channel_hash_hex"] == channel_hash), None)
        if match is None:
            return JSONResponse({"ok": False, "error": "no such pending invite"}, status_code=404)
        pending_invites.remove(match)
        if match["token_hex"] is None:
            if not backend.invite_mgr.accept_pending_membership(channel_hash):
                return JSONResponse(
                    {"ok": False, "error": "membership record could not be verified"},
                    status_code=409,
                )
            return {"ok": True}
        backend.accept_invite(
            match["channel_hash_hex"], bytes.fromhex(match["token_hex"]),
            match["expiry"], match["admin_hex"],
        )
        return {"ok": True}

    @app.post("/invites/{channel_hash}/decline")
    def decline_invite(channel_hash: str):
        # Same as _on_decline_invite: local bookkeeping only, nothing sent.
        match = next((i for i in pending_invites if i["channel_hash_hex"] == channel_hash), None)
        if match is not None:
            pending_invites.remove(match)
        if match is not None and match["token_hex"] is None:
            backend.invite_mgr.decline_pending_membership(channel_hash)
        else:
            backend.invite_mgr.decline_invite(channel_hash)
        return {"ok": True}

    # --- messages ---

    @app.get("/channels/{channel_hash}/messages")
    def list_messages(channel_hash: str, limit: int = 200,
                      before_ts: float | None = None):
        return [
            _message_to_dict(
                m,
                _reactions_summary(backend.storage, m["message_id"], backend.identity.hash_hex),
                _delivery_state_for(m),
            )
            for m in backend.storage.get_messages(
                channel_hash, limit=limit, before_ts=before_ts)
        ]

    @app.get("/channels/{channel_hash}/messages/{message_id}/image")
    def get_message_image(channel_hash: str, message_id: str):
        row = backend.storage.get_message(channel_hash, message_id)
        if row is None or not row["image_data"]:
            return JSONResponse({"error": "no image"}, status_code=404)
        data = bytes(row["image_data"])
        return Response(
            content=data,
            media_type="image/gif" if is_gif(data) else "image/jpeg",
            # The type is a guess over peer bytes, which storage accepts
            # without requiring them to parse as an image, so tell the browser
            # not to second-guess it either.
            headers={"X-Content-Type-Options": "nosniff"},
        )

    def _decode_attachment(image_data_b64: str | None):
        """(image bytes or None, error response or None).

        Fails closed: the re-encode is
        the only sanitisation in the pipeline, and it rejects precisely the
        inputs it exists to catch, so forwarding the original bytes would
        bypass it on exactly those.
        """
        if not image_data_b64:
            return None, None
        try:
            raw = base64.b64decode(image_data_b64, validate=True)
        except Exception:
            return None, JSONResponse(
                {"ok": False, "error": "image_data_b64 is not valid base64"},
                status_code=400,
            )
        try:
            image_data, _ = prepare_image(raw)
            return image_data, None
        except Exception as exc:
            RNS.log(f"TrenchChat testenv: image preparation failed: {exc}", RNS.LOG_WARNING)
            return None, None

    @app.post("/channels/{channel_hash}/messages")
    def send_message(channel_hash: str, req: SendMessageRequest):
        image_data, error = _decode_attachment(req.image_data_b64)
        if error is not None:
            return error

        # Messaging fires its message callback only for inbound LXMF, so the
        # sender's own message never reaches the WS bus by itself. Detect the
        # stored message by id and emit it here so browser clients update
        # live too. This also catches the silent-drop case: send_message
        # returns True but stores nothing when the recipient list is empty,
        # which must not be reported to the client as a successful send.
        before = backend.storage.get_latest_message_id(channel_hash)
        sent = actions.send_message(
            backend.storage, backend.subscription_mgr, backend.messaging,
            channel_hash, backend.identity.hash_hex, req.content,
            reply_to=req.reply_to, image_data=image_data,
        )
        after = backend.storage.get_latest_message_id(channel_hash)
        stored = sent and after is not None and after != before
        if stored:
            _on_message(channel_hash, after)
            return {"ok": True}
        return {"ok": False,
                "reason": "no_send_permission" if not sent else "no_recipients"}

    # --- reactions and custom emoji ---

    def _reaction_peers(channel_hash: str) -> list[str]:
        # Same recipient logic send_message uses, minus the SEND_MESSAGE
        # gate (which doesn't apply to reactions).
        # trenchchat_only: a reaction is a TrenchChat control message, and a
        # conversation's other end may be running something else entirely.
        return actions.conversation_recipients(
            backend.storage, backend.subscription_mgr, backend.direct_mgr,
            channel_hash, backend.identity.hash_hex, trenchchat_only=True,
        )

    @app.post("/channels/{channel_hash}/messages/{message_id}/reactions")
    def add_reaction(channel_hash: str, message_id: str, req: AddReactionRequest):
        backend.reaction_mgr.add_reaction(
            channel_hash, message_id, req.emoji_hash, _reaction_peers(channel_hash),
        )
        return {"ok": True}

    @app.delete("/channels/{channel_hash}/messages/{message_id}/reactions/{emoji_hash}")
    def remove_reaction(channel_hash: str, message_id: str, emoji_hash: str):
        backend.reaction_mgr.remove_reaction(
            channel_hash, message_id, emoji_hash, _reaction_peers(channel_hash),
        )
        return {"ok": True}

    @app.get("/emoji")
    def list_emoji():
        return [
            {
                "emoji_hash": row["emoji_hash"],
                "name": row["name"],
                "image_data_b64": base64.b64encode(bytes(row["image_data"])).decode(),
            }
            for row in backend.storage.list_emojis()
        ]

    @app.post("/emoji/import")
    def import_emoji(req: ImportEmojiRequest):
        try:
            raw = base64.b64decode(req.image_data_b64)
            emoji_hash = backend.reaction_mgr.import_emoji(req.name, raw)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return {"ok": True, "emoji_hash": emoji_hash}

    # --- voice ---

    @app.post("/channels/{channel_hash}/voice/join")
    def join_voice(channel_hash: str):
        # Same call the future GUI voice control makes; join_voice_channel()
        # re-applies the VOICE_CHAT gate itself, so an unauthorized request
        # is silently dropped and reported via ok=False.
        joined = actions.join_voice_channel(
            backend.storage, backend.voice_mgr, channel_hash,
            backend.identity.hash_hex,
        )
        return {"ok": joined}

    @app.post("/voice/leave")
    def leave_voice():
        return {"ok": actions.leave_voice_channel(backend.voice_mgr)}

    @app.post("/voice/mute")
    def set_voice_mute(req: VoiceMuteRequest):
        actions.set_voice_muted(backend.voice_mgr, req.muted)
        return {"ok": True}

    @app.get("/channels/{channel_hash}/voice/roster")
    def get_voice_roster(channel_hash: str):
        roster = backend.voice_mgr.get_roster(channel_hash)
        for entry in roster:
            entry["display_name"] = resolve_display_name(
                entry["identity_hash"], backend.identity.hash_hex,
                backend.storage, backend.config,
            )
        return roster

    @app.get("/voice/status")
    def get_voice_status():
        return {
            "channel": backend.voice_mgr.current_channel,
            "muted": backend.voice_mgr.is_muted,
            "stats": backend.voice_mgr.frame_stats(),
            "audio": backend.voice_mgr.audio_status(),
        }

    @app.get("/voice/devices")
    def get_voice_devices():
        return actions.list_audio_devices(backend.config)

    @app.post("/voice/devices")
    def set_voice_devices(req: VoiceDevicesRequest):
        # Persists the choice and rebuilds a live pipeline in place, so a
        # mid-call device switch takes effect without leaving the session.
        actions.set_audio_devices(
            backend.config, backend.voice_mgr,
            req.input_device, req.output_device,
        )
        return {"ok": True, "devices": actions.list_audio_devices(backend.config)}

    @app.post("/voice/test_tone")
    def set_test_tone(req: VoiceToneRequest):
        # Dev-harness control (like /net/offline): drives the headless
        # TonePipeline so two workers can prove the frame path end to end.
        pipeline = backend.voice_mgr.audio_pipeline
        if pipeline is None or not hasattr(pipeline, "set_tone_enabled"):
            return JSONResponse(
                {"ok": False, "error": "no tone pipeline active"},
                status_code=409,
            )
        pipeline.set_tone_enabled(req.enabled)
        return {"ok": True}

    # --- link control ---

    @app.get("/net/status")
    def get_net_status():
        iface = backend.link_interface()
        return {
            "online": backend.link_online(),
            "detached": bool(getattr(iface, "detached", True)) if iface is not None else True,
            "interface": str(iface) if iface is not None else None,
            "rxb": getattr(iface, "rxb", 0) if iface is not None else 0,
            "txb": getattr(iface, "txb", 0) if iface is not None else 0,
        }

    @app.post("/net/offline")
    def net_offline():
        ok = backend.go_offline()
        return {"ok": ok, "online": False}

    @app.post("/net/online")
    def net_online():
        # go_online() only starts the reconnect; the link isn't actually up
        # for another 5-15s, so "online" here always reports False -- poll
        # /net/status (or watch for the net_status WS event) for the real state.
        ok = backend.go_online()
        return {"ok": ok, "online": False}

    # --- live updates ---

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        # Browsers apply neither CORS nor same-origin policy to a WebSocket
        # handshake, so this socket -- which streams every inbound message --
        # is checked here or nowhere.
        if not _token_ok(api_token, _presented_token(ws.headers, ws.query_params)):
            await ws.close(code=1008)
            return
        # Checked before the origin test below, which derives "same origin"
        # from this very header.
        if not _host_allowed(ws.headers.get("host", ""), origins):
            await ws.close(code=1008)
            return
        if not _origin_allowed(ws.headers.get("origin", ""),
                               ws.headers.get("host", ""), origins):
            await ws.close(code=1008)
            return
        await ws.accept()
        await bus.register(ws)
        try:
            while True:
                # Client doesn't need to send anything; just keep the socket open.
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            bus.unregister(ws)

    return app
