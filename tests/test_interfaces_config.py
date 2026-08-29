"""
Tests for trenchchat.core.interfaces_config -- the load/build/write/delete
logic behind devtools/testenv/api.py's interfaces endpoints.
"""

import pytest
from configobj import ConfigObj

from trenchchat.core.interfaces_config import (
    DuplicateInterfaceError, EDITABLE_TYPES, InterfaceConfigError,
    SUGGESTED_DEFAULTS, build_interface_config_dict, delete_interface,
    get_missing_suggested_defaults, load_interfaces_config,
    missing_required_field,
    write_interface, write_interfaces_bulk,
)


# ---------------------------------------------------------------------------
# missing_required_field
# ---------------------------------------------------------------------------

def test_missing_required_field_present():
    assert missing_required_field("TCPClientInterface", {"target_host": "10.0.0.1"}) is None


def test_missing_required_field_absent():
    assert missing_required_field("TCPClientInterface", {}) == "target_host"


def test_missing_required_field_treats_zero_as_missing():
    assert missing_required_field("TCPServerInterface",
                                  {"listen_ip": "0.0.0.0", "listen_port": "0"}) == "listen_port"


def test_missing_required_field_type_with_no_requirements():
    assert missing_required_field("AutoInterface", {}) is None


# ---------------------------------------------------------------------------
# write_interface
# ---------------------------------------------------------------------------

def test_write_interface_creates_new(tmp_path):
    cfg_path = tmp_path / "config"
    cfg_path.write_text("[reticulum]\nenable_transport = No\n\n[interfaces]\n")

    write_interface(str(cfg_path), "My Hub",
                    {"type": "TCPClientInterface", "enabled": "Yes",
                     "target_host": "hub.example.com"}, is_new=True)

    result = load_interfaces_config(str(cfg_path))
    assert result["My Hub"]["target_host"] == "hub.example.com"


def test_write_interface_duplicate_name_raises(tmp_path):
    cfg_path = tmp_path / "config"
    cfg_path.write_text(
        "[interfaces]\n  [[Existing]]\n    type = AutoInterface\n    enabled = Yes\n"
    )

    with pytest.raises(DuplicateInterfaceError):
        write_interface(str(cfg_path), "Existing",
                        {"type": "AutoInterface", "enabled": "Yes"}, is_new=True)


def test_write_interface_edit_overwrites_existing(tmp_path):
    cfg_path = tmp_path / "config"
    cfg_path.write_text(
        "[interfaces]\n  [[Existing]]\n    type = AutoInterface\n    enabled = Yes\n"
    )

    write_interface(str(cfg_path), "Existing",
                    {"type": "AutoInterface", "enabled": "No"}, is_new=False)

    result = load_interfaces_config(str(cfg_path))
    assert result["Existing"]["enabled"] == "No"


def test_write_interface_bad_config_path_raises(tmp_path):
    # A directory in place of the config path makes ConfigObj's read fail.
    bad_path = tmp_path / "not_a_file"
    bad_path.mkdir()

    with pytest.raises(InterfaceConfigError):
        write_interface(str(bad_path), "Hub",
                        {"type": "AutoInterface", "enabled": "Yes"}, is_new=True)


# ---------------------------------------------------------------------------
# write_interfaces_bulk
# ---------------------------------------------------------------------------

def test_write_interfaces_bulk_adds_all(tmp_path):
    cfg_path = tmp_path / "config"
    cfg_path.write_text("[interfaces]\n")

    write_interfaces_bulk(str(cfg_path), {
        "RMAP": {"type": "TCPClientInterface", "enabled": "Yes", "target_host": "rmap.world"},
        "QUAD4": {"type": "TCPClientInterface", "enabled": "Yes", "target_host": "62.151.179.77"},
    })

    result = load_interfaces_config(str(cfg_path))
    assert set(result.keys()) == {"RMAP", "QUAD4"}


# ---------------------------------------------------------------------------
# delete_interface
# ---------------------------------------------------------------------------

def test_delete_interface_removes_it(tmp_path):
    cfg_path = tmp_path / "config"
    cfg_path.write_text(
        "[interfaces]\n"
        "  [[Keep Me]]\n    type = AutoInterface\n    enabled = Yes\n"
        "  [[Delete Me]]\n    type = AutoInterface\n    enabled = Yes\n"
    )

    deleted = delete_interface(str(cfg_path), "Delete Me")

    assert deleted is True
    result = load_interfaces_config(str(cfg_path))
    assert "Delete Me" not in result
    assert "Keep Me" in result


