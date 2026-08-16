"""Single-origin front for remote testing.

Serves the Flutter web build and reverse-proxies every other path -- including
the /ws event socket -- to a testenv tester API, so the client's page-origin
base-URL fallback works from any host that can reach this port (e.g. over a
Tailscale tailnet). Configure with TESTER_API and REMOTE_PROXY_PORT.
"""
import asyncio
import mimetypes
import os
import pathlib

import httpx
import uvicorn
import websockets
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
WEB_ROOT = REPO_ROOT / "flutter_ui" / "build" / "web"
BACKEND = os.environ.get("TESTER_API", "http://127.0.0.1:8801")
BACKEND_WS = BACKEND.replace("http", "ws", 1) + "/ws"
PORT = int(os.environ.get("REMOTE_PROXY_PORT", "8899"))

client = httpx.AsyncClient(base_url=BACKEND, timeout=30.0)


async def serve(request: Request) -> Response:
    """Serve a static file from the web build, or forward to the tester API."""
    path = request.path_params.get("path", "")
    candidate = (WEB_ROOT / path).resolve() if path else WEB_ROOT / "index.html"
    if request.method == "GET" and str(candidate).startswith(str(WEB_ROOT)) \
            and candidate.is_file():
        ctype = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        return Response(candidate.read_bytes(), media_type=ctype)

    upstream = await client.request(
        request.method,
        f"/{path}",
        params=request.query_params,
        content=await request.body(),
        headers={"content-type": request.headers.get("content-type", "")},
    )
    return Response(
        upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
    )


async def ws_bridge(ws: WebSocket) -> None:
    """Pump frames both ways between the browser and the tester's /ws."""
    await ws.accept()
    try:
        async with websockets.connect(BACKEND_WS) as backend:
            async def pump_in() -> None:
                async for msg in backend:
                    await ws.send_text(msg if isinstance(msg, str) else msg.decode())

            async def pump_out() -> None:
                while True:
                    await backend.send(await ws.receive_text())

            _, pending = await asyncio.wait(
                [asyncio.create_task(pump_in()), asyncio.create_task(pump_out())],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
    except Exception:
        pass
    finally:
        try:
            await ws.close()
        except Exception:
            pass


app = Starlette(routes=[
    WebSocketRoute("/ws", ws_bridge),
    Route("/", serve, methods=["GET"]),
    Route("/{path:path}", serve, methods=["GET", "POST", "PUT", "DELETE", "PATCH"]),
])

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
