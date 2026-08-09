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
import json
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from trenchchat.core import actions
from trenchchat.core.permissions import (
    PRESET_OPEN, PRESET_PRIVATE, is_open_join, permissions_from_json,
)

from backend_core import Backend


class CreateChannelRequest(BaseModel):
    name: str
    description: str = ""
    access: str = "public"  # "public" | "invite"


class SetDisplayNameRequest(BaseModel):
    display_name: str


class SendMessageRequest(BaseModel):
    content: str
    reply_to: str | None = None


class InviteRequest(BaseModel):
    peer_hash_hex: str


class JoinRequestRequest(BaseModel):
    channel_hash_hex: str
    token_hex: str
    expiry: float
    admin_hash_hex: str


class KickRequest(BaseModel):
    peer_hash_hex: str


def _channel_to_dict(row) -> dict[str, Any]:
    return {
        "hash": row["hash"],
        "name": row["name"],
        "description": row["description"],
        "creator_hash": row["creator_hash"],
        "open_join": is_open_join(permissions_from_json(row["permissions"])),
        "created_at": row["created_at"],
    }


def _message_to_dict(row) -> dict[str, Any]:
    return {
        "message_id": row["message_id"],
        "sender_hash": row["sender_hash"],
        "sender_name": row["sender_name"],
        "content": row["content"],
        "timestamp": row["timestamp"],
        "reply_to": row["reply_to"],
        "has_image": bool(row["image_data"]) if "image_data" in row.keys() else False,
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
        bus.emit("message", channel_hash=channel_hash_hex,
                message=_message_to_dict(row) if row else None)

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

    backend.messaging.add_message_callback(_on_message)
    backend.invite_mgr.add_invite_callback(_on_invite)
    backend.invite_mgr.add_channel_joined_callback(_on_channel_joined)
    backend.invite_mgr.add_member_list_callback(_on_member_list_updated)

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

    # --- channels ---

    @app.get("/channels")
    def list_channels():
        return [_channel_to_dict(c) for c in backend.storage.get_all_channels()]

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

    @app.post("/channels/{channel_hash}/kick")
    def kick_member(channel_hash: str, req: KickRequest):
        actions.update_membership(
            backend.storage, backend.invite_mgr, channel_hash, backend.identity.hash_hex,
            remove_members=[bytes.fromhex(req.peer_hash_hex)],
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
        return [_message_to_dict(m) for m in backend.storage.get_messages(channel_hash)]

    @app.post("/channels/{channel_hash}/messages")
    def send_message(channel_hash: str, req: SendMessageRequest):
        sent = actions.send_message(
            backend.storage, backend.subscription_mgr, backend.messaging,
            channel_hash, backend.identity.hash_hex, req.content,
            reply_to=req.reply_to,
        )
        return {"ok": sent}

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
