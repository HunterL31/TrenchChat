"""
Tests for trenchchat.config.Config.

Covers defensive on-disk value handling (wrong-typed nested values must fall
back to defaults rather than crash on property access) and setter validation
for voice bitrate, propagation storage limit, UI theme size/count caps, the
direct session's switch and port, and the public address echo.
"""

import json

import pytest

from trenchchat.config import (
    Config,
    MAX_STUN_SERVERS,
    MAX_THEME_BYTES,
    MAX_THEME_LIBRARY_ENTRIES,
    UPGRADE_DEFAULT_PORT,
    VOICE_MAX_BITRATE,
    VOICE_MIN_BITRATE,
)


def _write_config(data_dir, data) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "config.json").write_text(json.dumps(data))


# ---------------------------------------------------------------------------
# Bug 72 -- wrong-typed nested on-disk values must not crash property access
# ---------------------------------------------------------------------------

class TestDefensiveMerge:
    def test_nested_dict_replaced_by_scalar_falls_back(self, tmp_path):
        """A string where the 'voice' dict is expected must not crash bitrate."""
        _write_config(tmp_path, {"voice": "x"})
        config = Config(data_dir=tmp_path)
        assert config.voice_bitrate == 16000
        assert config.voice_mode == "vad"

    def test_wrong_typed_scalar_in_nested_dict_falls_back(self, tmp_path):
        _write_config(tmp_path, {"voice": {"bitrate": "not-a-number"}})
        config = Config(data_dir=tmp_path)
        assert config.voice_bitrate == 16000

    def test_wrong_typed_propagation_dict_falls_back(self, tmp_path):
        _write_config(tmp_path, {"propagation_node": 5})
        config = Config(data_dir=tmp_path)
        assert config.propagation_storage_limit_mb == 256
        assert config.propagation_enabled is False

    def test_valid_nested_override_still_applies(self, tmp_path):
        _write_config(tmp_path, {"voice": {"bitrate": 24000}})
        config = Config(data_dir=tmp_path)
        assert config.voice_bitrate == 24000

    def test_non_object_config_is_ignored(self, tmp_path):
        _write_config(tmp_path, ["not", "an", "object"])
        config = Config(data_dir=tmp_path)
        assert config.display_name == "Anonymous"

    def test_unknown_keys_pass_through(self, tmp_path):
        _write_config(tmp_path, {"future_field": {"a": 1}})
        config = Config(data_dir=tmp_path)
        assert config.display_name == "Anonymous"


# ---------------------------------------------------------------------------
# Bug 73 -- voice_bitrate must reject values outside the Opus range
# ---------------------------------------------------------------------------

class TestVoiceBitrateValidation:
    def test_in_range_accepted(self, tmp_path):
        config = Config(data_dir=tmp_path)
        config.voice_bitrate = 32000
        assert config.voice_bitrate == 32000

    def test_bounds_are_accepted(self, tmp_path):
        config = Config(data_dir=tmp_path)
        config.voice_bitrate = VOICE_MIN_BITRATE
        assert config.voice_bitrate == VOICE_MIN_BITRATE
        config.voice_bitrate = VOICE_MAX_BITRATE
        assert config.voice_bitrate == VOICE_MAX_BITRATE

    def test_too_high_rejected(self, tmp_path):
        config = Config(data_dir=tmp_path)
        with pytest.raises(ValueError):
            config.voice_bitrate = VOICE_MAX_BITRATE + 1
        assert config.voice_bitrate == 16000

    def test_negative_rejected(self, tmp_path):
        config = Config(data_dir=tmp_path)
        with pytest.raises(ValueError):
            config.voice_bitrate = -1

    def test_zero_rejected(self, tmp_path):
        config = Config(data_dir=tmp_path)
        with pytest.raises(ValueError):
            config.voice_bitrate = 0


# ---------------------------------------------------------------------------
# Bug 74 -- propagation_storage_limit_mb must reject negative values
# ---------------------------------------------------------------------------

class TestStorageLimitValidation:
    def test_positive_accepted(self, tmp_path):
        config = Config(data_dir=tmp_path)
        config.propagation_storage_limit_mb = 512
        assert config.propagation_storage_limit_mb == 512

    def test_zero_accepted(self, tmp_path):
        config = Config(data_dir=tmp_path)
        config.propagation_storage_limit_mb = 0
        assert config.propagation_storage_limit_mb == 0

    def test_negative_rejected(self, tmp_path):
        config = Config(data_dir=tmp_path)
        with pytest.raises(ValueError):
            config.propagation_storage_limit_mb = -1
        assert config.propagation_storage_limit_mb == 256


# ---------------------------------------------------------------------------
# Bug 75 -- UI theme size/count caps
# ---------------------------------------------------------------------------

