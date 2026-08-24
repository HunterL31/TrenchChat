"""
The interface-discovery endpoints of the HTTP/WS API.

Like test_api_theme.py these need no peer: the backend is stubbed down to the
config path the endpoints read and write, and the RNS discovery store is
replaced with canned entries.
"""

import sys
import warnings
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from trenchchat.core.interfaces_config import (
    SUGGESTED_DEFAULTS, load_discovery_settings, load_interfaces_config,
)

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

DISCOVERED = [{
    "name": "Test Hub",
    "type": "BackboneInterface",
    "status": "available",
    "hops": 2,
    "value": 20,
    "last_heard": 1000.0,
    "reachable_on": "hub.example.org",
    "port": 4242,
    "transport_id": "ab" * 16,
    "discovery_hash": "ef" * 16,
    "pinnable": True,
}]


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
class TestDiscoveryEndpoints:
    def test_get_returns_settings_and_interfaces(self, client):
        with patch("api.list_discovered_interfaces", return_value=DISCOVERED):
            res = client.get("/reticulum/discovery", headers=AUTH)
        assert res.status_code == 200
        body = res.json()
        assert body["settings"]["discover_interfaces"] is False
        assert body["interfaces"][0]["name"] == "Test Hub"

    def test_put_writes_the_settings(self, client, config_path):
        res = client.put("/reticulum/discovery", headers=AUTH, json={
            "discover_interfaces": True,
            "autoconnect_discovered_interfaces": 3,
        })
        assert res.status_code == 200
        assert res.json()["restart_required"] is True
        settings = load_discovery_settings(config_path)
        assert settings["discover_interfaces"] is True
        assert settings["autoconnect_discovered_interfaces"] == 3

    def test_pin_writes_the_discovered_entry(self, client, config_path):
        with patch("trenchchat.core.discovery.list_discovered_interfaces",
                   return_value=DISCOVERED):
            res = client.post("/reticulum/discovery/pin", headers=AUTH,
                              json={"discovery_hash": "ef" * 16})
        assert res.status_code == 200
        assert res.json()["name"] == "Test Hub"
        written = load_interfaces_config(config_path)["Test Hub"]
        assert written["type"] == "TCPClientInterface"
        assert written["target_host"] == "hub.example.org"

    def test_pin_unknown_hash_is_400(self, client):
        with patch("trenchchat.core.discovery.list_discovered_interfaces",
                   return_value=[]):
            res = client.post("/reticulum/discovery/pin", headers=AUTH,
                              json={"discovery_hash": "00" * 16})
        assert res.status_code == 400


@needs_backend
class TestSuggestedDefaultsEndpoints:
    def test_get_lists_missing_seeds(self, client):
        res = client.get("/reticulum/interfaces_suggested", headers=AUTH)
        assert res.status_code == 200
        assert set(res.json()["missing"]) == set(SUGGESTED_DEFAULTS)

    def test_post_applies_seeds_and_enables_discovery(self, client, config_path):
        res = client.post("/reticulum/interfaces_suggested", headers=AUTH)
        assert res.status_code == 200
        assert set(res.json()["added"]) == set(SUGGESTED_DEFAULTS)
        interfaces = load_interfaces_config(config_path)
        for name, cfg in SUGGESTED_DEFAULTS.items():
            assert interfaces[name]["bootstrap_only"] == "Yes"
            assert interfaces[name]["target_host"] == cfg["target_host"]
        assert load_discovery_settings(config_path)["discover_interfaces"] is True

        res = client.get("/reticulum/interfaces_suggested", headers=AUTH)
        assert res.json()["missing"] == {}
