"""
The node-wide Reticulum config reader/writer (trenchchat/core/reticulum_config.py).

Pure file manipulation, so no peers and no network: every test works on a
config file in tmp_path.
"""

import pytest
from configobj import ConfigObj

from trenchchat.core.interfaces_config import (
    InterfaceConfigError, load_discovery_settings, write_discovery_settings,
)
from trenchchat.core.reticulum_config import (
    RETICULUM_OPTIONS, load_reticulum_config, write_reticulum_config,
)

EXPECTED_KEYS = {
    # [logging]
    "loglevel", "logtimestamps",
    # instance
    "share_instance", "shared_instance_type", "instance_name",
    "shared_instance_port", "instance_control_port", "rpc_key",
    "force_shared_instance_bitrate",
    # transport & routing
    "enable_transport", "static_transport_identity", "network_identity",
    "local_hops_delta", "link_mtu_discovery", "use_implicit_proof",
    "respond_to_probes", "panic_on_interface_error", "default_gravity",
    # remote management
    "enable_remote_management", "remote_management_allowed",
    # interface discovery
    "discover_interfaces", "autoconnect_discovered_interfaces",
    "required_discovery_value", "interface_discovery_sources",
    "autoconnect_interface_mode", "autoconnect_interface_gravity",
    "autoconnect_announces_to_internal",
    # blackholes
    "publish_blackhole", "blackhole_sources", "blackhole_update_interval",
    # announce rate limits
    "default_ar_target", "default_ar_penalty", "default_ar_grace",
    # ingress/egress control
    "ic_max_held_announces", "ic_burst_hold", "ic_burst_freq_new",
    "ic_burst_freq", "ic_pr_burst_freq_new", "ic_pr_burst_freq", "ec_pr_freq",
    "egress_control", "ic_new_time", "ic_burst_penalty",
    "ic_held_release_interval",
}

HASH_A = "ab" * 16
HASH_B = "cd" * 16


@pytest.fixture
def config_path(tmp_path):
    return str(tmp_path / "config")


def values_by_key(config_path):
    return {opt["key"]: opt["value"] for opt in load_reticulum_config(config_path)}


class TestSchema:
    def test_covers_every_node_wide_option(self):
        assert {opt["key"] for opt in RETICULUM_OPTIONS} == EXPECTED_KEYS

    def test_every_entry_is_complete(self):
        for opt in RETICULUM_OPTIONS:
            assert opt["section"] in ("reticulum", "logging"), opt["key"]
            assert opt["kind"] in (
                "bool", "int", "float", "str", "choice", "hex", "hash_list"), opt["key"]
            assert opt["category"], opt["key"]
            assert opt["label"], opt["key"]
            assert opt["default"], opt["key"]
            # Every tooltip explains the setting and its downside, so a
            # one-liner stub is not enough.
            assert len(opt["description"]) > 60, opt["key"]
            if opt["kind"] == "choice":
                assert opt["choices"], opt["key"]

    def test_keys_are_unique(self):
        keys = [opt["key"] for opt in RETICULUM_OPTIONS]
        assert len(keys) == len(set(keys))


class TestLoad:
    def test_missing_file_leaves_everything_unset(self, config_path):
        options = load_reticulum_config(config_path)
        assert {opt["key"] for opt in options} == EXPECTED_KEYS
        assert all(opt["value"] == "" for opt in options)

    def test_unreadable_file_leaves_everything_unset(self, tmp_path):
        path = tmp_path / "config"
        path.write_text("[reticulum\nbroken = ")
        assert all(opt["value"] == "" for opt in load_reticulum_config(str(path)))

    def test_reads_both_sections(self, tmp_path):
        path = tmp_path / "config"
        path.write_text(
            "[reticulum]\nenable_transport = Yes\n\n[logging]\nloglevel = 6\n")
        values = values_by_key(str(path))
        assert values["enable_transport"] == "Yes"
        assert values["loglevel"] == "6"
        assert values["share_instance"] == ""

    def test_flattens_a_comma_separated_list(self, tmp_path):
        path = tmp_path / "config"
        path.write_text(f"[reticulum]\nblackhole_sources = {HASH_A}, {HASH_B}\n")
        assert values_by_key(str(path))["blackhole_sources"] == f"{HASH_A}, {HASH_B}"


