"""
Handing a second launch over to the instance already in the tray.

Closing the window no longer closes the node, so the next double-click
arrives while one is running. It has to reach that instance rather than
start a second backend over the same identity and database -- and it has to
start normally when the record it finds is stale, or a crash would leave
the app unable to open at all.
"""

import json
import os
import stat
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from trenchchat import single_instance
from trenchchat.single_instance import TOKEN_HEADER

TOKEN = "launcher-token-not-a-real-one"


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.server.requests.append((self.path, self.headers.get(TOKEN_HEADER)))
        status = 200 if self.headers.get(TOKEN_HEADER) == TOKEN else 401
        self.send_response(status)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def instance():
    """A stand-in for a launcher already running on this machine."""
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.url = f"http://127.0.0.1:{server.server_address[1]}"
    yield server
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


def test_a_running_instance_is_asked_to_open_its_window(tmp_path, instance):
    single_instance.publish(instance.url, TOKEN, data_dir=tmp_path)

    assert single_instance.hand_off(data_dir=tmp_path) is True
    assert instance.requests == [(single_instance.OPEN_UI_PATH, TOKEN)]


def test_no_record_means_nothing_is_running(tmp_path):
    assert single_instance.hand_off(data_dir=tmp_path) is False


def test_a_stale_record_starts_normally(tmp_path, instance):
    """The port a crashed instance left behind answers nothing."""
    single_instance.publish(instance.url, TOKEN, data_dir=tmp_path)
    instance.shutdown()
    instance.server_close()

    assert single_instance.hand_off(data_dir=tmp_path) is False


def test_a_corrupt_record_starts_normally(tmp_path):
    single_instance.record_path(tmp_path).write_text("not json")

    assert single_instance.hand_off(data_dir=tmp_path) is False


def test_a_refused_handoff_starts_normally(tmp_path, instance):
    """Another program on that port is not this user's launcher."""
    single_instance.publish(instance.url, "the-wrong-token", data_dir=tmp_path)

    assert single_instance.hand_off(data_dir=tmp_path) is False


def test_the_record_holds_the_api_and_is_owner_only(tmp_path):
    single_instance.publish("http://127.0.0.1:8810", TOKEN, data_dir=tmp_path)
    path = single_instance.record_path(tmp_path)

    assert json.loads(path.read_text()) == {"url": "http://127.0.0.1:8810",
                                            "token": TOKEN}
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_clearing_withdraws_the_record(tmp_path):
    single_instance.publish("http://127.0.0.1:8810", TOKEN, data_dir=tmp_path)

    single_instance.clear(data_dir=tmp_path)
    single_instance.clear(data_dir=tmp_path)

    assert not single_instance.record_path(tmp_path).exists()
