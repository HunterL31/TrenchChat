"""
The RRC surface of the HTTP/WS API.

Endpoints run against a real RRCManager (real Config in a tmp dir,
FakeRRCTransport, FakeHub) attached to an otherwise-MagicMock backend, so
what is under test is the endpoint contract rather than a stub echo. WS
events use the same registered-callback pattern as test_api_nomad.py.
"""

import sys
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from trenchchat.config import Config
from trenchchat.core.rrc import ROOM_JOINED, RRCManager

from tests.fake_rrc import FakeHub, FakeHubRegistry, FakeRRCTransport
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

SELF = "11" * 16
HUB = "cc" * 16


@pytest.fixture
def registry():
    reg = FakeHubRegistry()
    reg.add(FakeHub(HUB, name="coast hub"))
    return reg


@pytest.fixture
def backend(tmp_path, registry):
    backend = MagicMock()
    backend.config.display_name = "Tester"
    backend.identity.hash_hex = SELF
    backend.invite_mgr.list_pending_invites.return_value = []

    config = Config(data_dir=tmp_path)
    identity = SimpleNamespace(hash=bytes.fromhex(SELF), hash_hex=SELF,
                               display_name="Tester")
    transport = FakeRRCTransport(SELF, registry)
    backend.rrc = RRCManager(identity, config, transport=transport)
    yield backend
    transport.join_threads()


@pytest.fixture
def client(backend):
    with TestClient(create_app(backend, token=TOKEN),
                    base_url="http://127.0.0.1:8801") as client:
        yield client


def _connect_and_join(client, backend, room="general"):
    assert client.post("/rrc/connect", headers=AUTH,
                       json={"hub_hash": HUB}).status_code == 200
    assert wait_for(backend.rrc.is_active)
    res = client.post("/rrc/rooms", headers=AUTH, json={"room": room})
    assert res.status_code == 200
    assert wait_for(lambda: backend.rrc.rooms().get("#" + room) == ROOM_JOINED)


@needs_backend
class TestAuth:
    @pytest.mark.parametrize("method,path", [
        ("get", "/rrc"), ("get", "/rrc/hubs"), ("get", "/rrc/rooms/x/messages"),
        ("post", "/rrc/connect"), ("post", "/rrc/disconnect"),
        ("post", "/rrc/rooms"), ("post", "/rrc/rooms/part"),
        ("post", "/rrc/rooms/x/messages"), ("post", "/rrc/nickname"),
        ("post", "/rrc/bookmarks"),
    ])
    def test_every_rrc_endpoint_needs_the_token(self, client, method, path):
        res = client.post(path, json={}) if method == "post" else client.get(path)
        assert res.status_code == 401, f"{method} {path} was not gated"


@needs_backend
class TestHubsAndSession:
    def test_hubs_lists_what_was_heard(self, client, backend):
        backend.rrc.note_hub(HUB, "coast hub")
        (hub,) = client.get("/rrc/hubs", headers=AUTH).json()
        assert hub["hash"] == HUB
        assert hub["name"] == "coast hub"

    def test_connect_reports_the_session(self, client, backend):
        res = client.post("/rrc/connect", headers=AUTH, json={"hub_hash": HUB})
        assert res.status_code == 200
        assert res.json()["session"]["hub"] == HUB
        assert wait_for(backend.rrc.is_active)
        assert client.get("/rrc", headers=AUTH).json()["session"]["name"] \
            == "coast hub"

    def test_connecting_to_rubbish_is_a_bad_request(self, client):
        res = client.post("/rrc/connect", headers=AUTH,
                          json={"hub_hash": "not-a-hash"})
        assert res.status_code == 400
        assert res.json()["ok"] is False

    def test_disconnect_clears_the_session(self, client, backend):
        _connect_and_join(client, backend)
        res = client.post("/rrc/disconnect", headers=AUTH)
        assert res.status_code == 200
        assert res.json()["session"]["hub"] is None

    def test_state_is_one_read_of_everything(self, client, backend):
        _connect_and_join(client, backend)
        body = client.get("/rrc", headers=AUTH).json()
        assert body["session"]["rooms"] == {"#general": ROOM_JOINED}
        assert body["nickname"] == "Tester"
        assert body["rosters"]["#general"] == [SELF]
        assert body["bookmarks"] == []