class TestWrite:
    def test_round_trips_every_kind(self, config_path):
        write_reticulum_config(config_path, {
            "loglevel": "6",
            "logtimestamps": "no",
            "enable_transport": "true",
            "shared_instance_port": "37500",
            "shared_instance_type": "TCP",
            "instance_name": "second",
            "rpc_key": "deadbeef",
            "blackhole_update_interval": "12.5",
            "blackhole_sources": f"{HASH_A},{HASH_B}",
        })
        values = values_by_key(config_path)
        assert values["loglevel"] == "6"
        assert values["logtimestamps"] == "No"
        assert values["enable_transport"] == "Yes"
        assert values["shared_instance_port"] == "37500"
        assert values["shared_instance_type"] == "tcp"
        assert values["instance_name"] == "second"
        assert values["rpc_key"] == "deadbeef"
        assert values["blackhole_update_interval"] == "12.5"
        assert values["blackhole_sources"] == f"{HASH_A}, {HASH_B}"

    def test_empty_value_removes_the_key(self, config_path):
        write_reticulum_config(config_path, {"enable_transport": "Yes"})
        assert values_by_key(config_path)["enable_transport"] == "Yes"

        write_reticulum_config(config_path, {"enable_transport": ""})
        assert values_by_key(config_path)["enable_transport"] == ""
        assert "enable_transport" not in ConfigObj(config_path)["reticulum"]

    def test_only_touches_the_provided_keys(self, tmp_path):
        path = str(tmp_path / "config")
        cfg = ConfigObj()
        cfg.filename = path
        cfg["reticulum"] = {"enable_transport": "Yes", "some_future_option": "42"}
        cfg["interfaces"] = {"My Hub": {"type": "TCPClientInterface", "enabled": "Yes"}}
        cfg.write()

        write_reticulum_config(path, {"loglevel": "2"})

        after = ConfigObj(path)
        assert after["reticulum"]["enable_transport"] == "Yes"
        assert after["reticulum"]["some_future_option"] == "42"
        assert after["interfaces"]["My Hub"]["type"] == "TCPClientInterface"
        assert after["logging"]["loglevel"] == "2"

    def test_creates_missing_sections(self, config_path):
        write_reticulum_config(config_path, {"loglevel": "3", "respond_to_probes": "yes"})
        cfg = ConfigObj(config_path)
        assert cfg["logging"]["loglevel"] == "3"
        assert cfg["reticulum"]["respond_to_probes"] == "Yes"


class TestValidation:
    def test_unknown_key_is_rejected(self, config_path):
        with pytest.raises(InterfaceConfigError, match="not_a_real_option"):
            write_reticulum_config(config_path, {"not_a_real_option": "1"})

    @pytest.mark.parametrize("key,value", [
        ("shared_instance_port", "port"),
        ("default_gravity", "1.5"),
        ("blackhole_update_interval", "soon"),
        ("shared_instance_type", "carrier_pigeon"),
        ("autoconnect_interface_mode", "sideways"),
        ("rpc_key", "nothex!"),
        ("enable_transport", "maybe"),
        ("blackhole_sources", "abc"),
        ("remote_management_allowed", "zz" * 16),
        ("loglevel", "9"),
        ("loglevel", "-1"),
    ])
    def test_bad_value_names_the_key(self, config_path, key, value):
        with pytest.raises(InterfaceConfigError, match=key):
            write_reticulum_config(config_path, {key: value})

    def test_a_rejected_write_changes_nothing(self, config_path):
        write_reticulum_config(config_path, {"loglevel": "5"})
        with pytest.raises(InterfaceConfigError):
            write_reticulum_config(config_path, {"loglevel": "2", "rpc_key": "zz"})
        assert values_by_key(config_path)["loglevel"] == "5"


class TestDiscoveryWriterInterplay:
    """The three discovery keys have a second writer in interfaces_config;
    both must be able to write the same file without breaking the other."""

    def test_discovery_settings_survive_a_node_config_write(self, config_path):
        write_discovery_settings(config_path, True, 3, 12)
        write_reticulum_config(config_path, {"enable_transport": "Yes"})

        settings = load_discovery_settings(config_path)
        assert settings["discover_interfaces"] is True
        assert settings["autoconnect_discovered_interfaces"] == 3
        assert settings["required_discovery_value"] == 12
        assert values_by_key(config_path)["enable_transport"] == "Yes"

    def test_node_config_write_is_readable_by_the_discovery_reader(self, config_path):
        write_reticulum_config(config_path, {
            "discover_interfaces": "yes",
            "autoconnect_discovered_interfaces": "5",
            "required_discovery_value": "8",
        })
        settings = load_discovery_settings(config_path)
        assert settings["discover_interfaces"] is True
        assert settings["autoconnect_discovered_interfaces"] == 5
        assert settings["required_discovery_value"] == 8

    def test_discovery_writer_output_is_readable_here(self, config_path):
        write_discovery_settings(config_path, True, 2)
        values = values_by_key(config_path)
        assert values["discover_interfaces"] == "Yes"
        assert values["autoconnect_discovered_interfaces"] == "2"
        assert values["required_discovery_value"] == ""
