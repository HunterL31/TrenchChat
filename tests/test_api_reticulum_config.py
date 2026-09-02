"""
The node-wide Reticulum config endpoints of the HTTP/WS API.

Like test_api_discovery.py these need no peer: the backend is stubbed down to
the config path the endpoints read and write.
"""

import sys
import warnings
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from trenchchat.core.reticulum_config import RETICULUM_OPTIONS, load_reticulum_config

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


@pytest.fixture
def config_path(tmp_path):
    return str(tmp_path / "config")


@pytest.fixture
def client(config_path):
    backend = MagicMock()
    backend.identity.hash_hex = "a" * 32
    backend.rns_config_path = config_path
    with TestClient(create_app(backend, token=TOKEN),
                    base_url="http://127.0.0.1:8801") as client:
        yield client


@needs_backend
class TestReticulumConfigEndpoints:
    def test_get_returns_every_option(self, client):
        res = client.get("/reticulum/config", headers=AUTH)
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        keys = {opt["key"] for opt in body["options"]}
        assert keys == {opt["key"] for opt in RETICULUM_OPTIONS}
        first = body["options"][0]
        assert first["description"]
        assert first["value"] == ""

    def test_get_reflects_written_values(self, client, config_path):
        client.put("/reticulum/config", headers=AUTH,
                   json={"values": {"loglevel": "6"}})
        options = {opt["key"]: opt for opt in
                   client.get("/reticulum/config", headers=AUTH).json()["options"]}
        assert options["loglevel"]["value"] == "6"

    def test_put_writes_the_values(self, client, config_path):
        res = client.put("/reticulum/config", headers=AUTH, json={"values": {
            "enable_transport": "yes",
            "shared_instance_port": "37500",
        }})
        assert res.status_code == 200
        assert res.json()["restart_required"] is True
        values = {opt["key"]: opt["value"] for opt in load_reticulum_config(config_path)}
        assert values["enable_transport"] == "Yes"
        assert values["shared_instance_port"] == "37500"

    def test_put_empty_value_clears_the_key(self, client, config_path):
        client.put("/reticulum/config", headers=AUTH,
                   json={"values": {"enable_transport": "Yes"}})
        res = client.put("/reticulum/config", headers=AUTH,
                         json={"values": {"enable_transport": ""}})
        assert res.status_code == 200
        values = {opt["key"]: opt["value"] for opt in load_reticulum_config(config_path)}
        assert values["enable_transport"] == ""

    def test_put_rejects_a_bad_value(self, client):
        res = client.put("/reticulum/config", headers=AUTH,
                         json={"values": {"loglevel": "99"}})
        assert res.status_code == 400
        assert "loglevel" in res.json()["error"]

    def test_put_rejects_an_unknown_key(self, client):
        res = client.put("/reticulum/config", headers=AUTH,
                         json={"values": {"rm_rf": "yes"}})
        assert res.status_code == 400
        assert "rm_rf" in res.json()["error"]


@needs_backend
class TestAuth:
    def test_get_requires_the_token(self, client):
        assert client.get("/reticulum/config").status_code == 401

    def test_put_requires_the_token(self, client, config_path):
        res = client.put("/reticulum/config", json={"values": {"loglevel": "6"}})
        assert res.status_code == 401
        values = {opt["key"]: opt["value"] for opt in load_reticulum_config(config_path)}
        assert values["loglevel"] == ""
