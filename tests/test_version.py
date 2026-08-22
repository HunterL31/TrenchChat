"""
Tests for trenchchat.version.

The point of the module is that an installer which rolls a user back, or
swaps one same-version build for another, is distinguishable from an
ordinary restart. So the comparison rules and the on-disk record are both
under test, including the profile that predates version tracking entirely.
"""

import json
import sys

import pytest

from trenchchat.version import (
    BUNDLED_VERSION_FILE,
    CHANGE_DOWNGRADE,
    CHANGE_FIRST_RUN,
    CHANGE_SAME,
    CHANGE_SIDEGRADE,
    CHANGE_UNKNOWN,
    CHANGE_UPGRADE,
    MAX_HISTORY_ENTRIES,
    SOURCE_BUILD_TAG,
    UNKNOWN_VERSION,
    VERSION_ENV_VAR,
    VERSION_FILE_NAME,
    app_version,
    classify_change,
    compare_versions,
    parse_version,
    read_install_record,
    record_launch,
    reset_version_cache,
)


@pytest.fixture(autouse=True)
def clear_version_cache():
    reset_version_cache()
    yield
    reset_version_cache()


def _record(data_dir):
    return json.loads((data_dir / VERSION_FILE_NAME).read_text())


class TestParsing:
    def test_plain_semver(self):
        assert parse_version("1.4.2") == (1, 4, 2, (), "")

    def test_prerelease_and_build(self):
        assert parse_version("1.4.2-rc.1+ci.7") == (1, 4, 2, ("rc", "1"), "ci.7")

    @pytest.mark.parametrize("text", ["", "1.4", "v1.4.2", "1.4.2.3", "next"])
    def test_rejects_non_semver(self, text):
        assert parse_version(text) is None


class TestComparison:
    def test_orders_by_numeric_core(self):
        assert compare_versions("1.10.0", "1.9.9") > 0
        assert compare_versions("2.0.0", "10.0.0") < 0

    def test_prerelease_sorts_below_its_release(self):
        assert compare_versions("1.4.0-rc.1", "1.4.0") < 0
        assert compare_versions("1.4.0-rc.2", "1.4.0-rc.1") > 0
        assert compare_versions("1.4.0-alpha", "1.4.0-alpha.1") < 0

    def test_build_metadata_carries_no_precedence(self):
        assert compare_versions("1.4.0+ci.9", "1.4.0+local") == 0

    def test_unparseable_is_incomparable(self):
        assert compare_versions("nightly", "1.4.0") is None
        assert compare_versions("1.4.0", "") is None


class TestClassification:
    def test_no_record_and_no_profile_is_a_first_run(self):
        assert classify_change(None, "1.4.0", profile_exists=False) == CHANGE_FIRST_RUN

    def test_no_record_but_an_existing_profile_is_unknown(self):
        assert classify_change(None, "1.4.0", profile_exists=True) == CHANGE_UNKNOWN

    def test_same_string_is_a_restart(self):
        assert classify_change("1.4.0", "1.4.0") == CHANGE_SAME

    def test_newer_is_an_upgrade(self):
        assert classify_change("1.4.0", "1.5.0") == CHANGE_UPGRADE

    def test_older_is_a_downgrade(self):
        assert classify_change("1.5.0", "1.4.0") == CHANGE_DOWNGRADE

    def test_same_precedence_different_build_is_a_sidegrade(self):
        assert classify_change("1.4.0+ci.9", "1.4.0") == CHANGE_SIDEGRADE
        assert classify_change("1.4.0", "1.4.0+source") == CHANGE_SIDEGRADE

    def test_incomparable_versions_are_unknown(self):
        assert classify_change("nightly", "1.4.0") == CHANGE_UNKNOWN


