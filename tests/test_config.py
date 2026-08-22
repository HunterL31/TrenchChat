"""
Tests for trenchchat.config.Config.

Covers defensive on-disk value handling (wrong-typed nested values must fall
back to defaults rather than crash on property access) and setter validation
for voice bitrate, propagation storage limit, and UI theme size/count caps.
"""

import json

import pytest

from trenchchat.config import (
    Config,
    MAX_THEME_BYTES,
    MAX_THEME_LIBRARY_ENTRIES,
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
