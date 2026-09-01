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
from trenchchat.network import node_transport
from trenchchat.network.node_transport import (
    FETCH_BAD_PATH, FETCH_BAD_RESPONSE, FETCH_SEND_FAILED, FETCH_UNREACHABLE,
    MAX_SERVED_RESPONSE_BYTES, NODE_REDIAL_BACKOFF, NODE_SERVE_RATE_LIMIT,
    NodeTransportBase, RNSNodeTransport, is_valid_request_path,
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
        lambda fid, ok, payload, reason, name=None:
        results.append((fid, ok, reason)))
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


def test_exhausted_ladder_keeps_fetch_until_its_own_timeout(
        transport, monkeypatch):
    """Cold-path resolution can outlast the backoff ladder while the node is
    up, so exhaustion must not fail queued fetches early -- only the fetch's
    own deadline does."""
    results = _collect_results(transport)
    monkeypatch.setattr(RNS.Identity, "recall", staticmethod(lambda h: None))
    monkeypatch.setattr(RNS.Transport, "request_path",
                        staticmethod(lambda h, *a, **k: None))

    node_hex = "cd" * 16
    transport.fetch("f1", node_hex, "/page/index.mu", max_size=1024)
    for _ in range(len(NODE_REDIAL_BACKOFF) + 2):
        conn = transport._conns[node_hex]
        conn.next_dial_at = 0.0
        transport.tick()
    assert results == []
    conn = transport._conns[node_hex]
    assert conn.exhausted and conn.queued

    # Reaching the fetch's own deadline fails it as unreachable.
    conn.queued[0].created_at = 0.0
    transport.tick()
    assert results == [("f1", False, FETCH_UNREACHABLE)]


def test_fetch_queued_past_deadline_is_unreachable(transport, monkeypatch):
    """A fetch that never got a link reports unreachable, not timeout --
    timeout is reserved for mid-transfer stalls."""
    results = _collect_results(transport)
    monkeypatch.setattr(RNS.Identity, "recall", staticmethod(lambda h: None))
    monkeypatch.setattr(RNS.Transport, "request_path",
                        staticmethod(lambda h, *a, **k: None))

    node_hex = "ef" * 16
    transport.fetch("f1", node_hex, "/page/index.mu", max_size=1024,
                    timeout=0.0)
    transport.tick()
    assert ("f1", False, FETCH_UNREACHABLE) in results


class _FakeReceipt:
    """Stands in for an RNS RequestReceipt with a given response shape."""

    def __init__(self, response, metadata=None):
        self.response = response
        self.metadata = metadata


def _capture_result(transport, receipt):
    """Register a fetch against a receipt and run the response callback."""
    results = _collect_results(transport)
    fetch = node_transport._Fetch("f1", "ab" * 16, "/file/x.bin",
                                  max_size=1024, timeout=10)
    transport._active[id(receipt)] = fetch
    transport._on_response(receipt)
    return results


def test_page_response_of_plain_bytes_is_delivered(transport):
    receipt = _FakeReceipt(b"# hello")
    assert _capture_result(transport, receipt) == [("f1", True, None)]


def _collect_payloads(transport):
    delivered = []
    transport.set_fetch_result_callback(
        lambda fid, ok, payload, reason, name=None:
        delivered.append((ok, payload, name)))
    return delivered


def test_file_response_is_read_out_of_its_handle(transport, tmp_path):
    """nomadnet answers a /file/ request with an open handle plus metadata,
    and RNS deletes the temp file the moment this callback returns."""
    blob = tmp_path / "payload.bin"
    blob.write_bytes(b"file bytes")
    delivered = _collect_payloads(transport)
    fetch = node_transport._Fetch("f1", "ab" * 16, "/file/x.bin",
                                  max_size=1024, timeout=10)
    with blob.open("rb") as handle:
        receipt = _FakeReceipt(handle, metadata={"name": b"payload.bin"})
        transport._active[id(receipt)] = fetch
        transport._on_response(receipt)
    assert delivered == [(True, b"file bytes", "payload.bin")]


def test_legacy_name_and_data_file_response_is_delivered(transport):
    """Older nomadnet nodes answer with [name, data] instead of a handle."""
    delivered = _collect_payloads(transport)
    fetch = node_transport._Fetch("f1", "ab" * 16, "/file/x.bin",
                                  max_size=1024, timeout=10)
    receipt = _FakeReceipt(["payload.bin", b"file bytes"])
    transport._active[id(receipt)] = fetch
    transport._on_response(receipt)
    assert delivered == [(True, b"file bytes", "payload.bin")]