class TestResolution:
    def test_env_override_wins(self, monkeypatch):
        monkeypatch.setenv(VERSION_ENV_VAR, "9.9.9")
        assert app_version() == "9.9.9"

    def test_source_checkout_is_tagged_as_such(self, monkeypatch):
        monkeypatch.delenv(VERSION_ENV_VAR, raising=False)
        version = app_version()
        assert version.endswith(SOURCE_BUILD_TAG)
        assert parse_version(version) is not None

    def test_a_frozen_bundle_reports_its_stamp(self, tmp_path, monkeypatch):
        monkeypatch.delenv(VERSION_ENV_VAR, raising=False)
        (tmp_path / BUNDLED_VERSION_FILE).write_text("2.3.4\n")
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

        assert app_version() == "2.3.4"

    def test_a_bundle_with_no_stamp_falls_back_rather_than_failing(
            self, tmp_path, monkeypatch):
        monkeypatch.delenv(VERSION_ENV_VAR, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

        assert parse_version(app_version()) is not None

    def test_result_is_cached_until_reset(self, monkeypatch):
        monkeypatch.setenv(VERSION_ENV_VAR, "1.0.0")
        assert app_version() == "1.0.0"
        monkeypatch.setenv(VERSION_ENV_VAR, "2.0.0")
        assert app_version() == "1.0.0"
        reset_version_cache()
        assert app_version() == "2.0.0"


class TestRecording:
    def test_first_run_writes_the_record(self, tmp_path):
        state = record_launch(tmp_path, version="1.4.0")

        assert state.transition == CHANGE_FIRST_RUN
        assert state.previous is None
        assert _record(tmp_path)["version"] == "1.4.0"

    def test_an_untracked_profile_is_not_a_first_run(self, tmp_path):
        (tmp_path / "identity").write_bytes(b"")

        state = record_launch(tmp_path, version="1.4.0")

        assert state.transition == CHANGE_UNKNOWN

    def test_upgrade_is_detected_and_stored(self, tmp_path):
        record_launch(tmp_path, version="1.4.0")

        state = record_launch(tmp_path, version="1.5.0")

        assert state.transition == CHANGE_UPGRADE
        assert state.previous == "1.4.0"
        assert _record(tmp_path)["version"] == "1.5.0"

    def test_downgrade_is_detected(self, tmp_path):
        record_launch(tmp_path, version="1.5.0")

        state = record_launch(tmp_path, version="1.4.0")

        assert state.transition == CHANGE_DOWNGRADE
        assert state.is_downgrade
        assert state.previous == "1.5.0"

    def test_sidegrade_is_detected(self, tmp_path):
        record_launch(tmp_path, version="1.4.0+ci.9")

        state = record_launch(tmp_path, version="1.4.0+local")

        assert state.transition == CHANGE_SIDEGRADE
        assert state.is_sidegrade

    def test_a_restart_leaves_the_record_untouched(self, tmp_path):
        record_launch(tmp_path, version="1.4.0")
        before = (tmp_path / VERSION_FILE_NAME).read_text()

        state = record_launch(tmp_path, version="1.4.0")

        assert state.transition == CHANGE_SAME
        assert (tmp_path / VERSION_FILE_NAME).read_text() == before

    def test_first_seen_survives_later_changes(self, tmp_path):
        first = record_launch(tmp_path, version="1.4.0")

        later = record_launch(tmp_path, version="1.5.0")

        assert later.first_seen == first.first_seen
        assert later.changed_at >= first.changed_at

    def test_history_records_every_change_and_stays_bounded(self, tmp_path):
        for minor in range(MAX_HISTORY_ENTRIES + 5):
            record_launch(tmp_path, version=f"1.{minor}.0")

        history = _record(tmp_path)["history"]
        assert len(history) == MAX_HISTORY_ENTRIES
        assert history[-1]["version"] == f"1.{MAX_HISTORY_ENTRIES + 4}.0"
        assert history[-1]["transition"] == CHANGE_UPGRADE

    def test_a_corrupt_record_is_replaced_not_fatal(self, tmp_path):
        (tmp_path / VERSION_FILE_NAME).write_text("{not json")

        state = record_launch(tmp_path, version="1.4.0")

        assert state.previous is None
        assert _record(tmp_path)["version"] == "1.4.0"

    def test_read_install_record_returns_none_when_absent(self, tmp_path):
        assert read_install_record(tmp_path) is None

    def test_an_unwritable_profile_still_yields_a_state(self, tmp_path):
        blocked = tmp_path / "file"
        blocked.write_text("")

        state = record_launch(blocked / "profile", version="1.4.0")

        assert state.version == "1.4.0"
        assert state.transition == CHANGE_FIRST_RUN

    def test_falls_back_to_the_resolved_version(self, tmp_path, monkeypatch):
        monkeypatch.setenv(VERSION_ENV_VAR, "3.2.1")

        state = record_launch(tmp_path)

        assert state.version == "3.2.1"
        assert state.as_dict()["version"] == "3.2.1"

    def test_unknown_version_is_still_a_usable_default(self):
        assert parse_version(UNKNOWN_VERSION) is not None
