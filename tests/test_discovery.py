"""
Discovered-interface pinning and the discovery config settings.

Pure config/data logic: the RNS discovery store itself is exercised by the
scenario suite; here list_discovered_interfaces is stubbed where needed.
"""

from unittest.mock import patch

import pytest

from trenchchat.core.discovery import (
    _to_jsonable, build_pinned_config, pin_discovered_interface,
)
from trenchchat.core.interfaces_config import (
    SUGGESTED_DEFAULTS,
    InterfaceConfigError,
    apply_suggested_defaults,
    default_rns_config_path,
    get_missing_suggested_defaults,
    load_discovery_settings,
    load_interfaces_config,
    seed_initial_config,
    write_discovery_settings,
)


def _discovered_entry(**overrides) -> dict:
    entry = {
        "name": "Test Hub",
        "type": "BackboneInterface",
        "status": "available",
        "hops": 2,
        "value": 20,
        "last_heard": 1000.0,
        "reachable_on": "hub.example.org",
        "port": 4242,
        "transport_id": "ab" * 16,
        "network_id": "cd" * 16,
        "discovery_hash": "ef" * 16,
    }
    entry.update(overrides)
    return entry


# ---------------------------------------------------------------------------
# build_pinned_config
# ---------------------------------------------------------------------------

def test_pinned_config_is_tcp_client():
    name, cfg = build_pinned_config(_discovered_entry())
    assert name == "Test Hub"
    assert cfg["type"] == "TCPClientInterface"
    assert cfg["enabled"] == "Yes"
    assert cfg["target_host"] == "hub.example.org"
    assert cfg["target_port"] == "4242"
    assert cfg["transport_identity"] == "ab" * 16


def test_pinned_config_without_transport_id_omits_it():
    _, cfg = build_pinned_config(_discovered_entry(transport_id=None))
    assert "transport_identity" not in cfg


def test_pinned_config_rejects_radio_types():
    with pytest.raises(InterfaceConfigError):
        build_pinned_config(_discovered_entry(type="RNodeInterface"))


def test_pinned_config_rejects_missing_address():
    with pytest.raises(InterfaceConfigError):
        build_pinned_config(_discovered_entry(reachable_on=""))


# ---------------------------------------------------------------------------
# pin_discovered_interface
# ---------------------------------------------------------------------------

def test_pin_writes_the_section(tmp_path):
    cfg_path = str(tmp_path / "config")
    entry = _discovered_entry()
    with patch("trenchchat.core.discovery.list_discovered_interfaces",
               return_value=[entry]):
        name = pin_discovered_interface(cfg_path, "ef" * 16)
    assert name == "Test Hub"
    written = load_interfaces_config(cfg_path)["Test Hub"]
    assert written["target_host"] == "hub.example.org"
    assert written["transport_identity"] == "ab" * 16


def test_pin_unknown_hash_raises(tmp_path):
    with patch("trenchchat.core.discovery.list_discovered_interfaces",
               return_value=[]):
        with pytest.raises(InterfaceConfigError):
            pin_discovered_interface(str(tmp_path / "config"), "00" * 16)


def test_pin_twice_overwrites_not_duplicates(tmp_path):
    cfg_path = str(tmp_path / "config")
    entry = _discovered_entry()
    with patch("trenchchat.core.discovery.list_discovered_interfaces",
               return_value=[entry]):
        pin_discovered_interface(cfg_path, "ef" * 16)
        pin_discovered_interface(cfg_path, "ef" * 16)
    assert list(load_interfaces_config(cfg_path)) == ["Test Hub"]


# ---------------------------------------------------------------------------
# _to_jsonable
# ---------------------------------------------------------------------------

def test_to_jsonable_hexes_bytes_and_flags_pinnable():
    entry = _discovered_entry(discovery_hash=bytes.fromhex("ef" * 16))
    out = _to_jsonable(entry)
    assert out["discovery_hash"] == "ef" * 16
    assert out["pinnable"] is True


def test_to_jsonable_radio_types_not_pinnable():
    out = _to_jsonable(_discovered_entry(type="RNodeInterface"))
    assert out["pinnable"] is False


# ---------------------------------------------------------------------------
# discovery settings
# ---------------------------------------------------------------------------

