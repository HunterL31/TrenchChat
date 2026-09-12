"""
What the client can see about this node's direct sessions.

Path state is local knowledge: this node's own sessions and nothing more. It
reaches the client three ways, all additive: a path on every member row, a
path_changed event when a session comes up or goes away, and a diagnostics
listing of the sessions themselves.

Like test_api_channels.py these need no peer: the backend is a MagicMock
stubbed down to what the endpoints touch.
"""

import sys
import warnings
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock

import pytest

from trenchchat.network.base import PATH_DIRECT, PATH_OFFLINE, PATH_RETICULUM

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
# websocket_connect builds the Host header from nothing, not from base_url.
WS_HOST = {"host": "127.0.0.1:8801"}

CH = "cc" * 16
ME = "a" * 32
DIRECT_PEER = "1" * 32
MESH_PEER = "2" * 32
GONE_PEER = "3" * 32
LISTEN_PORT = 42420


def _member(identity_hash: str, role: str = "member") -> dict:
    return {"identity_hash": identity_hash, "display_name": "", "role": role,
            "added_at": 1.0}


@pytest.fixture
def backend():
    backend = MagicMock()
    backend.config.display_name = "Tester"
    backend.identity.hash_hex = ME
    backend.invite_mgr.list_pending_invites.return_value = []
    backend.storage.get_display_name_for_identity.return_value = ""
    backend.storage.get_members.return_value = [
        _member(ME, "owner"), _member(DIRECT_PEER), _member(MESH_PEER),
        _member(GONE_PEER),
    ]
    backend.router.path_for.side_effect = lambda peer: (
        PATH_DIRECT if peer == DIRECT_PEER else PATH_RETICULUM)
    backend.presence_mgr.is_online.side_effect = lambda peer: peer != GONE_PEER
    backend.router.direct_transport = None
    backend.upgrade_mgr.failures.return_value = {}
    backend.upgrade_mgr.offer.return_value = None
    return backend


@pytest.fixture
def with_direct(backend):
    """The same backend, holding a direct path with sessions on it."""
    backend.router.direct_transport = MagicMock()
    backend.router.direct_transport.sessions.return_value = []
    backend.router.direct_transport.listen_port = LISTEN_PORT
    return backend.router.direct_transport


@pytest.fixture
def client(backend):
    with TestClient(create_app(backend, token=TOKEN),
                    base_url="http://127.0.0.1:8801") as client:
        yield client


@needs_backend
class TestMemberPath:
    def test_every_member_row_carries_the_path_this_node_reaches_it_on(
            self, client):
        rows = client.get(f"/channels/{CH}/members", headers=AUTH).json()
        by_hash = {row["identity_hash"]: row["path"] for row in rows}
        assert by_hash[DIRECT_PEER] == PATH_DIRECT
        assert by_hash[MESH_PEER] == PATH_RETICULUM
        assert by_hash[GONE_PEER] == PATH_OFFLINE

    def test_this_node_is_not_a_peer_of_itself(self, client):
        rows = client.get(f"/channels/{CH}/members", headers=AUTH).json()
        mine = next(row for row in rows if row["identity_hash"] == ME)
        assert mine["path"] == PATH_RETICULUM

    def test_the_rest_of_the_row_is_unchanged(self, client):
        rows = client.get(f"/channels/{CH}/members", headers=AUTH).json()
        assert rows[0]["identity_hash"] == ME
        assert rows[0]["role"] == "owner"