def test_delete_interface_missing_name_returns_false(tmp_path):
    cfg_path = tmp_path / "config"
    cfg_path.write_text("[interfaces]\n  [[Keep Me]]\n    type = AutoInterface\n")

    deleted = delete_interface(str(cfg_path), "Nonexistent")

    assert deleted is False
    result = load_interfaces_config(str(cfg_path))
    assert "Keep Me" in result


# ---------------------------------------------------------------------------
# Interface type is decided by the validated argument, never by a field value
# ---------------------------------------------------------------------------

def test_build_rejects_a_type_key_smuggled_through_type_values():
    """
    The caller-supplied field dicts used to be copied over the assembled
    config, so a key named "type" replaced the value EDITABLE_TYPES had just
    approved -- reaching PipeInterface, which runs a shell command.
    """
    with pytest.raises(InterfaceConfigError):
        build_interface_config_dict(
            "Sneaky", "AutoInterface", True,
            {"type": "PipeInterface", "command": "touch /tmp/pwned"}, {},
        )


def test_build_rejects_a_type_key_smuggled_through_common_values():
    with pytest.raises(InterfaceConfigError):
        build_interface_config_dict(
            "Sneaky", "AutoInterface", True, {},
            {"type": "PipeInterface", "command": "touch /tmp/pwned"},
        )


def test_build_rejects_an_enabled_key_from_a_field_value():
    with pytest.raises(InterfaceConfigError):
        build_interface_config_dict("Sneaky", "AutoInterface", False,
                                    {"enabled": "Yes"}, {})


def test_build_rejects_a_type_that_is_not_editable():
    with pytest.raises(InterfaceConfigError):
        build_interface_config_dict("Sneaky", "PipeInterface", True,
                                    {"command": "touch /tmp/pwned"}, {})


def test_build_still_assembles_an_ordinary_interface():
    cfg = build_interface_config_dict(
        "My Hub", "TCPClientInterface", True,
        {"target_host": "hub.example.com", "target_port": "4965"},
        {"bitrate": "0"},
    )
    assert cfg["type"] == "TCPClientInterface"
    assert cfg["enabled"] == "Yes"
    assert cfg["target_host"] == "hub.example.com"
    assert "bitrate" not in cfg          # "0" means unset


def test_write_interface_refuses_a_non_editable_type(tmp_path):
    """The last layer before an interface class reaches the RNS config."""
    cfg_path = tmp_path / "config"
    cfg_path.write_text("[interfaces]\n")

    with pytest.raises(InterfaceConfigError):
        write_interface(str(cfg_path), "Sneaky",
                        {"type": "PipeInterface", "enabled": "Yes",
                         "command": "touch /tmp/pwned"}, is_new=True)

    assert load_interfaces_config(str(cfg_path)) == {}


# ---------------------------------------------------------------------------
# load_interfaces_config
# ---------------------------------------------------------------------------

def test_load_interfaces_config_missing_file(tmp_path):
    """Returns empty dict when the config file does not exist."""
    result = load_interfaces_config(str(tmp_path / "nonexistent.cfg"))
    assert result == {}


def test_load_interfaces_config_no_interfaces_section(tmp_path):
    """Returns empty dict when the config has no [interfaces] section."""
    cfg_path = tmp_path / "config"
    cfg_path.write_text("[reticulum]\nenable_transport = No\n")
    result = load_interfaces_config(str(cfg_path))
    assert result == {}


def test_load_interfaces_config_single_interface(tmp_path):
    """Parses a single interface section correctly."""
    cfg_path = tmp_path / "config"
    cfg_path.write_text(
        "[interfaces]\n"
        "  [[My Hub]]\n"
        "    type = TCPClientInterface\n"
        "    enabled = Yes\n"
        "    target_host = 192.168.1.1\n"
        "    target_port = 4965\n"
    )
    result = load_interfaces_config(str(cfg_path))
    assert "My Hub" in result
    iface = result["My Hub"]
    assert iface["type"] == "TCPClientInterface"
    assert iface["target_host"] == "192.168.1.1"
    assert iface["target_port"] == "4965"


def test_load_interfaces_config_multiple_interfaces(tmp_path):
    """Parses multiple interface sections."""
    cfg_path = tmp_path / "config"
    cfg_path.write_text(
        "[interfaces]\n"
        "  [[Auto]]\n"
        "    type = AutoInterface\n"
        "    enabled = Yes\n"
        "  [[Serial Port]]\n"
        "    type = SerialInterface\n"
        "    enabled = No\n"
        "    port = /dev/ttyUSB0\n"
    )
    result = load_interfaces_config(str(cfg_path))
    assert set(result.keys()) == {"Auto", "Serial Port"}
    assert result["Auto"]["type"] == "AutoInterface"
    assert result["Serial Port"]["port"] == "/dev/ttyUSB0"


