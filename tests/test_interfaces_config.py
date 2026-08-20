"""
Tests for trenchchat.core.interfaces_config -- the write/delete logic
InterfacesWidget and devtools/testenv/api.py's write endpoints both call.
"""

import pytest

from trenchchat.core.interfaces_config import (
    DuplicateInterfaceError, InterfaceConfigError,
    build_interface_config_dict, delete_interface, load_interfaces_config,
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