@needs_backend
class TestUpgradeSessions:
    def test_a_node_with_no_direct_path_lists_nothing_and_is_not_listening(
            self, client):
        body = client.get("/upgrade/sessions", headers=AUTH).json()
        assert body == {"sessions": [], "last_failure": {},
                        "listening": False, "listen_port": 0}

    def test_a_session_is_listed_with_what_this_node_knows_about_it(
            self, client, with_direct):
        with_direct.sessions.return_value = [{
            "peer": DIRECT_PEER, "since": 1700.0, "round_trip_secs": 0.012,
            "bytes_in": 4096, "bytes_out": 2048, "pending_acks": 0,
        }]
        body = client.get("/upgrade/sessions", headers=AUTH).json()
        assert body["last_failure"] == {}
        assert len(body["sessions"]) == 1
        session = body["sessions"][0]
        assert session["peer"] == DIRECT_PEER
        assert session["since"] == 1700.0
        assert session["round_trip_secs"] == 0.012
        assert session["bytes_in"] == 4096
        assert session["bytes_out"] == 2048
        assert session["display_name"]

    def test_it_says_where_this_node_listens(self, client, with_direct):
        body = client.get("/upgrade/sessions", headers=AUTH).json()
        assert body["listening"] is True
        assert body["listen_port"] == LISTEN_PORT

    def test_a_port_that_could_not_be_bound_reads_as_not_listening(
            self, client, with_direct):
        # The transport can still dial with no listener, so the sessions it
        # holds are listed either way; what a user needs told is that nothing
        # can arrive, which is a firewall rather than a NAT that will not punch.
        with_direct.listen_port = 0

        body = client.get("/upgrade/sessions", headers=AUTH).json()

        assert body["listening"] is False
        assert body["listen_port"] == 0

    def test_it_needs_the_token_like_every_other_endpoint(self, client):
        assert client.get("/upgrade/sessions").status_code == 401


@needs_backend
class TestUpgradeFailures:
    """Why a pair has no session, which is the panel's other half."""

    def test_a_peers_last_failure_reaches_the_client(self, client, backend):
        backend.upgrade_mgr.failures.return_value = {
            MESH_PEER: {"reason": "punch_failed", "at": 1700.0,
                        "next_attempt": 1760.0},
        }
        body = client.get("/upgrade/sessions", headers=AUTH).json()
        assert body["last_failure"][MESH_PEER]["reason"] == "punch_failed"
        assert body["last_failure"][MESH_PEER]["next_attempt"] == 1760.0


@needs_backend
class TestTryNow:
    """The diagnostics panel's per-peer button, gate and all."""

    def test_it_asks_the_manager_for_a_session_with_that_peer(self, client,
                                                              backend):
        backend.storage.member_scopes.return_value = {"scope"}
        backend.storage.get_server.return_value = {"hash": "scope"}
        body = client.post(f"/upgrade/try/{MESH_PEER}", headers=AUTH).json()
        assert body == {"ok": True, "reason": None}
        backend.upgrade_mgr.offer.assert_called_once_with(MESH_PEER,
                                                          ignore_backoff=True)

    def test_an_ineligible_peer_is_refused_before_the_manager_is_asked(
            self, client, backend):
        backend.storage.member_scopes.return_value = set()
        body = client.post(f"/upgrade/try/{MESH_PEER}", headers=AUTH).json()
        assert body == {"ok": False, "reason": "ineligible"}
        backend.upgrade_mgr.offer.assert_not_called()

    def test_it_needs_the_token(self, client):
        assert client.post(f"/upgrade/try/{MESH_PEER}").status_code == 401


@needs_backend
class TestCloseSession:
    """The harness hook the scenarios drop a session with."""

    def test_it_closes_the_session_with_one_peer(self, client, with_direct):
        with_direct.close_session.return_value = True
        body = client.post(f"/upgrade/close/{DIRECT_PEER}", headers=AUTH).json()
        assert body == {"ok": True}
        assert with_direct.close_session.call_args.args[0] == DIRECT_PEER

    def test_a_node_with_no_direct_path_closes_nothing(self, client):
        body = client.post(f"/upgrade/close/{DIRECT_PEER}", headers=AUTH).json()
        assert body == {"ok": False}

    def test_it_needs_the_token(self, client):
        assert client.post(f"/upgrade/close/{DIRECT_PEER}").status_code == 401


