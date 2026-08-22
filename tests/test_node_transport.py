"""
Unit tests for the nomad node transport: request path validation, the
serve-side registry and its caps, the fetch dial ladder, and the node
announce handler.
"""

import time

import pytest
import RNS

from trenchchat.config import Config
from trenchchat.core.identity import Identity
from trenchchat.network.announce import NodeAnnounceHandler
from trenchchat.network.node_transport import (
    FETCH_BAD_PATH, FETCH_UNREACHABLE, MAX_SERVED_RESPONSE_BYTES,
    NODE_REDIAL_BACKOFF, NODE_SERVE_RATE_LIMIT, NodeTransportBase,
    RNSNodeTransport, is_valid_request_path,
)


# ---------------------------------------------------------------------------
# is_valid_request_path
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/page/index.mu",
    "/page/sub/dir/page.mu",
    "/file/archive.zip",
    "/page/with spaces.mu",
])
def test_valid_request_paths(path):
    assert is_valid_request_path(path)


@pytest.mark.parametrize("path", [
    "",
    None,
    b"/page/index.mu",
    "/etc/passwd",
    "page/index.mu",
    "/page/../../etc/passwd",
    "/file/..",
    "/page/a\\b.mu",
    "/page/a\0b.mu",
    "/page/a\nb.mu",
    "/page/" + "a" * 300,
])
def test_invalid_request_paths(path):
    assert not is_valid_request_path(path)


# ---------------------------------------------------------------------------
# Base class callback safety
# ---------------------------------------------------------------------------

def test_base_callbacks_swallow_errors():
    base = NodeTransportBase()

    def bad_cb(*args):
        raise RuntimeError("boom")

    base.set_fetch_result_callback(bad_cb)
    base.set_fetch_progress_callback(bad_cb)
    base._notify_result("f1", True, b"x", None)
    base._notify_progress("f1", 0.5)


def test_base_callbacks_none_is_noop():
    base = NodeTransportBase()
    base._notify_result("f1", False, None, "timeout")
    base._notify_progress("f1", 1.0)


# ---------------------------------------------------------------------------
# Serve side
# ---------------------------------------------------------------------------

@pytest.fixture
def transport(rns_instance, tmp_path):
    config = Config(data_dir=tmp_path)
    identity = Identity(config, identity_path=tmp_path / "identity")
    return RNSNodeTransport(identity)


def test_start_hosting_registers_valid_paths_only(transport):
    providers = {
        "/page/index.mu": lambda: b"hello",
        "/page/../evil.mu": lambda: b"evil",
        "/etc/passwd": lambda: b"evil",
    }
    transport.start_hosting("Test Node", providers)
    assert set(transport._providers) == {"/page/index.mu"}
    assert transport._serve("/page/index.mu", None, b"r", b"l1", None,
                            time.time()) == b"hello"


def test_serve_unknown_path_returns_none(transport):
    transport.start_hosting("Test Node", {"/page/index.mu": lambda: b"hi"})
    assert transport._serve("/page/other.mu", None, b"r", b"l1", None,
                            time.time()) is None


def test_serve_oversized_response_refused(transport):
    big = b"x" * (MAX_SERVED_RESPONSE_BYTES + 1)
    transport.start_hosting("Test Node", {"/file/big": lambda: big})
    assert transport._serve("/file/big", None, b"r", b"l1", None,
                            time.time()) is None


def test_serve_provider_error_returns_none(transport):
    def boom():
        raise OSError("gone")

    transport.start_hosting("Test Node", {"/page/index.mu": boom})
    assert transport._serve("/page/index.mu", None, b"r", b"l1", None,
                            time.time()) is None


def test_serve_rate_limit_per_link(transport):
    transport.start_hosting("Test Node", {"/page/index.mu": lambda: b"hi"})
    now = time.time()
    served = sum(
        1 for _ in range(NODE_SERVE_RATE_LIMIT * 2)
        if transport._serve("/page/index.mu", None, b"r", b"l1", None,
                            now) is not None
    )
    assert served == NODE_SERVE_RATE_LIMIT
    # A different link is unaffected.
    assert transport._serve("/page/index.mu", None, b"r", b"l2", None,
                            now) == b"hi"


def test_update_hosting_swaps_registered_paths(transport):
    transport.start_hosting("Test Node", {"/page/a.mu": lambda: b"a"})
    transport.update_hosting("Test Node", {"/page/b.mu": lambda: b"b"})
    assert transport._serve("/page/a.mu", None, b"r", b"l1", None,
                            time.time()) is None
    assert transport._serve("/page/b.mu", None, b"r", b"l2", None,
                            time.time()) == b"b"


