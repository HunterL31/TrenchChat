"""
The nomad page-browsing surface of the HTTP/WS API.

Endpoints run against a real NodeBrowserManager (real Storage + Config in a
tmp dir, FakeNodeTransport) attached to an otherwise-MagicMock backend, so
what is under test is the endpoint contract, not stub echoes. WS events use
the same registered-callback pattern as test_api_naming.py.
"""

import base64
import sys
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from trenchchat.config import Config
from trenchchat.core.node_browser import NodeBrowserManager
from trenchchat.core.storage import Storage

from tests.fake_node import FakeNodeRegistry, FakeNodeTransport
from tests.helpers import wait_for

_TESTENV_DIR = Path(__file__).resolve().parents[1] / "devtools" / "testenv"
if str(_TESTENV_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTENV_DIR))

try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient

    from api import TOKEN_HEADER, create_app
    _HAVE_BACKEND_DEPS = True
except ImportError:  # pragma: no cover - depends on the local install
    _HAVE_BACKEND_DEPS = False
    TOKEN_HEADER = "x-tc-token"

needs_backend = pytest.mark.skipif(
    not _HAVE_BACKEND_DEPS,
    reason="install devtools/testenv/requirements.txt to exercise the API",
)

TOKEN = "test-token-not-a-real-one"
AUTH = {TOKEN_HEADER: TOKEN}
WS_HOST = {"Host": "127.0.0.1:8801"}

NODE = "cc" * 16


@pytest.fixture
def registry():
    return FakeNodeRegistry()


@pytest.fixture
def backend(tmp_path, registry):
    backend = MagicMock()
    backend.config.display_name = "Tester"
    backend.identity.hash_hex = "a" * 32
    backend.invite_mgr.list_pending_invites.return_value = []

    config = Config(data_dir=tmp_path)
    storage = Storage(db_path=tmp_path / "storage.db")
    transport = FakeNodeTransport("11" * 16, registry)
    backend.node_browser = NodeBrowserManager(
        SimpleNamespace(hash_hex="11" * 16), storage, config,
        transport=transport)
    yield backend
    transport.join_threads()
    storage.close()


@pytest.fixture
def client(backend):
    with TestClient(create_app(backend, token=TOKEN),
                    base_url="http://127.0.0.1:8801") as client:
        yield client


def _serve(registry, providers):
    host = FakeNodeTransport("99" * 16, registry, node_hex=NODE)
    host.start_hosting("Host", providers)
    return host