@needs_backend
class TestRooms:
    def test_join_and_part(self, client, backend):
        _connect_and_join(client, backend)
        res = client.post("/rrc/rooms/part", headers=AUTH,
                          json={"room": "general"})
        assert res.status_code == 200
        assert wait_for(lambda: "#general" not in backend.rrc.rooms())

    def test_joining_without_a_session_is_refused(self, client):
        res = client.post("/rrc/rooms", headers=AUTH, json={"room": "general"})
        assert res.status_code == 400
        assert "not connected" in res.json()["error"]

    def test_parting_a_room_never_joined_is_refused(self, client, backend):
        _connect_and_join(client, backend)
        res = client.post("/rrc/rooms/part", headers=AUTH,
                          json={"room": "elsewhere"})
        assert res.status_code == 400

    def test_a_room_is_named_without_its_hash_in_the_url(self, client, backend):
        """'#' cannot travel in a URL path, so the endpoints take the bare
        name and put the hash back."""
        _connect_and_join(client, backend)
        assert client.get("/rrc/rooms/general/roster",
                          headers=AUTH).json() == [SELF]


@needs_backend
class TestMessages:
    def test_sending_returns_the_line_it_recorded(self, client, backend):
        _connect_and_join(client, backend)
        res = client.post("/rrc/rooms/general/messages", headers=AUTH,
                          json={"text": "hello room"})
        assert res.status_code == 200
        (line,) = res.json()["lines"]
        assert line["text"] == "hello room"
        assert line["own"] is True

    def test_the_transcript_reads_back(self, client, backend):
        _connect_and_join(client, backend)
        client.post("/rrc/rooms/general/messages", headers=AUTH,
                    json={"text": "one"})
        client.post("/rrc/rooms/general/messages", headers=AUTH,
                    json={"text": "two"})
        lines = client.get("/rrc/rooms/general/messages", headers=AUTH).json()
        assert [line["text"] for line in lines] == ["one", "two"]

    def test_sending_to_a_room_not_joined_is_refused(self, client, backend):
        _connect_and_join(client, backend)
        res = client.post("/rrc/rooms/elsewhere/messages", headers=AUTH,
                          json={"text": "hi"})
        assert res.status_code == 400

    def test_a_notice_is_accepted(self, client, backend):
        _connect_and_join(client, backend)
        res = client.post("/rrc/rooms/general/messages", headers=AUTH,
                          json={"text": "heads up", "notice": True})
        assert res.status_code == 200


@needs_backend
class TestNicknameAndBookmarks:
    def test_setting_a_nickname(self, client, backend):
        res = client.post("/rrc/nickname", headers=AUTH,
                          json={"nickname": "tester"})
        assert res.json()["nickname"] == "tester"
        assert backend.rrc.nickname() == "tester"

    def test_a_nickname_with_control_characters_is_refused(self, client):
        res = client.post("/rrc/nickname", headers=AUTH,
                          json={"nickname": "bad\x07nick"})
        assert res.status_code == 400

    def test_bookmarking_a_hub(self, client, backend):
        res = client.post("/rrc/bookmarks", headers=AUTH,
                          json={"hub_hash": HUB, "bookmarked": True})
        assert res.json()["bookmarks"] == [HUB]
        res = client.post("/rrc/bookmarks", headers=AUTH,
                          json={"hub_hash": HUB, "bookmarked": False})
        assert res.json()["bookmarks"] == []

    def test_bookmarking_rubbish_is_refused(self, client):
        res = client.post("/rrc/bookmarks", headers=AUTH,
                          json={"hub_hash": "nope", "bookmarked": True})
        assert res.status_code == 400