def test_discovery_settings_default_off(tmp_path):
    settings = load_discovery_settings(str(tmp_path / "missing"))
    assert settings == {
        "discover_interfaces": False,
        "autoconnect_discovered_interfaces": 0,
        "required_discovery_value": None,
    }


def test_discovery_settings_round_trip(tmp_path):
    cfg_path = str(tmp_path / "config")
    write_discovery_settings(cfg_path, True, 3, 16)
    settings = load_discovery_settings(cfg_path)
    assert settings["discover_interfaces"] is True
    assert settings["autoconnect_discovered_interfaces"] == 3
    assert settings["required_discovery_value"] == 16


def test_discovery_settings_preserve_other_reticulum_keys(tmp_path):
    cfg_path = str(tmp_path / "config")
    with open(cfg_path, "w") as f:
        f.write("[reticulum]\n  enable_transport = No\n")
    write_discovery_settings(cfg_path, True, 3)
    with open(cfg_path) as f:
        content = f.read()
    assert "enable_transport = No" in content
    assert "discover_interfaces = Yes" in content


def test_discovery_settings_disable_clears_autoconnect(tmp_path):
    cfg_path = str(tmp_path / "config")
    write_discovery_settings(cfg_path, True, 3)
    write_discovery_settings(cfg_path, False, 0)
    settings = load_discovery_settings(cfg_path)
    assert settings["discover_interfaces"] is False
    assert settings["autoconnect_discovered_interfaces"] == 0


# ---------------------------------------------------------------------------
# bootstrap seeds
# ---------------------------------------------------------------------------

def test_suggested_defaults_are_bootstrap_only():
    for cfg in SUGGESTED_DEFAULTS.values():
        assert cfg["type"] == "TCPClientInterface"
        assert cfg["bootstrap_only"] == "Yes"
        assert cfg["target_host"]
        assert cfg["target_port"]


def test_apply_suggested_defaults_writes_seeds_and_enables_discovery(tmp_path):
    cfg_path = str(tmp_path / "config")
    added = apply_suggested_defaults(cfg_path)
    assert set(added) == set(SUGGESTED_DEFAULTS)
    assert get_missing_suggested_defaults(cfg_path) == {}
    settings = load_discovery_settings(cfg_path)
    assert settings["discover_interfaces"] is True
    assert settings["autoconnect_discovered_interfaces"] > 0


def test_apply_suggested_defaults_is_idempotent(tmp_path):
    cfg_path = str(tmp_path / "config")
    apply_suggested_defaults(cfg_path)
    assert apply_suggested_defaults(cfg_path) == []
    seeds = [n for n in load_interfaces_config(cfg_path) if n in SUGGESTED_DEFAULTS]
    assert len(seeds) == len(SUGGESTED_DEFAULTS)


def test_apply_suggested_defaults_keeps_higher_autoconnect(tmp_path):
    cfg_path = str(tmp_path / "config")
    write_discovery_settings(cfg_path, False, 5)
    apply_suggested_defaults(cfg_path)
    assert load_discovery_settings(cfg_path)["autoconnect_discovered_interfaces"] == 5


# ---------------------------------------------------------------------------
# first-run config seeding
# ---------------------------------------------------------------------------

def test_seed_initial_config_creates_full_config(tmp_path):
    cfg_path = str(tmp_path / "rns" / "config")
    assert seed_initial_config(cfg_path) is True
    interfaces = load_interfaces_config(cfg_path)
    assert interfaces["Default Interface"]["type"] == "AutoInterface"
    for name in SUGGESTED_DEFAULTS:
        assert interfaces[name]["bootstrap_only"] == "Yes"
    settings = load_discovery_settings(cfg_path)
    assert settings["discover_interfaces"] is True
    assert settings["autoconnect_discovered_interfaces"] > 0
    assert get_missing_suggested_defaults(cfg_path) == {}


def test_seed_initial_config_never_touches_existing(tmp_path):
    cfg_path = str(tmp_path / "config")
    with open(cfg_path, "w") as f:
        f.write("[interfaces]\n")
    assert seed_initial_config(cfg_path) is False
    with open(cfg_path) as f:
        assert f.read() == "[interfaces]\n"


def test_default_rns_config_path_explicit_dir(tmp_path):
    assert default_rns_config_path(str(tmp_path)) == str(tmp_path / "config")