@needs_backend
class TestNodesAndBrowse:
    def test_nodes_lists_discovered(self, client, backend):
        backend.node_browser.record_node_announce(NODE, "A Node")
        res = client.get("/nomad/nodes", headers=AUTH)
        assert res.status_code == 200
        (node,) = res.json()
        assert node["node_hash"] == NODE
        assert node["display_name"] == "A Node"

    def test_browse_starts_fetch_and_caches(self, client, backend, registry):
        _serve(registry, {"/page/index.mu": lambda: b">Hello"})
        res = client.post("/nomad/browse", headers=AUTH,
                          json={"url": f"{NODE}:/page/index.mu"})
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        assert body["node_hash"] == NODE
        assert body["path"] == "/page/index.mu"
        assert body["kind"] == "page"
        assert wait_for(lambda: backend.node_browser.get_cached_page(
            NODE, "/page/index.mu") is not None)

        cached = client.get(f"/nomad/page/{NODE}", headers=AUTH,
                            params={"path": "/page/index.mu"})
        assert cached.status_code == 200
        assert base64.b64decode(cached.json()["content_b64"]) == b">Hello"

    def test_browse_relative_url_resolves_current_node(self, client, backend,
                                                       registry):
        _serve(registry, {"/page/about.mu": lambda: b"about"})
        res = client.post("/nomad/browse", headers=AUTH,
                          json={"url": ":/page/about.mu",
                                "current_node": NODE})
        assert res.status_code == 200
        assert res.json()["node_hash"] == NODE

    def test_browse_relative_url_without_current_node_is_400(self, client):
        res = client.post("/nomad/browse", headers=AUTH,
                          json={"url": ":/page/about.mu"})
        assert res.status_code == 400
        assert res.json()["ok"] is False

    def test_browse_malformed_url_is_400(self, client):
        res = client.post("/nomad/browse", headers=AUTH,
                          json={"url": "https://example.com/"})
        assert res.status_code == 400

    def test_fetch_status_answers_a_client_that_missed_the_event(
            self, client, backend, registry):
        """The event socket can be down for the whole fetch; the status
        endpoint is how a client finds out anyway."""
        _serve(registry, {"/page/index.mu": lambda: b">Hello"})
        fetch_id = client.post("/nomad/browse", headers=AUTH,
                               json={"url": f"{NODE}:/page/index.mu"}
                               ).json()["fetch_id"]
        assert wait_for(
            lambda: client.get(f"/nomad/fetch/{fetch_id}", headers=AUTH)
            .json().get("status") == "done")
        body = client.get(f"/nomad/fetch/{fetch_id}", headers=AUTH).json()
        assert body["node_hash"] == NODE
        assert body["path"] == "/page/index.mu"

    def test_unknown_fetch_is_404_with_reason(self, client):
        res = client.get(f"/nomad/fetch/{'00' * 8}", headers=AUTH)
        assert res.status_code == 404
        assert res.json()["reason"] == "unknown"

    def test_uncached_page_is_404_with_reason(self, client):
        res = client.get(f"/nomad/page/{NODE}", headers=AUTH,
                         params={"path": "/page/none.mu"})
        assert res.status_code == 404
        assert res.json()["reason"] == "not_cached"


@needs_backend
class TestFileEndpoint:
    def test_cached_file_served_as_attachment(self, client, backend, registry):
        _serve(registry, {"/file/data.bin": lambda: b"\x00\x01"})
        client.post("/nomad/fetch", headers=AUTH,
                    json={"node_hash": NODE, "path": "/file/data.bin"})
        assert wait_for(lambda: backend.node_browser.get_cached_file(
            NODE, "/file/data.bin") is not None)
        res = client.get(f"/nomad/file/{NODE}", headers=AUTH,
                         params={"path": "/file/data.bin"})
        assert res.status_code == 200
        assert res.content == b"\x00\x01"
        assert res.headers["x-content-type-options"] == "nosniff"
        assert res.headers["content-type"].startswith(
            "application/octet-stream")
        assert 'filename="data.bin"' in res.headers["content-disposition"]
        assert "attachment" in res.headers["content-disposition"]

    def test_download_uses_the_name_the_node_gave_the_file(
            self, client, backend, registry, tmp_path):
        """A node serves /file/<path> under whatever name it chooses; the
        download must carry that name, not the path's basename."""
        blob = tmp_path / "Quarterly Report.pdf"
        blob.write_bytes(b"%PDF")
        _serve(registry, {"/file/dl": lambda: blob})
        client.post("/nomad/fetch", headers=AUTH,
                    json={"node_hash": NODE, "path": "/file/dl"})
        assert wait_for(lambda: backend.node_browser.get_cached_file(
            NODE, "/file/dl") is not None)

        res = client.get(f"/nomad/file/{NODE}", headers=AUTH,
                         params={"path": "/file/dl"})

        assert 'filename="Quarterly Report.pdf"' in \
            res.headers["content-disposition"]

    def test_uncached_file_is_404(self, client):
        res = client.get(f"/nomad/file/{NODE}", headers=AUTH,
                         params={"path": "/file/none"})
        assert res.status_code == 404


