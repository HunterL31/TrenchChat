"""
main_flutter.py's console-less startup.

The installed Windows and macOS builds are windowed: the process has no
console, so sys.stdout and sys.stderr are None. uvicorn's logging config
calls isatty() on stdout, and the app died there before the backend ever
started.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main_flutter


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(main_flutter, "_LOG_DIR", tmp_path / "profile")
    monkeypatch.setattr(main_flutter, "_LAUNCHER_LOG", tmp_path / "profile" / "launcher.log")
    return tmp_path / "profile"


def _no_console(monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)


def test_missing_streams_are_replaced_with_a_log_file(log_dir, monkeypatch):
    _no_console(monkeypatch)
    main_flutter.ensure_std_streams()

    assert sys.stdout is not None and sys.stderr is not None
    print("hello from a windowed build")
    assert sys.stdout.isatty() is False
    assert "hello from a windowed build" in (log_dir / "launcher.log").read_text()


def test_an_unwritable_log_dir_still_yields_streams(tmp_path, monkeypatch):
    blocked = tmp_path / "file"
    blocked.write_text("")
    monkeypatch.setattr(main_flutter, "_LOG_DIR", blocked / "profile")
    monkeypatch.setattr(main_flutter, "_LAUNCHER_LOG", blocked / "profile" / "launcher.log")
    _no_console(monkeypatch)

    main_flutter.ensure_std_streams()

    assert sys.stdout is not None and sys.stderr is not None
    print("discarded, not fatal")


def test_real_streams_are_left_alone(log_dir, monkeypatch):
    sentinel_out, sentinel_err = sys.stdout, sys.stderr

    main_flutter.ensure_std_streams()

    assert sys.stdout is sentinel_out
    assert sys.stderr is sentinel_err
    assert not log_dir.exists()


def test_uvicorn_logging_configures_without_a_console(log_dir, monkeypatch):
    """The exact failure users hit: uvicorn.Config() calling stdout.isatty()."""
    uvicorn = pytest.importorskip("uvicorn")
    _no_console(monkeypatch)
    main_flutter.ensure_std_streams()

    uvicorn.Config(lambda scope, receive, send: None,
                   host="127.0.0.1", port=8810, log_level="warning")
