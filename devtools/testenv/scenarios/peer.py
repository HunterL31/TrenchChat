"""
HTTP clients for one tester's API and for the orchestrator.

Every method is one endpoint call. No business logic lives here: a scenario
reads as a sequence of user actions, and every action goes through the same
api.py -> actions.py path the Flutter client uses.
"""

import httpx

_ORCH_PORT = 8800
_TIMEOUT = 15.0
_TOKEN_HEADER = "x-tc-token"
TOKEN_QUERY_PARAM = "token"


def _auth_headers(token: str) -> dict[str, str]:
    return {_TOKEN_HEADER: token} if token else {}


class Peer:
    """One tester, addressed by its API port.

    Every route on a tester's API requires the environment's token; without
    it each call answers 401. The orchestrator mints one for all of them and
    hands it out through its own /config.
    """

    def __init__(self, tag: str, api_port: int, token: str = ""):
        self.tag = tag
        self.api_port = api_port
        self.token = token
        self._base = f"http://127.0.0.1:{api_port}"
        self._client = httpx.Client(timeout=_TIMEOUT, headers=_auth_headers(token))
        self._hash: str | None = None

    def __repr__(self) -> str:
        return f"<Peer {self.tag}>"

    @property
    def base_url(self) -> str:
        return self._base

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

    def post_status(self, path: str, body: dict | None = None) -> tuple[int, dict]:
        """POST returning (status_code, body) instead of raising.

        Some endpoints refuse with a status rather than {"ok": false} -- a
        server channel's permissions with 409, an unpermitted channel creation
        with 403 -- and the refusal is the thing under test.
        """
        r = self._client.post(self._base + path, json=body if body is not None else {})
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {}

    def raw_get(self, path: str, *, token: str | None = "", headers: dict | None = None,
                params: dict | None = None) -> int:
        """GET without the client's own auth, returning just the status.

        token="" sends none, token=None sends this peer's real one, and any
        other value is sent verbatim. Used to prove the API refuses a caller
        rather than to read what it returns.
        """
        sent = dict(headers or {})
        if token is None:
            sent.update(_auth_headers(self.token))
        elif token:
            sent[_TOKEN_HEADER] = token
        with httpx.Client(timeout=_TIMEOUT) as bare:
            return bare.get(self._base + path, headers=sent, params=params).status_code

    def raw_post(self, path: str, body: dict | None = None, *,
                 token: str | None = "", headers: dict | None = None) -> int:
        """POST without the client's own auth, returning just the status."""
        sent = dict(headers or {})
        if token is None:
            sent.update(_auth_headers(self.token))
        elif token:
            sent[_TOKEN_HEADER] = token
        with httpx.Client(timeout=_TIMEOUT) as bare:
            return bare.post(self._base + path, json=body or {},
                             headers=sent).status_code

    def ws_probe(self, *, token: str | None = "", origin: str | None = None,
                 query_token: bool = False) -> str:
        """Try to open the event socket. Returns "open", or "closed:<code>".

        A browser sets no headers on a WebSocket handshake, so the query
        parameter is the only way it can authenticate -- both routes are worth
        proving. CORS does not apply to this handshake at all, which is why the
        backend checks Origin here itself.
        """
        import asyncio

        import websockets

        url = self._base.replace("http://", "ws://") + "/ws"
        real = self.token if token is None else token
        headers = {}
        if real and not query_token:
            headers[_TOKEN_HEADER] = real
        if real and query_token:
            url += f"?{TOKEN_QUERY_PARAM}={real}"
        if origin is not None:
            headers["Origin"] = origin

        async def attempt() -> str:
            try:
                async with websockets.connect(
                    url, additional_headers=headers, open_timeout=10,
                ) as ws:
                    # The backend closes an unauthorised socket right after the
                    # handshake, so an accepted handshake is not yet a pass.
                    try:
                        await asyncio.wait_for(ws.recv(), timeout=2.0)
                    except asyncio.TimeoutError:
                        return "open"
                    except websockets.ConnectionClosed as closed:
                        return f"closed:{closed.rcvd.code}"
                    return "open"
            except websockets.InvalidStatus as e:
                return f"rejected:{e.response.status_code}"
            except websockets.ConnectionClosed as e:
                return f"closed:{e.rcvd.code if e.rcvd else 'unknown'}"

        return asyncio.run(attempt())

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

    # --- avatar ---

    def set_avatar(self, image_data_b64: str) -> dict:
        return self._post("/me/avatar", {"image_data_b64": image_data_b64})

    def own_avatar(self) -> dict:
        return self._get("/me/avatar")

    def remove_avatar(self) -> dict:
        return self._delete("/me/avatar")

    def try_remove_avatar(self) -> int:
        """Remove the avatar, returning the status instead of raising.

        AvatarManager enforces one change per SEND_RATE_LIMIT_SECS and answers
        429 until it elapses, so a set-then-remove sequence has to wait it out.
        """
        return self._client.delete(self._base + "/me/avatar").status_code

    def peer_avatar(self, peer_hash: str) -> dict:
        return self._get(f"/peers/{peer_hash}/avatar")

    # --- friends ---

    def friends(self) -> list[dict]:
        return self._get("/friends")

    def add_friend(self, identity_hash: str, nickname: str = "", note: str = "") -> dict:
        return self._post("/friends", {"identity_hash": identity_hash,
                                       "nickname": nickname, "note": note})

    def remove_friend(self, identity_hash: str) -> dict:
        return self._delete(f"/friends/{identity_hash}")

    # --- servers ---

    def create_server(self, name: str, description: str = "") -> str:
        return self._post("/servers", {"name": name, "description": description})["hash"]

    def servers(self) -> list[dict]:
        return self._get("/servers")

    def server_channels(self, server_hash: str) -> list[dict]:
        return self._get(f"/servers/{server_hash}/channels")

    def create_server_channel(self, server_hash: str, name: str,
                              description: str = "") -> dict:
        return self._post(f"/servers/{server_hash}/channels",
                          {"name": name, "description": description})

    def server_members(self, server_hash: str) -> list[dict]:
        return self._get(f"/servers/{server_hash}/members")

    def server_permissions(self, server_hash: str) -> dict:
        return self._get(f"/servers/{server_hash}/permissions")

    def set_server_permissions(self, server_hash: str, admin: list[str],
                               member: list[str]) -> bool:
        return self._post(f"/servers/{server_hash}/permissions",
                          {"admin": admin, "member": member})["ok"]

    def set_server_roles(self, server_hash: str, *,
                         remove_members: list[str] | None = None,
                         add_admins: list[str] | None = None,
                         remove_admins: list[str] | None = None) -> bool:
        return self._post(f"/servers/{server_hash}/roles", {
            "remove_members": remove_members or [],
            "add_admins": add_admins or [],
            "remove_admins": remove_admins or [],
        })["ok"]

    def invite_to_server(self, server_hash: str, peer_hash: str) -> dict:
        return self._post(f"/servers/{server_hash}/invite", {"peer_hash_hex": peer_hash})

    def leave_server(self, server_hash: str) -> dict:
        return self._post(f"/servers/{server_hash}/leave")

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

    def subscribers(self, channel_hash: str) -> list[str]:
        return self._get(f"/channels/{channel_hash}/subscribers")

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
             reply_to: str | None = None, image_data_b64: str | None = None) -> dict:
        return self._post(f"/channels/{channel_hash}/messages",
                          {"content": content, "reply_to": reply_to,
                           "image_data_b64": image_data_b64})

    def message_image_status(self, channel_hash: str, message_id: str) -> int:
        """Status of fetching a message's attachment; 404 when none is stored."""
        return self._client.get(
            f"{self._base}/channels/{channel_hash}/messages/{message_id}/image"
        ).status_code

    def message_by_content(self, channel_hash: str, content: str) -> dict | None:
        for m in self.messages(channel_hash):
            if m["content"] == content:
                return m
        return None

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

    # --- voice ---

    def join_voice(self, channel_hash: str) -> bool:
        return self._post(f"/channels/{channel_hash}/voice/join")["ok"]

    def leave_voice(self) -> bool:
        return self._post("/voice/leave")["ok"]

    def set_voice_muted(self, muted: bool) -> dict:
        return self._post("/voice/mute", {"muted": muted})

    def voice_roster(self, channel_hash: str) -> list[dict]:
        return self._get(f"/channels/{channel_hash}/voice/roster")

    def voice_status(self) -> dict:
        return self._get("/voice/status")

    def set_test_tone(self, enabled: bool) -> tuple[int, dict]:
        """Drive the headless TonePipeline. 409 when no pipeline is active."""
        return self.post_status("/voice/test_tone", {"enabled": enabled})

    def voice_link_states(self, channel_hash: str) -> dict[str, str]:
        """{identity_hash: link_state} for a channel's voice roster."""
        return {e["identity_hash"]: e["link_state"]
                for e in self.voice_roster(channel_hash)}

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

    def api_token(self) -> str:
        """The token every tester API requires, minted by the orchestrator."""
        return self.config().get("api_token", "")

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
        """Retune a tester's simulated link, raising if the shaper refused.

        An unknown profile name answers 400 and leaves the link unshaped. That
        used to pass silently, so scenarios named "flaky" and "serial9600" --
        neither of which exists -- ran on broadband while reporting that they
        had exercised a degraded link.
        """
        r = self._client.post(f"{self._base}/testers/{tag}/link_profile",
                              json={"profile": profile, **overrides})
        body = r.json() if r.content else {}
        if r.status_code != 200 or not body.get("ok"):
            raise RuntimeError(
                f"link profile {profile!r} rejected for {tag}: "
                f"HTTP {r.status_code} {body}"
            )
        return body

    def hub_kill(self) -> dict:
        return self._post("/hub/kill")

    def hub_start(self) -> dict:
        return self._post("/hub/start")

    def hub_restart(self) -> dict:
        return self._post("/hub/restart")