@needs_backend
class TestPathChangedEvent:
    def test_a_session_coming_up_reaches_the_client(self, client, backend,
                                                    with_direct):
        with_direct.session_for.return_value = MagicMock(opened_at=1700.0)
        fired = [call.args for call in
                 backend.router.add_path_changed_callback.call_args_list]
        assert fired, "no path_changed callback was registered"
        callback = fired[0][0]

        with client.websocket_connect(f"/ws?token={TOKEN}",
                                      headers=WS_HOST) as socket:
            callback(DIRECT_PEER, PATH_DIRECT)
            event = socket.receive_json()

        assert event["type"] == "path_changed"
        assert event["peer"] == DIRECT_PEER
        assert event["path"] == PATH_DIRECT
        assert event["since"] == 1700.0

    def test_a_session_going_away_reaches_the_client(self, client, backend,
                                                     with_direct):
        with_direct.session_for.return_value = None
        callback = backend.router.add_path_changed_callback.call_args_list[0].args[0]

        with client.websocket_connect(f"/ws?token={TOKEN}",
                                      headers=WS_HOST) as socket:
            callback(MESH_PEER, PATH_RETICULUM)
            event = socket.receive_json()

        assert event["type"] == "path_changed"
        assert event["path"] == PATH_RETICULUM
        assert event["since"] > 0


@needs_backend
class TestTheDirectConnectionsSwitch:
    """The client gate, which is a user saying nobody may hold their address."""

    def test_turning_it_off_reaches_the_manager(self, client, backend):
        backend.upgrade_mgr.set_enabled.return_value = False

        res = client.post("/upgrade/enabled", headers=AUTH,
                          json={"enabled": False})

        assert res.status_code == 200
        assert res.json() == {"ok": True, "enabled": False}
        backend.upgrade_mgr.set_enabled.assert_called_once_with(False)

    def test_the_switch_and_the_port_can_be_read_back(self, client, backend):
        backend.config.upgrade_enabled = True
        backend.config.upgrade_listen_port = 42424

        body = client.get("/upgrade/enabled", headers=AUTH).json()

        assert body == {"enabled": True, "listen_port": 42424}

    def test_it_needs_the_token_like_everything_else(self, client):
        assert client.post("/upgrade/enabled",
                           json={"enabled": False}).status_code == 401


@needs_backend
class TestTheListenPort:
    """The port a session arrives on, which a user edits with the settings."""

    @pytest.fixture
    def settings_backend(self, backend):
        backend.config.propagation_enabled = False
        backend.config.propagation_node_name = ""
        backend.config.propagation_storage_limit_mb = 500
        backend.config.outbound_propagation_node = ""
        backend.config.upgrade_listen_port = LISTEN_PORT
        return backend

    def test_it_is_read_back_with_the_rest_of_the_settings(self, client,
                                                           settings_backend):
        body = client.get("/settings", headers=AUTH).json()

        assert body["upgrade_listen_port"] == LISTEN_PORT
        assert body["propagation_storage_limit_mb"] == 500

    def test_saving_one_writes_it_and_leaves_the_rest_alone(self, client,
                                                            settings_backend):
        res = client.post("/settings", headers=AUTH,
                          json={"upgrade_listen_port": 42999})

        assert res.status_code == 200
        assert settings_backend.config.upgrade_listen_port == 42999
        # Nothing live is touched: the port is bound on the next launch.
        settings_backend.router.enable_propagation.assert_not_called()

    def test_a_port_the_config_refuses_is_an_error_not_a_silent_drop(
            self, client, settings_backend):
        type(settings_backend.config).upgrade_listen_port = PropertyMock(
            side_effect=ValueError("upgrade_listen_port must be between 0 "
                                   "and 65535, got 99999"))

        res = client.post("/settings", headers=AUTH,
                          json={"upgrade_listen_port": 99999})

        assert res.status_code == 400
        assert "65535" in res.json()["error"]

    def test_a_save_that_does_not_mention_it_leaves_it_where_it_was(
            self, client, settings_backend):
        res = client.post("/settings", headers=AUTH,
                          json={"propagation_node_name": "ridge-relay"})

        assert res.status_code == 200
        assert settings_backend.config.upgrade_listen_port == LISTEN_PORT
