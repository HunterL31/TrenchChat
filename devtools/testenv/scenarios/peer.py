"""
HTTP clients for one tester's API and for the orchestrator.

Every method is one endpoint call. No business logic lives here: a scenario
reads as a sequence of user actions, and every action goes through the same
api.py -> actions.py path the Flutter client uses.
"""

import httpx

_ORCH_PORT = 8800
_TIMEOUT = 15.0


class Peer:
    """One tester, addressed by its API port."""

    def __init__(self, tag: str, api_port: int):
        self.tag = tag
        self.api_port = api_port
        self._base = f"http://127.0.0.1:{api_port}"
        self._client = httpx.Client(timeout=_TIMEOUT)
        self._hash: str | None = None

    def __repr__(self) -> str:
        return f"<Peer {self.tag}>"

    def close(self) -> None:
        self._client.close()

    # --- plumbing ---

    def _get(self, path: str, **params):
        r = self._client.get(self._base + path, params=params or None)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict | None = None):
        r = self._client.post(self._base + path, json=body if body is not None else {})
        r.raise_for_status()
        return r.json()

    def _delete(self, path: str):
        r = self._client.delete(self._base + path)
        r.raise_for_status()
        return r.json()

    def alive(self) -> bool:
        """True if this tester's API is answering. False across a kill."""
        try:
            self._client.get(self._base + "/me", timeout=1.0).raise_for_status()
            return True
        except httpx.HTTPError:
            return False

    # --- identity ---

    def me(self) -> dict:
        return self._get("/me")

    @property
    def hash(self) -> str:
        """Cached identity hash. Stable across restarts, so caching is safe."""
        if self._hash is None:
            self._hash = self.me()["hash_hex"]
        return self._hash

    def forget_hash(self) -> None:
        """Drop the cached hash, for scenarios that wipe a tester's data dir."""
        self._hash = None

    def set_display_name(self, name: str) -> dict:
        return self._post("/me/display_name", {"display_name": name})

    def directory(self, q: str = "") -> list[dict]:
        return self._get("/directory", q=q)

    def presence(self, peer_hash: str) -> dict:
        return self._get(f"/peers/{peer_hash}/presence")

    # --- channels ---

    def create_channel(self, name: str, access: str = "public",
                       description: str = "") -> str:
        return self._post("/channels", {"name": name, "access": access,
                                        "description": description})["hash"]

    def channels(self) -> list[dict]:
        return self._get("/channels")

    def discovered(self) -> list[dict]:
        return self._get("/channels/discovered")

    def join(self, channel_hash: str) -> bool:
        return self._post(f"/channels/{channel_hash}/join")["ok"]

    def leave(self, channel_hash: str) -> bool:
        return self._post(f"/channels/{channel_hash}/leave")["ok"]

    def members(self, channel_hash: str) -> list[dict]:
        return self._get(f"/channels/{channel_hash}/members")

    def sync_status(self, channel_hash: str) -> dict:
        return self._get(f"/channels/{channel_hash}/sync_status")

    def my_permissions(self, channel_hash: str) -> dict:
        return self._get(f"/channels/{channel_hash}/my_permissions")

    def permissions(self, channel_hash: str) -> dict:
        return self._get(f"/channels/{channel_hash}/permissions")

    def set_permissions(self, channel_hash: str, admin: list[str],
                        member: list[str]) -> bool:
        return self._post(f"/channels/{channel_hash}/permissions",
                          {"admin": admin, "member": member})["ok"]

    def set_roles(self, channel_hash: str, *, remove_members: list[str] | None = None,
                  add_admins: list[str] | None = None,
                  remove_admins: list[str] | None = None) -> bool:
        return self._post(f"/channels/{channel_hash}/roles", {
            "remove_members": remove_members or [],
            "add_admins": add_admins or [],
            "remove_admins": remove_admins or [],
        })["ok"]

    # --- invites ---

    def invite(self, channel_hash: str, peer_hash: str) -> dict:
        return self._post(f"/channels/{channel_hash}/invite", {"peer_hash_hex": peer_hash})

    def invites(self) -> list[dict]:
        return self._get("/invites")

    def accept_invite(self, channel_hash: str) -> dict:
        return self._post(f"/invites/{channel_hash}/accept")

    def decline_invite(self, channel_hash: str) -> dict:
        return self._post(f"/invites/{channel_hash}/decline")

    # --- messages ---

    def send(self, channel_hash: str, content: str,
             reply_to: str | None = None) -> dict:
        return self._post(f"/channels/{channel_hash}/messages",
                          {"content": content, "reply_to": reply_to})

    def messages(self, channel_hash: str) -> list[dict]:
        return self._get(f"/channels/{channel_hash}/messages")

    def contents(self, channel_hash: str) -> set[str]:
        """Message bodies held for a channel, as a set."""
        return {m["content"] for m in self.messages(channel_hash)}

    def react(self, channel_hash: str, message_id: str, emoji_hash: str) -> dict:
        return self._post(f"/channels/{channel_hash}/messages/{message_id}/reactions",
                          {"emoji_hash": emoji_hash})

    def unreact(self, channel_hash: str, message_id: str, emoji_hash: str) -> dict:
        return self._delete(
            f"/channels/{channel_hash}/messages/{message_id}/reactions/{emoji_hash}"
        )

    # --- link control ---

    def net_status(self) -> dict:
        return self._get("/net/status")

    def go_offline(self) -> dict:
        return self._post("/net/offline")

    def go_online(self) -> dict:
        return self._post("/net/online")


class Orchestrator:
    """Process and link lifecycle for the whole environment."""

    def __init__(self, port: int = _ORCH_PORT):
        self._base = f"http://127.0.0.1:{port}"
        self._client = httpx.Client(timeout=180.0)

    def close(self) -> None:
        self._client.close()

    def _post(self, path: str, body: dict | None = None):
        r = self._client.post(self._base + path, json=body if body is not None else {})
        return r.json()

    def config(self) -> dict:
        r = self._client.get(self._base + "/config", timeout=5.0)
        r.raise_for_status()
        return r.json()

    def status(self) -> dict:
        r = self._client.get(self._base + "/status", timeout=10.0)
        r.raise_for_status()
        return r.json()

    def up(self) -> bool:
        try:
            self.config()
            return True
        except httpx.HTTPError:
            return False

    def reset(self) -> dict:
        return self._post("/reset")

    def kill(self, tag: str) -> dict:
        return self._post(f"/testers/{tag}/kill")

    def start(self, tag: str) -> dict:
        return self._post(f"/testers/{tag}/start")

    def restart(self, tag: str) -> dict:
        return self._post(f"/testers/{tag}/restart")

    def reset_tester(self, tag: str) -> dict:
        return self._post(f"/testers/{tag}/reset")

    def link_profile(self, tag: str, profile: str, **overrides) -> dict:
        return self._post(f"/testers/{tag}/link_profile", {"profile": profile, **overrides})

    def hub_kill(self) -> dict:
        return self._post("/hub/kill")

    def hub_start(self) -> dict:
        return self._post("/hub/start")

    def hub_restart(self) -> dict:
        return self._post("/hub/restart")