def test_load_interfaces_config_unsupported_type_included(tmp_path):
    """Interfaces of unsupported types are still returned (for display)."""
    cfg_path = tmp_path / "config"
    cfg_path.write_text(
        "[interfaces]\n"
        "  [[KISS Radio]]\n"
        "    type = KISSInterface\n"
        "    enabled = Yes\n"
        "    port = /dev/ttyS0\n"
    )
    result = load_interfaces_config(str(cfg_path))
    assert "KISS Radio" in result
    assert result["KISS Radio"]["type"] == "KISSInterface"


# ---------------------------------------------------------------------------
# build_interface_config_dict
# ---------------------------------------------------------------------------

def test_build_config_dict_enabled_yes():
    cfg = build_interface_config_dict(
        "Test", "AutoInterface", True, {}, {}
    )
    assert cfg["enabled"] == "Yes"
    assert cfg["type"] == "AutoInterface"


def test_build_config_dict_enabled_no():
    cfg = build_interface_config_dict(
        "Test", "AutoInterface", False, {}, {}
    )
    assert cfg["enabled"] == "No"


def test_build_config_dict_type_specific_values():
    type_vals = {"target_host": "10.0.0.1", "target_port": "4965"}
    cfg = build_interface_config_dict(
        "Hub", "TCPClientInterface", True, type_vals, {}
    )
    assert cfg["target_host"] == "10.0.0.1"
    assert cfg["target_port"] == "4965"


def test_build_config_dict_empty_values_excluded():
    """Empty string values should not be written to the config."""
    type_vals = {"target_host": "", "target_port": "4965"}
    cfg = build_interface_config_dict(
        "Hub", "TCPClientInterface", True, type_vals, {}
    )
    assert "target_host" not in cfg
    assert cfg["target_port"] == "4965"


def test_build_config_dict_common_values_included():
    common_vals = {"networkname": "mynet", "passphrase": "secret"}
    cfg = build_interface_config_dict(
        "Hub", "AutoInterface", True, {}, common_vals
    )
    assert cfg["networkname"] == "mynet"
    assert cfg["passphrase"] == "secret"


def test_build_config_dict_all_values_are_strings():
    """All values in the resulting dict must be strings (ConfigObj INI format)."""
    type_vals = {"target_port": 4965, "kiss_framing": False}
    cfg = build_interface_config_dict(
        "Hub", "TCPClientInterface", True, type_vals, {}
    )
    for key, val in cfg.items():
        assert isinstance(val, str), f"Value for '{key}' is not a string: {val!r}"


def test_build_config_dict_zero_values_excluded():
    """"0" and "0.0" are treated as unset."""
    type_vals = {"bitrate": "0", "airtime_limit_short": "0.0", "txpower": "14"}
    cfg = build_interface_config_dict(
        "Hub", "RNodeInterface", True, type_vals, {}
    )
    assert "bitrate" not in cfg
    assert "airtime_limit_short" not in cfg
    assert cfg["txpower"] == "14"


# ---------------------------------------------------------------------------
# Config write round-trip
# ---------------------------------------------------------------------------

def test_config_write_round_trip(tmp_path):
    """Write an interface to a config file and read it back correctly."""
    cfg_path = tmp_path / "config"
    cfg_path.write_text(
        "[reticulum]\nenable_transport = No\n\n[interfaces]\n"
    )

    file_cfg = ConfigObj(str(cfg_path))
    if "interfaces" not in file_cfg:
        file_cfg["interfaces"] = {}
    file_cfg["interfaces"]["My TCP Hub"] = {
        "type": "TCPClientInterface",
        "enabled": "Yes",
        "target_host": "hub.example.com",
        "target_port": "4965",
    }
    file_cfg.write()

    result = load_interfaces_config(str(cfg_path))
    assert "My TCP Hub" in result
    iface = result["My TCP Hub"]
    assert iface["type"] == "TCPClientInterface"
    assert iface["target_host"] == "hub.example.com"
    assert iface["target_port"] == "4965"


def test_config_write_preserves_existing_interfaces(tmp_path):
    """Adding a new interface does not remove existing ones."""
    cfg_path = tmp_path / "config"
    cfg_path.write_text(
        "[interfaces]\n"
        "  [[Existing]]\n"
        "    type = AutoInterface\n"
        "    enabled = Yes\n"
    )

    file_cfg = ConfigObj(str(cfg_path))
    file_cfg["interfaces"]["New Hub"] = {
        "type": "TCPClientInterface",
        "enabled": "Yes",
        "target_host": "10.0.0.1",
        "target_port": "4965",
    }
    file_cfg.write()

    result = load_interfaces_config(str(cfg_path))
    assert "Existing" in result
    assert "New Hub" in result