def test_stop_hosting_clears_providers(transport):
    transport.start_hosting("Test Node", {"/page/index.mu": lambda: b"hi"})
    transport.stop_hosting()
    assert transport._providers == {}
    assert transport._serve("/page/index.mu", None, b"r", b"l1", None,
                            time.time()) is None


# ---------------------------------------------------------------------------
# Fetch dial ladder
# ---------------------------------------------------------------------------

def _collect_results(transport):
    results = []
    transport.set_fetch_result_callback(
        lambda fid, ok, payload, reason: results.append((fid, ok, reason)))
    return results


def test_fetch_invalid_path_fails_immediately(transport):
    results = _collect_results(transport)
    transport.fetch("f1", "aa" * 16, "/nope", max_size=1024)
    assert results == [("f1", False, FETCH_BAD_PATH)]


def test_fetch_unknown_identity_requests_path_and_backs_off(
        transport, monkeypatch):
    results = _collect_results(transport)
    requested = []
    monkeypatch.setattr(RNS.Identity, "recall", staticmethod(lambda h: None))
    monkeypatch.setattr(RNS.Transport, "request_path",
                        staticmethod(lambda h, *a, **k: requested.append(h)))

    node_hex = "ab" * 16
    transport.fetch("f1", node_hex, "/page/index.mu", max_size=1024)
    assert requested == [bytes.fromhex(node_hex)]
    assert results == []
    conn = transport._conns[node_hex]
    assert conn.dial_attempts == 1
    assert conn.next_dial_at > time.time()


def test_fetch_exhausted_dials_fail_unreachable(transport, monkeypatch):
    results = _collect_results(transport)
    monkeypatch.setattr(RNS.Identity, "recall", staticmethod(lambda h: None))
    monkeypatch.setattr(RNS.Transport, "request_path",
                        staticmethod(lambda h, *a, **k: None))

    node_hex = "cd" * 16
    transport.fetch("f1", node_hex, "/page/index.mu", max_size=1024)
    for _ in range(len(NODE_REDIAL_BACKOFF)):
        conn = transport._conns.get(node_hex)
        if conn is None:
            break
        conn.next_dial_at = 0.0
        transport.tick()
    assert results == [("f1", False, FETCH_UNREACHABLE)]
    assert node_hex not in transport._conns


def test_fetch_queued_times_out(transport, monkeypatch):
    results = _collect_results(transport)
    monkeypatch.setattr(RNS.Identity, "recall", staticmethod(lambda h: None))
    monkeypatch.setattr(RNS.Transport, "request_path",
                        staticmethod(lambda h, *a, **k: None))

    node_hex = "ef" * 16
    transport.fetch("f1", node_hex, "/page/index.mu", max_size=1024,
                    timeout=0.0)
    transport.tick()
    assert ("f1", False, "timeout") in results


# ---------------------------------------------------------------------------
# NodeAnnounceHandler
# ---------------------------------------------------------------------------

def test_node_announce_handler_aspect():
    assert NodeAnnounceHandler.aspect_filter == "nomadnetwork.node"


def test_node_announce_handler_fires_with_name(transport):
    seen = []
    handler = NodeAnnounceHandler(
        lambda node_hex, name, iface: seen.append((node_hex, name)))
    dest_hash = bytes(range(16))
    handler.received_announce(
        dest_hash, transport._identity.rns_identity, b"My Node", b"ph")
    assert seen == [(dest_hash.hex(), "My Node")]


def test_node_announce_handler_sanitizes_app_data(transport):
    seen = []
    handler = NodeAnnounceHandler(
        lambda node_hex, name, iface: seen.append(name))
    handler.received_announce(
        b"\x01" * 16, transport._identity.rns_identity,
        b"\x00\x1bBad\xffName" + b"x" * 200, b"ph")
    assert len(seen) == 1
    name = seen[0]
    assert "\x00" not in name and "\x1b" not in name
    assert len(name) <= 64


def test_node_announce_handler_ignores_unknown_identity():
    seen = []
    handler = NodeAnnounceHandler(
        lambda *args: seen.append(args))
    handler.received_announce(b"\x02" * 16, None, b"name", b"ph")
    assert seen == []


def test_node_announce_handler_callback_error_swallowed(transport):
    def bad_cb(*args):
        raise RuntimeError("boom")

    handler = NodeAnnounceHandler(bad_cb)
    handler.received_announce(
        b"\x03" * 16, transport._identity.rns_identity, b"name", b"ph")