@pytest.mark.parametrize("given,expected", [
    (b"notes.txt", "notes.txt"),
    ("../../etc/passwd", "passwd"),
    (rb"C:\\windows\\system32\\evil.exe", "evil.exe"),
    ('re"port.txt', "report.txt"),
    ("", None),
    ("...", None),
    (12345, None),
])
def test_a_node_supplied_name_is_reduced_to_a_bare_basename(given, expected):
    """The name comes from the remote and ends up in a download header."""
    assert node_transport._clean_filename(given) == expected


def test_response_of_an_unusable_shape_is_reported(transport):
    receipt = _FakeReceipt({"not": "a payload"})
    assert _capture_result(transport, receipt) == [
        ("f1", False, FETCH_BAD_RESPONSE)]


def test_served_file_carries_a_handle_and_its_name(transport, tmp_path):
    """A nomadnet browser handed raw bytes reads response[0] as a filename
    and drops the download; it needs the handle-plus-name shape."""
    blob = tmp_path / "notes.txt"
    blob.write_bytes(b"served bytes")
    transport.start_hosting("Node", {"/file/notes.txt": lambda: blob})

    response = transport._serve("/file/notes.txt", None, b"rid", b"lid",
                                None, time.time())

    assert isinstance(response, list) and len(response) == 2
    handle, metadata = response
    assert metadata == {"name": b"notes.txt"}
    with handle:
        assert handle.read() == b"served bytes"


def test_served_page_stays_plain_bytes(transport, tmp_path):
    transport.start_hosting("Node", {"/page/index.mu": lambda: b"# page"})
    response = transport._serve("/page/index.mu", None, b"rid", b"lid",
                                None, time.time())
    assert response == b"# page"


def test_an_oversized_file_is_refused_without_reading_it(transport, tmp_path):
    blob = tmp_path / "big.bin"
    blob.write_bytes(b"x" * 16)
    transport.start_hosting("Node", {"/file/big.bin": lambda: blob})
    monkey = MAX_SERVED_RESPONSE_BYTES
    assert monkey > 16   # sanity: the cap is what refuses, not the size
    node_transport.MAX_SERVED_RESPONSE_BYTES = 4
    try:
        assert transport._serve("/file/big.bin", None, b"rid", b"lid", None,
                                time.time()) is None
    finally:
        node_transport.MAX_SERVED_RESPONSE_BYTES = monkey


class _DeadLink:
    """A link the remote has already dropped: it still looks established
    here, and every request on it fails to send."""

    def __init__(self):
        self.torn_down = False

    def request(self, *args, **kwargs):
        return False

    def teardown(self):
        self.torn_down = True


def _linked_conn(transport, node_hex, link):
    conn = node_transport._NodeConn(node_hex)
    conn.state = node_transport._LINKED
    conn.link = link
    transport._conns[node_hex] = conn
    transport._by_link[id(link)] = node_hex
    return conn


def test_dead_link_redials_instead_of_failing(transport, monkeypatch):
    results = _collect_results(transport)
    requested = []
    monkeypatch.setattr(RNS.Identity, "recall", staticmethod(lambda h: None))
    monkeypatch.setattr(RNS.Transport, "request_path",
                        staticmethod(lambda h, *a, **k: requested.append(h)))
    node_hex = "ab" * 16
    link = _DeadLink()
    _linked_conn(transport, node_hex, link)

    transport.fetch("f1", node_hex, "/page/index.mu", max_size=1024)

    assert results == []
    assert link.torn_down
    assert requested == [bytes.fromhex(node_hex)]
    assert [f.fetch_id for f in transport._conns[node_hex].queued] == ["f1"]


def test_a_second_dead_link_fails_the_fetch(transport, monkeypatch):
    results = _collect_results(transport)
    monkeypatch.setattr(RNS.Identity, "recall", staticmethod(lambda h: None))
    monkeypatch.setattr(RNS.Transport, "request_path",
                        staticmethod(lambda h, *a, **k: None))
    node_hex = "cd" * 16
    _linked_conn(transport, node_hex, _DeadLink())
    transport.fetch("f1", node_hex, "/page/index.mu", max_size=1024)

    conn = transport._conns[node_hex]
    second = _DeadLink()
    conn.state = node_transport._LINKED
    conn.link = second
    transport._by_link[id(second)] = node_hex
    transport._flush_queued(node_hex)

    assert results == [("f1", False, FETCH_SEND_FAILED)]


def test_an_orderly_close_clears_the_backoff_ladder(transport):
    node_hex = "ef" * 16
    link = _DeadLink()
    conn = _linked_conn(transport, node_hex, link)
    conn.dial_attempts = 3
    conn.next_dial_at = time.time() + 999

    transport._on_link_closed(link)

    assert conn.state == node_transport._IDLE
    assert conn.dial_attempts == 0
    assert conn.next_dial_at == 0.0


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
