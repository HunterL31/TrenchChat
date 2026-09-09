"""
main_flutter.py's console-less startup, its --version flag, and the client
window it supervises.

The installed Windows and macOS builds are windowed: the process has no
console, so sys.stdout and sys.stderr are None. uvicorn's logging config
calls isatty() on stdout, and the app died there before the backend ever
started.

Closing that window no longer ends the run -- the node stays up in the tray
-- so the launcher has to notice the close, report it once, and be able to
open a window again against the backend that is still running.

A windowed build fails quietly in another way too: the server it starts has
to answer, not merely bind. An HTTP implementation that imports but cannot
parse leaves the port accepting connections it never replies to.
"""

import sys
import threading
import time
from pathlib import Path
from types import ModuleType

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main_flutter
from trenchchat.version import VERSION_ENV_VAR, reset_version_cache


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


def test_the_server_never_picks_the_httptools_parser(monkeypatch):
    """An httptools that imports without its compiled parser must not be used.

    A frozen build can carry exactly that. uvicorn's "auto" prefers httptools
    whenever it imports, then raises in its protocol factory for every
    connection: the port accepts and answers nothing, and the client waits on
    its loading state with no error to show.
    """
    pytest.importorskip("uvicorn")
    from uvicorn.protocols.http.h11_impl import H11Protocol

    monkeypatch.setitem(sys.modules, "httptools", ModuleType("httptools"))
    monkeypatch.delitem(sys.modules, "uvicorn.protocols.http.auto", raising=False)

    config = main_flutter._server_config(lambda scope, receive, send: None, 8810)
    config.load()

    assert config.http_protocol_class is H11Protocol


def test_version_flag_prints_the_build_and_starts_nothing(monkeypatch, capsys):
    """Support's first question, answerable without launching the app."""
    monkeypatch.setenv(VERSION_ENV_VAR, "4.2.0")
    reset_version_cache()
    monkeypatch.setattr(sys, "argv", ["main_flutter.py", "--version"])

    main_flutter.main()

    assert capsys.readouterr().out.strip() == "4.2.0"
    reset_version_cache()


class FakeProc:
    """A client window the test can close on its own terms."""

    def __init__(self, cmd, env=None):
        self.cmd = cmd
        self.env = env or {}
        self.terminated = False
        self._exited = threading.Event()

    def wait(self):
        self._exited.wait(WATCHER_TIMEOUT_SECS)

    def poll(self):
        return 0 if self._exited.is_set() else None

    def terminate(self):
        self.terminated = True
        self._exited.set()

    def exit(self):
        self._exited.set()


WATCHER_TIMEOUT_SECS = 5
BINARY = Path("/opt/trenchchat/flutter_ui")
URL = "http://127.0.0.1:8810"
TOKEN = "launcher-token-not-a-real-one"


@pytest.fixture
def spawned(monkeypatch):
    """Every client process the launcher starts, in order."""
    procs = []

    def fake_popen(cmd, env=None):
        procs.append(FakeProc(cmd, env))
        return procs[-1]

    monkeypatch.setattr(main_flutter.subprocess, "Popen", fake_popen)
    return procs


@pytest.fixture
def closes():
    """Records each time the launcher is told the window went away."""
    return []


def _window(spawned_binary=BINARY, on_closed=lambda: None):
    return main_flutter.ClientWindow(URL, TOKEN, spawned_binary, on_closed)


def test_desktop_client_is_told_where_the_backend_is(spawned):
    _window().open()

    assert spawned[0].cmd == [str(BINARY)]
    assert spawned[0].env["TC_API_URL"] == URL
    assert spawned[0].env["TC_API_TOKEN"] == TOKEN


def test_closing_the_window_is_reported_once(spawned, closes):
    window = _window(on_closed=lambda: closes.append(1))
    window.open()

    spawned[0].exit()
    deadline = time.time() + WATCHER_TIMEOUT_SECS
    while not closes and time.time() < deadline:
        time.sleep(0.01)

    assert closes == [1]


def test_a_closed_window_opens_again(spawned):
    """What the tray's Open does: the same backend, a new window."""
    window = _window()
    window.open()
    spawned[0].exit()

    window.open()

    assert len(spawned) == 2
    assert spawned[1].env["TC_API_TOKEN"] == TOKEN


def test_an_open_window_is_not_opened_twice(spawned):
    window = _window()
    window.open()

    window.open()

    assert len(spawned) == 1


def test_quitting_takes_the_window_down_with_it(spawned):
    window = _window()
    window.open()

    window.close()

    assert spawned[0].terminated


def test_a_browser_client_is_opened_at_a_url_carrying_the_token(monkeypatch):
    opened = []
    monkeypatch.setattr(main_flutter.webbrowser, "open", opened.append)
    window = _window(spawned_binary=None)

    window.open()
    window.open()

    assert opened == [f"{URL}/?token={TOKEN}"] * 2