class TestThemeCaps:
    def test_small_theme_accepted(self, tmp_path):
        config = Config(data_dir=tmp_path)
        config.ui_theme = {"accent": "#ff8800"}
        assert config.ui_theme == {"accent": "#ff8800"}

    def test_oversized_theme_rejected(self, tmp_path):
        config = Config(data_dir=tmp_path)
        huge = {"blob": "x" * (MAX_THEME_BYTES + 1)}
        with pytest.raises(ValueError):
            config.ui_theme = huge
        assert config.ui_theme == {}

    def test_oversized_library_theme_rejected(self, tmp_path):
        config = Config(data_dir=tmp_path)
        huge = {"blob": "x" * (MAX_THEME_BYTES + 1)}
        with pytest.raises(ValueError):
            config.save_ui_theme("big", huge)
        assert config.ui_theme_library == {}

    def test_library_entry_count_capped(self, tmp_path):
        config = Config(data_dir=tmp_path)
        for i in range(MAX_THEME_LIBRARY_ENTRIES):
            config.save_ui_theme(f"theme{i}", {"accent": "#000000"})
        assert len(config.ui_theme_library) == MAX_THEME_LIBRARY_ENTRIES
        with pytest.raises(ValueError):
            config.save_ui_theme("one-too-many", {"accent": "#ffffff"})
        assert len(config.ui_theme_library) == MAX_THEME_LIBRARY_ENTRIES

    def test_overwriting_existing_name_allowed_at_capacity(self, tmp_path):
        config = Config(data_dir=tmp_path)
        for i in range(MAX_THEME_LIBRARY_ENTRIES):
            config.save_ui_theme(f"theme{i}", {"accent": "#000000"})
        # Replacing an existing entry must not be blocked by the count cap.
        config.save_ui_theme("theme0", {"accent": "#123456"})
        assert config.ui_theme_library["theme0"] == {"accent": "#123456"}


class TestDirectSessionSettings:
    """The "upgrade" block: whether this node holds direct sessions, and where."""

    def test_defaults_are_on_and_off_the_test_environments_ports(self, tmp_path):
        config = Config(data_dir=tmp_path)
        assert config.upgrade_enabled is True
        assert config.upgrade_listen_port == UPGRADE_DEFAULT_PORT
        assert not 8800 <= UPGRADE_DEFAULT_PORT <= 8899
        assert not 41001 <= UPGRADE_DEFAULT_PORT <= 41199

    def test_the_switch_and_the_port_persist(self, tmp_path):
        config = Config(data_dir=tmp_path)
        config.upgrade_enabled = False
        config.upgrade_listen_port = 40000
        reloaded = Config(data_dir=tmp_path)
        assert reloaded.upgrade_enabled is False
        assert reloaded.upgrade_listen_port == 40000

    def test_a_port_outside_the_range_is_refused(self, tmp_path):
        config = Config(data_dir=tmp_path)
        for bad in (-1, 65536):
            with pytest.raises(ValueError):
                config.upgrade_listen_port = bad
        assert config.upgrade_listen_port == UPGRADE_DEFAULT_PORT

    def test_zero_asks_the_kernel_for_a_port(self, tmp_path):
        config = Config(data_dir=tmp_path)
        config.upgrade_listen_port = 0
        assert config.upgrade_listen_port == 0


class TestTheAddressEchoSettings:
    """The "upgrade.stun" block: a disclosure, so it starts off."""

    def test_it_is_off_by_default_with_servers_ready_to_ask(self, tmp_path):
        config = Config(data_dir=tmp_path)
        assert config.stun_enabled is False
        assert config.stun_servers, "no default server to ask once it is on"
        assert all(":" in server for server in config.stun_servers)

    def test_the_switch_and_the_servers_persist(self, tmp_path):
        config = Config(data_dir=tmp_path)
        config.stun_enabled = True
        config.stun_servers = ["stun.example.com:3478", "203.0.113.7"]
        reloaded = Config(data_dir=tmp_path)
        assert reloaded.stun_enabled is True
        assert reloaded.stun_servers == ["stun.example.com:3478", "203.0.113.7"]

    def test_a_server_that_is_not_a_host_and_port_is_refused(self, tmp_path):
        config = Config(data_dir=tmp_path)
        defaults = config.stun_servers
        for bad in (["two words:3478"], ["host:70000"], ["[2001:db8::1"]):
            with pytest.raises(ValueError):
                config.stun_servers = bad
        assert config.stun_servers == defaults

    def test_a_list_longer_than_the_cap_is_refused(self, tmp_path):
        config = Config(data_dir=tmp_path)
        with pytest.raises(ValueError):
            config.stun_servers = [f"stun{n}.example.com:3478"
                                   for n in range(MAX_STUN_SERVERS + 1)]

    def test_blanks_are_dropped_rather_than_stored(self, tmp_path):
        config = Config(data_dir=tmp_path)
        config.stun_servers = [" stun.example.com:3478 ", "", "   "]
        assert config.stun_servers == ["stun.example.com:3478"]

    def test_an_empty_list_means_nothing_to_ask(self, tmp_path):
        config = Config(data_dir=tmp_path)
        config.stun_servers = []
        assert config.stun_servers == []
