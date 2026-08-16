"""
FastAPI wrapper around one tester's Backend.

Every mutating endpoint here calls the exact same functions the Qt GUI
calls: trenchchat.core.actions for multi-step sequences (create channel,
send message, edit permissions, ...) and the core managers directly for
single-call actions (send_invite, send_join_request, ...). There is no
reimplementation of GUI logic here -- see trenchchat/core/actions.py,
which both this file and trenchchat/gui/main_window.py import from.

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
from typing import Any

import RNS
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from trenchchat.core import actions
from trenchchat.core.image import MAX_IMAGE_BYTES, is_gif, prepare_image
from trenchchat.core.avatar import compress_avatar
from trenchchat.core.interfaces_config import (
    DuplicateInterfaceError, EDITABLE_TYPES, InterfaceConfigError,
    build_interface_config_dict, delete_interface, load_interfaces_config,
    missing_required_field, write_interface,
)
from trenchchat.core.permissions import (
    ALL_PERMISSIONS, CREATE_CHANNEL, INVITE, KICK, MANAGE_CHANNEL, MANAGE_ROLES,
    ROLE_ADMIN, ROLE_MEMBER, PRESET_OPEN, PRESET_PRIVATE,
    is_open_join, permissions_from_json,
)
from trenchchat.core.presence import resolve_display_name

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


class UpdateFriendRequest(BaseModel):
    nickname: str | None = None
    note: str | None = None


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
    channel_filter_mode: str | None = None
    channel_filter_hashes: list[str] | None = None
    outbound_propagation_node: str | None = None


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


class VoiceMuteRequest(BaseModel):
    muted: bool


class VoiceToneRequest(BaseModel):
    enabled: bool


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


def _message_to_dict(row, reactions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "message_id": row["message_id"],
        "sender_hash": row["sender_hash"],
        "sender_name": row["sender_name"],
        "content": row["content"],
        "timestamp": row["timestamp"],
        "reply_to": row["reply_to"],
        "has_image": bool(row["image_data"]) if "image_data" in row.keys() else False,
        "reactions": reactions or [],
    }


class EventBus:
    """Fan-out for backend callbacks (fired on RNS/LXMF background threads)
    to any connected WebSocket clients (running in the asyncio event loop).
    Mirrors how main_window.py marshals background-thread callbacks into
    the Qt main thread via signals -- same idea, different thread model."""

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


def create_app(backend: Backend) -> FastAPI:
    app = FastAPI(title=f"TrenchChat tester: {backend.config.display_name}")
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
    )
    bus = EventBus()

    @app.on_event("startup")
    async def _on_startup():
        bus.bind_loop(asyncio.get_running_loop())

    @app.on_event("shutdown")
    async def _on_shutdown():
        # uvicorn runs this on SIGINT/SIGTERM, so both entry points quit
        # gracefully: orchestrator.py's "kill tester", and Ctrl+C on
        # serve_profile.py, which is a real client and really is going away.
        backend.announce_offline()
        backend.close()

    # --- wire backend callbacks (RNS/LXMF background threads) to the bus ---

    def _on_message(channel_hash_hex: str, message_id: str):
        msgs = backend.storage.get_messages(channel_hash_hex)
        row = next((m for m in msgs if m["message_id"] == message_id), None)
        reactions = _reactions_summary(backend.storage, message_id, backend.identity.hash_hex) if row else None
        bus.emit("message", channel_hash=channel_hash_hex,
                message=_message_to_dict(row, reactions) if row else None)

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

    def _on_invite(channel_hash_hex, channel_name, token, expiry, admin_hex):
        # A re-invite to the same channel refreshes the pending entry (new
        # token/expiry) instead of stacking a second one alongside it --
        # mirrors main_window.py's _on_invite_received_main_thread.
        pending_invites[:] = [
            i for i in pending_invites if i["channel_hash_hex"] != channel_hash_hex
        ]
        pending_invites.append({
            "channel_hash_hex": channel_hash_hex, "channel_name": channel_name,
            "token_hex": token.hex(), "expiry": expiry, "admin_hex": admin_hex,
            "scope_kind": backend.invite_mgr.invite_scope_kind(channel_hash_hex),
        })
        bus.emit("invite_received", channel_hash=channel_hash_hex, channel_name=channel_name)

    def _on_channel_joined(channel_hash_hex, channel_name):
        bus.emit("channel_joined", channel_hash=channel_hash_hex, channel_name=channel_name)

    def _on_member_list_updated(channel_hash_hex):
        bus.emit("member_list_updated", channel_hash=channel_hash_hex)

    def _on_channel_discovered(channel_hash_hex, channel_name):
        bus.emit("channel_discovered", channel_hash=channel_hash_hex, channel_name=channel_name)

    def _on_presence_changed(peer_hash_hex: str, is_online: bool):
        bus.emit("presence", identity_hash=peer_hash_hex, is_online=is_online)

    def _on_avatar_changed(identity_hash_hex: str):
        bus.emit("avatar_updated", identity_hash=identity_hash_hex)

    def _on_friend_updated(identity_hash_hex: str):
        bus.emit("friend_updated", identity_hash=identity_hash_hex)

    def _on_reaction_changed(channel_hash_hex: str, message_id: str):
        bus.emit("reaction_updated", channel_hash=channel_hash_hex, message_id=message_id)

    def _on_emoji_received(emoji_hash: str):
        bus.emit("emoji_received", emoji_hash=emoji_hash)

    def _on_sync_status(channel_hash_hex: str):
        bus.emit("sync_status", channel_hash=channel_hash_hex,
                 status=backend.sync_mgr.status.get_status(channel_hash_hex))

    def _on_link_status(is_online: bool):
        bus.emit("net_status", online=is_online)

    def _on_voice_roster(channel_hash_hex: str):
        bus.emit("voice_roster", channel_hash=channel_hash_hex)

    def _on_voice_speaking(channel_hash_hex: str, peer_hex: str, speaking: bool):
        bus.emit("voice_speaking", channel_hash=channel_hash_hex,
                 identity_hash=peer_hex, speaking=speaking)

    def _on_voice_session(state: str):
        bus.emit("voice_session", state=state)

    backend.messaging.add_message_callback(_on_message)
    backend.invite_mgr.add_invite_callback(_on_invite)
    backend.invite_mgr.add_channel_joined_callback(_on_channel_joined)
    backend.invite_mgr.add_member_list_callback(_on_member_list_updated)
    backend.channel_mgr.add_channel_discovered_callback(_on_channel_discovered)
    backend.presence_mgr.add_presence_callback(_on_presence_changed)
    backend.avatar_mgr.add_avatar_callback(_on_avatar_changed)
    backend.friends_mgr.add_friends_callback(_on_friend_updated)
    backend.reaction_mgr.add_reaction_callback(_on_reaction_changed)
    backend.reaction_mgr.add_emoji_callback(_on_emoji_received)
    backend.sync_mgr.status.add_status_callback(_on_sync_status)
    backend.add_link_callback(_on_link_status)
    backend.voice_mgr.add_roster_callback(_on_voice_roster)
    backend.voice_mgr.add_speaking_callback(_on_voice_speaking)
    backend.voice_mgr.add_session_callback(_on_voice_session)

    # --- identity ---

    @app.get("/me")
    def get_me():
        return {
            "hash_hex": backend.identity.hash_hex,
            "display_name": backend.config.display_name,
        }

    @app.post("/me/display_name")
    def set_display_name(req: SetDisplayNameRequest):
        # Same call the real Settings dialog makes.
        backend.router.set_display_name(req.display_name)
        # Propagate promptly rather than waiting for the periodic
        # heartbeat's next reannounce cycle.
        backend.router.announce_user()
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

    @app.get("/peers/{peer_hash}/presence")
    def get_peer_presence(peer_hash: str):
        return {
            "identity_hash": peer_hash,
            "is_online": backend.presence_mgr.is_online(peer_hash),
        }

    @app.get("/network/map")
    def get_network_map():
        # Same free function NetworkMapDialog calls. Imported from core, not
        # the Qt module, so this works in headless installs without PyQt6.
        from trenchchat.core.network_map import gather_network_data
        return gather_network_data(backend.rns, backend.identity.hash_hex, backend.storage)

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
                # values -- the Qt widget reads the config file directly, but
                # a remote client can't. ConfigObj parses comma values into
                # lists; flatten those back to the string form the editor
                # writes.
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

        cfg = build_interface_config_dict(
            name, req.type, req.enabled, req.type_values, req.common_values,
        )
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

        cfg = build_interface_config_dict(
            name, req.type, req.enabled, req.type_values, req.common_values,
        )
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

    @app.get("/directory")
    def search_directory(q: str = ""):
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
        for r in results:
            r["is_online"] = backend.presence_mgr.is_online(r["identity_hash"])
        return results

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
        return backend.friends_mgr.get_friends()

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

    # --- servers ---

    @app.get("/servers")
    def list_servers():
        return [_server_to_dict(s) for s in backend.server_mgr.list_servers()]

    @app.post("/servers")
    def create_server(req: CreateServerRequest):
        return {"hash": actions.create_server(
            backend.server_mgr, backend.invite_mgr,
            name=req.name, description=req.description,
        )}

    @app.get("/servers/{server_hash}/channels")
    def list_server_channels(server_hash: str):
        return [_channel_to_dict(c)
                for c in backend.storage.get_server_channels(server_hash)]

    @app.post("/servers/{server_hash}/channels")
    def create_server_channel(server_hash: str, req: CreateServerChannelRequest):
        # Outbound guard: returns None when the caller lacks CREATE_CHANNEL.
        # The core layer re-checks on both the publishing and receiving side.
        ch_hash = actions.create_channel_in_server(
            backend.storage, backend.channel_mgr, backend.invite_mgr,
            server_hash, backend.identity.hash_hex,
            name=req.name, description=req.description,
        )
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
            remove_members=[bytes.fromhex(h) for h in req.remove_members] or None,
            add_admins=[bytes.fromhex(h) for h in req.add_admins] or None,
            remove_admins=[bytes.fromhex(h) for h in req.remove_admins] or None,
        )}

    @app.post("/servers/{server_hash}/invite")
    def invite_to_server(server_hash: str, req: InviteRequest):
        backend.invite_mgr.send_invite(server_hash, req.peer_hash_hex)
        return {"ok": True}

    @app.post("/servers/{server_hash}/leave")
    def leave_server(server_hash: str):
        return {"ok": actions.leave_server(
            backend.storage, backend.subscription_mgr, server_hash)}

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

    @app.post("/channels")
    def create_channel(req: CreateChannelRequest):
        permissions = PRESET_OPEN if req.access == "public" else PRESET_PRIVATE
        ch_hash = actions.create_channel(
            backend.channel_mgr, backend.invite_mgr,
            name=req.name, description=req.description, permissions=permissions,
        )
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
        # Lets the UI gate kick/promote/demote controls the same way
        # main_window.py's _on_view_members does -- server-side enforcement
        # in actions.update_membership is the real boundary regardless.
        my_hex = backend.identity.hash_hex
        return {
            "kick": backend.storage.has_permission(channel_hash, my_hex, KICK),
            "manage_roles": backend.storage.has_permission(channel_hash, my_hex, MANAGE_ROLES),
            "manage_channel": backend.storage.has_permission(channel_hash, my_hex, MANAGE_CHANNEL),
        }

    @app.get("/channels/{channel_hash}/permissions")
    def get_permissions(channel_hash: str):
        perms = backend.storage.get_channel_permissions(channel_hash)
        return {
            "all_permissions": list(ALL_PERMISSIONS),
            "admin": perms.get(ROLE_ADMIN, []),
            "member": perms.get(ROLE_MEMBER, []),
        }

    @app.post("/channels/{channel_hash}/permissions")
    def update_permissions(channel_hash: str, req: UpdatePermissionsRequest):
        # Same entry point main_window.py's _on_edit_permissions uses.
        # edit_channel_permissions re-checks MANAGE_CHANNEL itself and
        # no-ops if the caller lacks it, same as the GUI's pre-flight gate.
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
        # Same entry point main_window.py's _on_view_members uses --
        # update_membership() re-applies the KICK/MANAGE_ROLES gate itself,
        # so an unauthorized request here is silently dropped server-side
        # even if a caller bypasses the UI gate above. Its return value
        # distinguishes that silent drop from an actual change, so this
        # response doesn't claim success for a request that had no effect.
        applied = actions.update_membership(
            backend.storage, backend.invite_mgr, channel_hash, backend.identity.hash_hex,
            remove_members=[bytes.fromhex(h) for h in req.remove_members] or None,
            add_admins=[bytes.fromhex(h) for h in req.add_admins] or None,
            remove_admins=[bytes.fromhex(h) for h in req.remove_admins] or None,
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
        # Same call main_window.py's _on_accept_invite makes (via Backend.accept_invite).
        match = next((i for i in pending_invites if i["channel_hash_hex"] == channel_hash), None)
        if match is None:
            return JSONResponse({"ok": False, "error": "no such pending invite"}, status_code=404)
        pending_invites.remove(match)
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
        backend.invite_mgr.decline_invite(channel_hash)
        return {"ok": True}

    # --- messages ---

    @app.get("/channels/{channel_hash}/messages")
    def list_messages(channel_hash: str):
        return [
            _message_to_dict(
                m, _reactions_summary(backend.storage, m["message_id"], backend.identity.hash_hex)
            )
            for m in backend.storage.get_messages(channel_hash)
        ]

    @app.get("/channels/{channel_hash}/messages/{message_id}/image")
    def get_message_image(channel_hash: str, message_id: str):
        msgs = backend.storage.get_messages(channel_hash)
        row = next((m for m in msgs if m["message_id"] == message_id), None)
        if row is None or not row["image_data"]:
            return JSONResponse({"error": "no image"}, status_code=404)
        data = bytes(row["image_data"])
        return Response(content=data, media_type="image/gif" if is_gif(data) else "image/jpeg")

    @app.post("/channels/{channel_hash}/messages")
    def send_message(channel_hash: str, req: SendMessageRequest):
        image_data = None
        if req.image_data_b64:
            raw = base64.b64decode(req.image_data_b64)
            # Same compress-or-fall-back-to-raw pattern main_window.py's
            # _on_send_message uses.
            try:
                image_data, _ = prepare_image(raw)
            except Exception as exc:
                RNS.log(f"TrenchChat testenv: image preparation failed: {exc}", RNS.LOG_WARNING)
                if len(raw) <= MAX_IMAGE_BYTES:
                    image_data = raw

        # Messaging fires its message callback only for inbound LXMF, so the
        # sender's own message never reaches the WS bus by itself -- the Qt
        # client refreshes its own view after sending instead. Detect the
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
        # Same recipient logic _on_send_message uses, minus the SEND_MESSAGE
        # gate (which doesn't apply to reactions) -- see main_window.py's
        # _get_reaction_peers.
        return actions.compute_channel_recipients(
            backend.storage, backend.subscription_mgr, channel_hash, backend.identity.hash_hex,
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
