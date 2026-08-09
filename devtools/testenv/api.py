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
from trenchchat.core.permissions import (
    ALL_PERMISSIONS, KICK, MANAGE_CHANNEL, MANAGE_ROLES, ROLE_ADMIN, ROLE_MEMBER,
    PRESET_OPEN, PRESET_PRIVATE, is_open_join, permissions_from_json,
)

from backend_core import Backend


class CreateChannelRequest(BaseModel):
    name: str
    description: str = ""
    access: str = "public"  # "public" | "invite"


class SetDisplayNameRequest(BaseModel):
    display_name: str


class SetAvatarRequest(BaseModel):
    image_data_b64: str


class AddReactionRequest(BaseModel):
    emoji_hash: str


class ImportEmojiRequest(BaseModel):
    name: str
    image_data_b64: str


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


def _channel_to_dict(row) -> dict[str, Any]:
    return {
        "hash": row["hash"],
        "name": row["name"],
        "description": row["description"],
        "creator_hash": row["creator_hash"],
        "open_join": is_open_join(permissions_from_json(row["permissions"])),
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

    # --- wire backend callbacks (RNS/LXMF background threads) to the bus ---

    def _on_message(channel_hash_hex: str, message_id: str):
        msgs = backend.storage.get_messages(channel_hash_hex)
        row = next((m for m in msgs if m["message_id"] == message_id), None)
        reactions = _reactions_summary(backend.storage, message_id, backend.identity.hash_hex) if row else None
        bus.emit("message", channel_hash=channel_hash_hex,
                message=_message_to_dict(row, reactions) if row else None)

    # Pending invites, exactly like MainWindow._pending_invites -- nothing
    # is sent to the network until the user explicitly accepts or declines.
    pending_invites: list[dict[str, Any]] = []

    def _on_invite(channel_hash_hex, channel_name, token, expiry, admin_hex):
        pending_invites.append({
            "channel_hash_hex": channel_hash_hex, "channel_name": channel_name,
            "token_hex": token.hex(), "expiry": expiry, "admin_hex": admin_hex,
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

    def _on_reaction_changed(channel_hash_hex: str, message_id: str):
        bus.emit("reaction_updated", channel_hash=channel_hash_hex, message_id=message_id)

    def _on_emoji_received(emoji_hash: str):
        bus.emit("emoji_received", emoji_hash=emoji_hash)

    backend.messaging.add_message_callback(_on_message)
    backend.invite_mgr.add_invite_callback(_on_invite)
    backend.invite_mgr.add_channel_joined_callback(_on_channel_joined)
    backend.invite_mgr.add_member_list_callback(_on_member_list_updated)
    backend.channel_mgr.add_channel_discovered_callback(_on_channel_discovered)
    backend.presence_mgr.add_presence_callback(_on_presence_changed)
    backend.avatar_mgr.add_avatar_callback(_on_avatar_changed)
    backend.reaction_mgr.add_reaction_callback(_on_reaction_changed)
    backend.reaction_mgr.add_emoji_callback(_on_emoji_received)

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

    @app.get("/peers/{peer_hash}/presence")
    def get_peer_presence(peer_hash: str):
        return {
            "identity_hash": peer_hash,
            "is_online": backend.presence_mgr.is_online(peer_hash),
        }

    @app.get("/network/map")
    def get_network_map():
        # Same free function NetworkMapDialog calls -- no GUI coupling.
        from trenchchat.gui.network_map import gather_network_data
        return gather_network_data(backend.rns, backend.identity.hash_hex, backend.storage)

    @app.get("/reticulum/interfaces")
    def get_interfaces():
        # Same data source InterfacesWidget.load_interfaces() merges: the
        # configured [interfaces] section plus live rns.get_interface_stats().
        # Read-only -- live editing is a separate, bigger scope-add.
        from trenchchat.gui.interfaces_widget import load_interfaces_config

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
            result.append({
                "name": name,
                "type": cfg.get("type", "Unknown"),
                "enabled": enabled_str.lower() in ("yes", "true", "1"),
                "status": stats.get("status"),
                "rxb": stats.get("rxb"),
                "txb": stats.get("txb"),
            })
        return result

    @app.get("/directory")
    def search_directory(q: str = ""):
        results = backend.user_directory.search(q)
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

    # --- channels ---

    @app.get("/channels")
    def list_channels():
        # Channels this tester has actually joined -- both join paths
        # (join_public_channel for open-join, the invite/accept flow for
        # invite-only) call storage.subscribe(), and create_channel()
        # subscribes the owner too, so is_subscribed is a reliable "am I
        # part of this channel" signal across every channel type.
        return [_channel_to_dict(c) for c in backend.storage.get_all_channels()
               if backend.storage.is_subscribed(c["hash"])]

    @app.get("/channels/discovered")
    def list_discovered_channels():
        # Channels heard via a real-time announce (see
        # ChannelAnnounceHandler / channel_mgr._on_channel_discovered) but
        # never joined. Only ever open-join channels in practice --
        # invite-only channels aren't broadcast (is_discoverable is False
        # for PRESET_PRIVATE), so they never reach local storage this way.
        return [_channel_to_dict(c) for c in backend.storage.get_all_channels()
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
        # even if a caller bypasses the UI gate above.
        actions.update_membership(
            backend.storage, backend.invite_mgr, channel_hash, backend.identity.hash_hex,
            remove_members=[bytes.fromhex(h) for h in req.remove_members] or None,
            add_admins=[bytes.fromhex(h) for h in req.add_admins] or None,
            remove_admins=[bytes.fromhex(h) for h in req.remove_admins] or None,
        )
        return {"ok": True}

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

        sent = actions.send_message(
            backend.storage, backend.subscription_mgr, backend.messaging,
            channel_hash, backend.identity.hash_hex, req.content,
            reply_to=req.reply_to, image_data=image_data,
        )
        return {"ok": sent}

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