def test_config_delete_interface(tmp_path):
    """Deleting an interface from the config removes it on read-back."""
    cfg_path = tmp_path / "config"
    cfg_path.write_text(
        "[interfaces]\n"
        "  [[Keep Me]]\n"
        "    type = AutoInterface\n"
        "    enabled = Yes\n"
        "  [[Delete Me]]\n"
        "    type = SerialInterface\n"
        "    enabled = Yes\n"
        "    port = /dev/ttyUSB0\n"
    )

    file_cfg = ConfigObj(str(cfg_path))
    del file_cfg["interfaces"]["Delete Me"]
    file_cfg.write()

    result = load_interfaces_config(str(cfg_path))
    assert "Keep Me" in result
    assert "Delete Me" not in result


# ---------------------------------------------------------------------------
# EDITABLE_TYPES coverage
# ---------------------------------------------------------------------------

def test_editable_types_are_all_supported():
    """All expected interface types are in EDITABLE_TYPES."""
    expected = {
        "AutoInterface",
        "TCPClientInterface",
        "TCPServerInterface",
        "UDPInterface",
        "SerialInterface",
        "RNodeInterface",
    }
    assert expected.issubset(set(EDITABLE_TYPES))


def test_unsupported_types_not_in_editable_types():
    """Types that are display-only should not be in EDITABLE_TYPES."""
    unsupported = ["KISSInterface", "AX25KISSInterface", "PipeInterface",
                   "I2PInterface", "WeaveInterface"]
    for t in unsupported:
        assert t not in EDITABLE_TYPES, f"{t} should not be editable"


# ---------------------------------------------------------------------------
# get_missing_suggested_defaults
# ---------------------------------------------------------------------------

def test_missing_suggested_defaults_all_missing(tmp_path):
    """When no suggested defaults are present, all are returned as missing."""
    cfg_path = tmp_path / "config"
    cfg_path.write_text("[reticulum]\nenable_transport = No\n\n[interfaces]\n")
    missing = get_missing_suggested_defaults(str(cfg_path))
    assert set(missing.keys()) == set(SUGGESTED_DEFAULTS.keys())


def test_missing_suggested_defaults_none_missing(tmp_path):
    """When all suggested defaults are already configured, returns empty dict."""
    cfg_path = tmp_path / "config"
    lines = ["[interfaces]\n"]
    for name, cfg in SUGGESTED_DEFAULTS.items():
        lines.append(f"  [[{name}]]\n")
        for k, v in cfg.items():
            lines.append(f"    {k} = {v}\n")
    cfg_path.write_text("".join(lines))
    missing = get_missing_suggested_defaults(str(cfg_path))
    assert missing == {}


def test_missing_suggested_defaults_one_present(tmp_path):
    """When only one suggested default is present, the other is returned as missing."""
    cfg_path = tmp_path / "config"
    first_name = next(iter(SUGGESTED_DEFAULTS))
    first_cfg = SUGGESTED_DEFAULTS[first_name]
    lines = [f"[interfaces]\n  [[{first_name}]]\n"]
    for k, v in first_cfg.items():
        lines.append(f"    {k} = {v}\n")
    cfg_path.write_text("".join(lines))
    missing = get_missing_suggested_defaults(str(cfg_path))
    assert first_name not in missing
    remaining = set(SUGGESTED_DEFAULTS.keys()) - {first_name}
    assert set(missing.keys()) == remaining


def test_missing_suggested_defaults_empty_config_file(tmp_path):
    """When the config file does not exist, all suggested defaults are missing."""
    missing = get_missing_suggested_defaults(str(tmp_path / "nonexistent"))
    assert set(missing.keys()) == set(SUGGESTED_DEFAULTS.keys())


def test_missing_suggested_defaults_returns_correct_config(tmp_path):
    """Missing entries carry the full config dict from SUGGESTED_DEFAULTS."""
    cfg_path = tmp_path / "config"
    cfg_path.write_text("[reticulum]\nenable_transport = No\n\n[interfaces]\n")
    missing = get_missing_suggested_defaults(str(cfg_path))
    for name, cfg in missing.items():
        assert cfg == SUGGESTED_DEFAULTS[name]


def test_missing_suggested_defaults_different_name_same_endpoint(tmp_path):
    """An interface already present under a different name is not reported as missing."""
    cfg_path = tmp_path / "config"
    lines = ["[interfaces]\n"]
    for _name, cfg in SUGGESTED_DEFAULTS.items():
        lines.append("  [[My Custom Name]]\n")
        for k, v in cfg.items():
            lines.append(f"    {k} = {v}\n")
        break
    cfg_path.write_text("".join(lines))
    missing = get_missing_suggested_defaults(str(cfg_path))
    first_name = next(iter(SUGGESTED_DEFAULTS))
    assert first_name not in missing