@needs_backend
class TestBookmarks:
    def test_bookmark_roundtrip(self, client):
        res = client.post("/nomad/bookmarks", headers=AUTH,
                          json={"node_hash": NODE, "path": "/page/index.mu",
                                "label": "Home"})
        assert res.json() == {"ok": True}
        listed = client.get("/nomad/bookmarks", headers=AUTH).json()
        assert [(b["node_hash"], b["path"], b["label"]) for b in listed] == [
            (NODE, "/page/index.mu", "Home")]
        deleted = client.post("/nomad/bookmarks/delete", headers=AUTH,
                              json={"node_hash": NODE,
                                    "path": "/page/index.mu"})
        assert deleted.json() == {"ok": True}
        assert client.get("/nomad/bookmarks", headers=AUTH).json() == []

    def test_bad_bookmark_is_400(self, client):
        res = client.post("/nomad/bookmarks", headers=AUTH,
                          json={"node_hash": "nothex", "path": "/page/x.mu"})
        assert res.status_code == 400


@needs_backend
class TestHosting:
    def test_hosting_enable_disable(self, client, backend):
        res = client.post("/nomad/hosting", headers=AUTH,
                          json={"enabled": True, "node_name": "My Node"})
        body = res.json()
        assert body["ok"] is True
        assert body["enabled"] is True
        assert body["node_name"] == "My Node"
        assert {p["path"] for p in body["pages"]} == {"/page/index.mu"}

        status = client.get("/nomad/hosting", headers=AUTH).json()
        assert status["enabled"] is True

        res = client.post("/nomad/hosting", headers=AUTH,
                          json={"enabled": False})
        assert res.json()["enabled"] is False

    def test_hosting_refresh_reports_new_pages(self, client, backend):
        client.post("/nomad/hosting", headers=AUTH,
                    json={"enabled": True, "node_name": "n"})
        pages_dir = backend.node_browser.pages_root / "pages"
        (pages_dir / "extra.mu").write_text(">Extra")
        res = client.post("/nomad/hosting/refresh", headers=AUTH)
        assert {p["path"] for p in res.json()["pages"]} == \
            {"/page/index.mu", "/page/extra.mu"}


@needs_backend
class TestNomadEvents:
    def test_node_announce_publishes_event(self, client, backend):
        with client.websocket_connect(f"/ws?token={TOKEN}",
                                      headers=WS_HOST) as ws:
            backend.node_browser.record_node_announce(NODE, "A Node")
            event = ws.receive_json()
        assert event == {
            "type": "nomad_node",
            "node_hash": NODE,
            "display_name": "A Node",
        }

    def test_fetch_lifecycle_publishes_events(self, client, backend, registry):
        _serve(registry, {"/page/index.mu": lambda: b"x"})
        with client.websocket_connect(f"/ws?token={TOKEN}",
                                      headers=WS_HOST) as ws:
            res = client.post("/nomad/fetch", headers=AUTH,
                              json={"node_hash": NODE})
            fetch_id = res.json()["fetch_id"]
            statuses = []
            while not statuses or statuses[-1] not in ("done", "failed"):
                event = ws.receive_json()
                if event["type"] == "nomad_fetch" \
                        and event["fetch_id"] == fetch_id:
                    statuses.append(event["status"])
        assert statuses[0] == "queued"
        assert statuses[-1] == "done"


@needs_backend
class TestFriendsPageDecoration:
    def test_friends_carry_node_hash_once_heard(self, client, backend):
        from trenchchat.core.node_browser import nomad_node_hash_for_identity

        friend_hex = "ee" * 16
        backend.friends_mgr.get_friends.return_value = [{
            "identity_hash": friend_hex, "nickname": "", "note": "",
            "display_name": "Pal", "added_at": 1.0, "last_seen_at": 2.0,
            "is_online": True,
        }]

        before = client.get("/friends", headers=AUTH).json()
        assert before[0]["nomad_node_hash"] is None

        node_hex = nomad_node_hash_for_identity(friend_hex)
        backend.node_browser.record_node_announce(node_hex, "Pal's node")
        after = client.get("/friends", headers=AUTH).json()
        assert after[0]["nomad_node_hash"] == node_hex
