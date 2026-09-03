"""
Unit tests for the file plane: inbound request validation, the serve-side
refusals and their caps, the fetch dial ladder and stall timeout, and the
in-process fake the FileManager tests run against.
"""

import time

import pytest
import RNS

from trenchchat import APP_NAME, APP_ASPECT_FILES
from trenchchat.config import Config
from trenchchat.core.files import WINDOW_GROWTH_STREAK
from trenchchat.core.identity import Identity
from trenchchat.core.protocol import FILE_CHUNK_BYTES, MAX_SHARED_FILE_BYTES
from trenchchat.network import file_transport
from trenchchat.network.file_transport import (
    FETCH_REFUSED, FETCH_STALLED, FILE_REQUEST_MAX_CHUNKS, FILE_REQUEST_PATH,
    FILE_SERVE_RATE_LIMIT, FILE_SERVE_SETTLE_SECS, FILE_STALL_SECS,
    MAX_CHUNK_INDEX, MAX_CHUNK_LIST_BYTES, MAX_CONCURRENT_SERVES,
    RESPONSE_ENVELOPE_BYTES,
    R_COUNT, R_FILE_HASH, R_FIRST, R_WANT_LIST, FileTransportBase,
    RNSFileTransport, max_response_for, parse_file_request,
)
from trenchchat.network.link_client import (
    FETCH_BAD_PATH, FETCH_BAD_RESPONSE, FETCH_LINK_CLOSED, FETCH_TOO_LARGE,
    FETCH_UNREACHABLE, LinkConn, LinkFetch, _LINKED,
)
from tests.fake_file_transport import FakeFileRegistry, FakeFileTransport

HASH_A = "aa" * 32
HOLDER = "11" * 16
OTHER_HOLDER = "22" * 16


# ---------------------------------------------------------------------------
# Request parsing
# ---------------------------------------------------------------------------

def test_a_chunk_range_request_parses():
    data = {R_FILE_HASH: bytes.fromhex(HASH_A), R_FIRST: 3, R_COUNT: 4}
    assert parse_file_request(data) == (HASH_A, 3, 4, False)


def test_a_chunk_list_request_parses():
    data = {R_FILE_HASH: bytes.fromhex(HASH_A), R_WANT_LIST: 1}
    assert parse_file_request(data) == (HASH_A, 0, 0, True)


def test_msgpack_byte_keys_parse_the_same():
    """LXMF is not in this path, but a peer chooses its own msgpack encoding
    and may hand over byte keys."""
    data = {b"h": bytes.fromhex(HASH_A), b"i": 0, b"n": 1}
    assert parse_file_request(data) == (HASH_A, 0, 1, False)


@pytest.mark.parametrize("data", [
    None,
    b"not a dict",
    {},
    {R_FILE_HASH: b"short", R_FIRST: 0, R_COUNT: 1},
    {R_FILE_HASH: HASH_A, R_FIRST: 0, R_COUNT: 1},           # hex, not bytes
    {R_FILE_HASH: bytes.fromhex(HASH_A), R_FIRST: -1, R_COUNT: 1},
    {R_FILE_HASH: bytes.fromhex(HASH_A), R_FIRST: 0, R_COUNT: 0},
    {R_FILE_HASH: bytes.fromhex(HASH_A), R_FIRST: 0,
     R_COUNT: FILE_REQUEST_MAX_CHUNKS + 1},
    {R_FILE_HASH: bytes.fromhex(HASH_A), R_FIRST: MAX_CHUNK_INDEX + 1,
     R_COUNT: 1},
    {R_FILE_HASH: bytes.fromhex(HASH_A), R_FIRST: 2 ** 60, R_COUNT: 1},
    {R_FILE_HASH: bytes.fromhex(HASH_A), R_FIRST: "0", R_COUNT: 1},
    {R_FILE_HASH: bytes.fromhex(HASH_A), R_FIRST: 0, R_COUNT: True},
    {R_FILE_HASH: bytes.fromhex(HASH_A), R_FIRST: 0},
])
def test_malformed_requests_are_refused(data):
    assert parse_file_request(data) is None


def test_max_response_leaves_room_for_the_msgpack_envelope():
    """RNS measures the packed [request_id, payload] against
    max_response_size, so a full range needs headroom or the requester
    rejects its own answer."""
    assert max_response_for(4) > 4 * FILE_CHUNK_BYTES
    assert max_response_for(4) < 5 * FILE_CHUNK_BYTES


def test_the_chunk_list_ceiling_covers_the_largest_file():
    chunks = -(-MAX_SHARED_FILE_BYTES // FILE_CHUNK_BYTES)
    assert MAX_CHUNK_LIST_BYTES >= chunks * 32 + RESPONSE_ENVELOPE_BYTES
    assert MAX_CHUNK_LIST_BYTES == (MAX_CHUNK_INDEX + 1) * 32 \
        + RESPONSE_ENVELOPE_BYTES


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

def test_base_callbacks_swallow_errors():
    base = FileTransportBase()

    def bad_cb(*args):
        raise RuntimeError("boom")

    base.set_result_callback(bad_cb)
    base.set_progress_callback(bad_cb)
    base.set_serve_callback(bad_cb)
    base._notify_result("f1", True, b"x", None)
    base._notify_progress("f1", 0.5)
    assert base._call_serve(HOLDER, HASH_A, 0, 1, False) is None


def test_base_callbacks_none_is_noop():
    base = FileTransportBase()
    base._notify_result("f1", False, None, FETCH_STALLED)
    base._notify_progress("f1", 1.0)
    assert base._call_serve(HOLDER, HASH_A, 0, 1, False) is None


def test_a_serve_callback_that_answers_with_the_wrong_type_is_dropped():
    base = FileTransportBase()
    base.set_serve_callback(lambda *args: "not bytes")
    assert base._call_serve(HOLDER, HASH_A, 0, 1, False) is None


# ---------------------------------------------------------------------------
# Serve side
# ---------------------------------------------------------------------------

@pytest.fixture
def transport(rns_instance, tmp_path):
    config = Config(data_dir=tmp_path)
    identity = Identity(config, identity_path=tmp_path / "identity")
    return RNSFileTransport(identity)


class _Requester:
    """Stands in for the RNS Identity a link identified with."""

    def __init__(self, hash_hex: str):
        self.hash = bytes.fromhex(hash_hex)


def _serving(transport, payload=b"chunk bytes"):
    """Serve the given payload and record what the handler was asked for."""
    seen = []

    def serve(requester, file_hash_hex, first, count, want_list):
        seen.append((requester, file_hash_hex, first, count, want_list))
        return payload

    transport.set_serve_callback(serve)
    transport.start_serving()
    return seen


def _request(first=0, count=1, file_hash=HASH_A):
    return {R_FILE_HASH: bytes.fromhex(file_hash), R_FIRST: first,
            R_COUNT: count}


def test_a_served_range_reaches_the_callback_with_the_requester(transport):
    seen = _serving(transport)
    response = transport._serve(FILE_REQUEST_PATH, _request(2, 4), b"r",
                                b"l1", _Requester(HOLDER), time.time())
    assert response == b"chunk bytes"
    assert seen == [(HOLDER, HASH_A, 2, 4, False)]


def test_a_chunk_list_request_reaches_the_callback(transport):
    seen = _serving(transport, payload=b"h" * 32)
    data = {R_FILE_HASH: bytes.fromhex(HASH_A), R_WANT_LIST: 1}
    assert transport._serve(FILE_REQUEST_PATH, data, b"r", b"l1",
                            _Requester(HOLDER), time.time()) == b"h" * 32
    assert seen == [(HOLDER, HASH_A, 0, 0, True)]


def test_an_unidentified_link_reaches_the_callback_as_none(transport):
    """The membership check lives in the core layer, so the transport hands
    it the absence of an identity rather than deciding on its own."""
    seen = _serving(transport)
    transport._serve(FILE_REQUEST_PATH, _request(), b"r", b"l1", None,
                     time.time())
    assert seen == [(None, HASH_A, 0, 1, False)]


def test_a_refusing_callback_answers_with_silence(transport):
    transport.set_serve_callback(lambda *args: None)
    transport.start_serving()
    assert transport._serve(FILE_REQUEST_PATH, _request(), b"r", b"l1",
                            _Requester(HOLDER), time.time()) is None


def test_no_serve_callback_means_nothing_is_served(transport):
    transport.start_serving()
    assert transport._serve(FILE_REQUEST_PATH, _request(), b"r", b"l1",
                            _Requester(HOLDER), time.time()) is None


def test_a_serve_callback_that_raises_answers_with_silence(transport):
    def boom(*args):
        raise OSError("gone")

    transport.set_serve_callback(boom)
    transport.start_serving()
    assert transport._serve(FILE_REQUEST_PATH, _request(), b"r", b"l1",
                            _Requester(HOLDER), time.time()) is None


@pytest.mark.parametrize("data", [
    None,
    {R_FILE_HASH: b"short", R_FIRST: 0, R_COUNT: 1},
    {R_FILE_HASH: bytes.fromhex(HASH_A), R_FIRST: 0,
     R_COUNT: FILE_REQUEST_MAX_CHUNKS + 1},
])
def test_a_malformed_request_never_reaches_the_callback(transport, data):
    seen = _serving(transport)
    assert transport._serve(FILE_REQUEST_PATH, data, b"r", b"l1",
                            _Requester(HOLDER), time.time()) is None
    assert seen == []


def test_an_oversized_answer_is_refused(transport, monkeypatch):
    monkeypatch.setattr(file_transport, "MAX_SERVED_RESPONSE_BYTES", 8)
    _serving(transport, payload=b"x" * 9)
    assert transport._serve(FILE_REQUEST_PATH, _request(), b"r", b"l1",
                            _Requester(HOLDER), time.time()) is None
    # The refused serve does not keep its slot.
    assert transport._serves == {}


def test_serve_rate_limit_per_link(transport, monkeypatch):
    monkeypatch.setattr(file_transport, "MAX_CONCURRENT_SERVES", 100)
    _serving(transport)
    now = time.time()
    served = sum(
        1 for _ in range(FILE_SERVE_RATE_LIMIT * 2)
        if transport._serve(FILE_REQUEST_PATH, _request(), b"r", b"l1", None,
                            now) is not None
    )
    assert served == FILE_SERVE_RATE_LIMIT
    # A different link is unaffected.
    assert transport._serve(FILE_REQUEST_PATH, _request(), b"r", b"l2", None,
                            now) is not None


def _ranges_for_a_whole_file(chunks: int) -> int:
    """Requests the window rule takes to cover a file, plus its chunk list."""
    window, wins, held, requests = 1, 0, 0, 1
    while held < chunks:
        held += min(window, chunks - held)
        requests += 1
        wins += 1
        if wins >= WINDOW_GROWTH_STREAK:
            wins = 0
            window = min(window * 2, FILE_REQUEST_MAX_CHUNKS)
    return requests


def test_a_whole_download_is_never_rate_limited(transport):
    """Regression guard for the defect files9 found.

    A download issues one range at a time and waits for it, so its request
    rate is the link's speed. At the shared 8-per-second ceiling every one of
    three members pulling the same 2 MB file over loopback was refused
    mid-download, and a refusal is silence, so each paid a 120s stall sweep
    and none of them finished in 924s. The largest file allowed has to fit
    inside one second of this limit, because on a fast enough link it will.
    """
    _serving(transport)
    now = time.time()
    requests = _ranges_for_a_whole_file(MAX_CHUNK_INDEX)
    assert requests <= FILE_SERVE_RATE_LIMIT
    served = sum(
        1 for _ in range(requests)
        if transport._serve(FILE_REQUEST_PATH, _request(), b"r", b"l1",
                            _Requester(HOLDER), now) is not None
    )
    assert served == requests


def test_only_two_files_are_served_at_once(transport):
    _serving(transport)
    now = time.time()
    served = [transport._serve(FILE_REQUEST_PATH, _request(), b"r",
                               bytes([n]), _Requester(HOLDER), now)
              for n in range(MAX_CONCURRENT_SERVES + 1)]
    assert served[:MAX_CONCURRENT_SERVES] == [b"chunk bytes"] * \
        MAX_CONCURRENT_SERVES
    assert served[-1] is None


def test_the_next_range_of_a_download_already_being_served_is_not_a_third(
        transport):
    """Regression guard for the defect the files scenarios found.

    The cap counts concurrent downloads, and a download issues one request at
    a time, so the next range on a link already serving is the same transfer
    continuing. Counted per request instead, every download stalled after its
    first chunk: two requesters filled the cap and every later range was
    refused with silence until the requester's stall timeout expired.
    """
    _serving(transport)
    now = time.time()
    for n in range(MAX_CONCURRENT_SERVES):
        assert transport._serve(FILE_REQUEST_PATH, _request(), b"r",
                                bytes([n]), _Requester(HOLDER), now) is not None

    for _ in range(4):
        assert transport._serve(FILE_REQUEST_PATH, _request(), b"r", b"\x00",
                                _Requester(HOLDER), now) == b"chunk bytes"
    assert transport._serve(FILE_REQUEST_PATH, _request(), b"r", b"l9",
                            _Requester(HOLDER), now) is None


def test_a_finished_serve_frees_its_slot(transport):
    """RNS exposes no concluded callback for a response resource, so a slot
    is freed once its link has no outgoing resource left and the settle floor
    has passed."""
    _serving(transport)
    now = time.time()
    for n in range(MAX_CONCURRENT_SERVES):
        transport._serve(FILE_REQUEST_PATH, _request(), b"r", bytes([n]),
                         _Requester(HOLDER), now)
    assert transport._serve(FILE_REQUEST_PATH, _request(), b"r", b"l9",
                            _Requester(HOLDER), now) is None

    transport._serves = {
        link_id: started - FILE_SERVE_SETTLE_SECS - 1.0
        for link_id, started in transport._serves.items()
    }
    assert transport._serve(FILE_REQUEST_PATH, _request(), b"r", b"l9",
                            _Requester(HOLDER), time.time()) == b"chunk bytes"


def test_start_serving_is_idempotent(transport):
    transport.start_serving()
    first = transport._in_dest
    transport.start_serving()
    assert transport._in_dest is first


def test_stop_serving_deregisters_the_request_handler(transport):
    _serving(transport)
    path_hash = RNS.Identity.truncated_hash(FILE_REQUEST_PATH.encode("utf-8"))
    assert path_hash in transport._in_dest.request_handlers

    transport.stop_serving()
    assert path_hash not in transport._in_dest.request_handlers
    assert transport._serves == {}

    transport.start_serving()
    assert path_hash in transport._in_dest.request_handlers


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def _collect_results(transport):
    results = []
    transport.set_result_callback(
        lambda fid, ok, payload, reason: results.append((fid, ok, reason)))
    return results


@pytest.mark.parametrize("first,count,file_hash", [
    (0, 1, "aa" * 16),                        # hash too short
    (0, 1, "zz" * 32),                        # not hex
    (0, 0, HASH_A),
    (0, FILE_REQUEST_MAX_CHUNKS + 1, HASH_A),
    (-1, 1, HASH_A),
    (MAX_CHUNK_INDEX + 1, 1, HASH_A),
])
def test_an_unusable_fetch_fails_without_dialing(transport, first, count,
                                                 file_hash):
    results = _collect_results(transport)
    transport.fetch_chunks("f1", HOLDER, file_hash, first, count)
    assert results == [("f1", False, FETCH_BAD_PATH)]
    assert transport._conns == {}


def test_a_chunk_list_fetch_of_a_bad_hash_fails(transport):
    results = _collect_results(transport)
    transport.fetch_chunk_list("f1", HOLDER, "aa" * 16)
    assert results == [("f1", False, FETCH_BAD_PATH)]


class _Receipt:
    """Stands in for an RNS RequestReceipt with a given response shape."""

    def __init__(self, response=None, progress=0.0):
        self.response = response
        self.progress = progress


class _RecordingLink:
    """An established link that records identify proofs and requests, in the
    order they were made on it."""

    def __init__(self):
        self.events: list[tuple] = []
        self.torn_down = False

    def identify(self, identity):
        self.events.append(("identify", identity))

    def request(self, path, **kwargs):
        self.events.append(("request", path, kwargs))
        return _Receipt()

    def teardown(self):
        self.torn_down = True

    @property
    def requests(self):
        return [e for e in self.events if e[0] == "request"]


class _DeadLink:
    """A link the remote has already dropped: it still looks established
    here, and every request on it fails to send."""

    def __init__(self):
        self.torn_down = False

    def request(self, *args, **kwargs):
        return False

    def teardown(self):
        self.torn_down = True


def _linked_conn(transport, holder_hex, link):
    conn = LinkConn(holder_hex)
    conn.state = _LINKED
    conn.link = link
    transport._conns[holder_hex] = conn
    transport._by_link[id(link)] = holder_hex
    return conn


def test_a_chunk_request_carries_the_range_and_its_ceiling(transport):
    link = _RecordingLink()
    _linked_conn(transport, HOLDER, link)

    transport.fetch_chunks("f1", HOLDER, HASH_A, 8, 4, timeout=30.0)

    (_, path, kwargs), = link.requests
    assert path == FILE_REQUEST_PATH
    assert kwargs["data"] == {R_FILE_HASH: bytes.fromhex(HASH_A),
                              R_FIRST: 8, R_COUNT: 4}
    assert kwargs["max_response_size"] == max_response_for(4)
    assert kwargs["timeout"] == 30.0


def test_a_chunk_list_request_asks_for_the_list_and_its_ceiling(transport):
    link = _RecordingLink()
    _linked_conn(transport, HOLDER, link)

    transport.fetch_chunk_list("f1", HOLDER, HASH_A)

    (_, path, kwargs), = link.requests
    assert path == FILE_REQUEST_PATH
    assert kwargs["data"] == {R_FILE_HASH: bytes.fromhex(HASH_A),
                              R_WANT_LIST: 1}
    assert kwargs["max_response_size"] == MAX_CHUNK_LIST_BYTES


def test_a_link_identifies_before_its_first_request(transport):
    """A holder cannot check membership without knowing who is asking, so
    identifying is not optional here and must precede the request."""
    link = _RecordingLink()
    conn = _linked_conn(transport, HOLDER, link)
    conn.queued.append(LinkFetch("f1", HOLDER, FILE_REQUEST_PATH, 1024, 60.0,
                                 _request()))

    transport._on_outbound_established(link)

    assert [e[0] for e in link.events] == ["identify", "request"]
    assert link.events[0][1] is transport._identity.rns_identity
    assert transport.is_identified(HOLDER) is True


def test_an_unknown_identity_requests_the_delivery_path(transport,
                                                        monkeypatch):
    """The holder is an identity hash; its file destination is derived from
    the identity recalled for its delivery destination."""
    results = _collect_results(transport)
    requested = []
    monkeypatch.setattr(RNS.Identity, "recall", staticmethod(lambda h: None))
    monkeypatch.setattr(RNS.Transport, "request_path",
                        staticmethod(lambda h, *a, **k: requested.append(h)))

    transport.fetch_chunks("f1", HOLDER, HASH_A, 0, 1)

    delivery_hash = RNS.Destination.hash(bytes.fromhex(HOLDER), "lxmf",
                                         "delivery")
    assert requested == [delivery_hash]
    assert results == []
    conn = transport._conns[HOLDER]
    assert conn.dial_attempts == 1
    assert conn.next_dial_at > time.time()


def test_a_known_identity_dials_the_files_aspect(transport, monkeypatch):
    """The destination dialed is the peer's files destination, not its
    delivery one."""
    dialed = []
    identity = transport._identity.rns_identity
    monkeypatch.setattr(RNS.Identity, "recall",
                        staticmethod(lambda h: identity))
    monkeypatch.setattr(RNS.Transport, "has_path", staticmethod(lambda h: True))
    monkeypatch.setattr(file_transport.RNS, "Link",
                        lambda dest, **kwargs: dialed.append(dest) or object())

    transport.fetch_chunks("f1", HOLDER, HASH_A, 0, 1)

    expected = RNS.Destination(identity, RNS.Destination.OUT,
                               RNS.Destination.SINGLE, APP_NAME,
                               APP_ASPECT_FILES)
    assert [d.hash for d in dialed] == [expected.hash]


def test_a_dead_link_redials_instead_of_failing(transport, monkeypatch):
    results = _collect_results(transport)
    monkeypatch.setattr(RNS.Identity, "recall", staticmethod(lambda h: None))
    monkeypatch.setattr(RNS.Transport, "request_path",
                        staticmethod(lambda h, *a, **k: None))
    link = _DeadLink()
    _linked_conn(transport, HOLDER, link)

    transport.fetch_chunks("f1", HOLDER, HASH_A, 0, 1)

    assert results == []
    assert link.torn_down
    assert [f.fetch_id for f in transport._conns[HOLDER].queued] == ["f1"]


def test_a_second_dead_link_fails_the_fetch(transport, monkeypatch):
    results = _collect_results(transport)
    monkeypatch.setattr(RNS.Identity, "recall", staticmethod(lambda h: None))
    monkeypatch.setattr(RNS.Transport, "request_path",
                        staticmethod(lambda h, *a, **k: None))
    _linked_conn(transport, HOLDER, _DeadLink())
    transport.fetch_chunks("f1", HOLDER, HASH_A, 0, 1)

    conn = transport._conns[HOLDER]
    second = _DeadLink()
    conn.state = _LINKED
    conn.link = second
    transport._by_link[id(second)] = HOLDER
    transport._flush_queued(HOLDER)

    assert [(fid, ok) for fid, ok, _ in results] == [("f1", False)]


def _active_fetch(transport, holder_hex=HOLDER):
    link = _RecordingLink()
    _linked_conn(transport, holder_hex, link)
    transport.fetch_chunks("f1", holder_hex, HASH_A, 0, 1)
    return link, next(iter(transport._active.values()))


def test_a_request_with_no_progress_stalls(transport):
    results = _collect_results(transport)
    _, fetch = _active_fetch(transport)
    fetch.last_progress_at = time.time() - FILE_STALL_SECS - 1.0

    transport.tick()

    assert results == [("f1", False, FETCH_STALLED)]
    assert transport._active == {}


def test_a_slow_transfer_is_not_a_failure(transport):
    """There is no total deadline: a transfer that keeps making progress runs
    as long as it needs to."""
    results = _collect_results(transport)
    _, fetch = _active_fetch(transport)
    fetch.created_at = time.time() - FILE_STALL_SECS * 10

    transport.tick()

    assert results == []
    assert transport._active


def test_progress_resets_the_stall_clock(transport):
    results = _collect_results(transport)
    _, fetch = _active_fetch(transport)
    fetch.last_progress_at = time.time() - FILE_STALL_SECS - 1.0

    transport._on_request_progress(fetch.receipt)
    transport.tick()

    assert results == []
    assert transport._active


def test_a_response_of_bytes_is_delivered(transport):
    delivered = []
    transport.set_result_callback(
        lambda fid, ok, payload, reason: delivered.append((ok, payload)))
    fetch = LinkFetch("f1", HOLDER, FILE_REQUEST_PATH, 1024, 60.0, _request())
    receipt = _Receipt(response=b"64 kilobytes, honest")
    transport._active[id(receipt)] = fetch
    transport._on_response(receipt)

    assert delivered == [(True, b"64 kilobytes, honest")]


@pytest.mark.parametrize("response,reason", [
    (None, FETCH_REFUSED),
    (b"", FETCH_REFUSED),
    ({"not": "bytes"}, FETCH_BAD_RESPONSE),
])
def test_an_unusable_response_is_reported(transport, response, reason):
    results = _collect_results(transport)
    fetch = LinkFetch("f1", HOLDER, FILE_REQUEST_PATH, 1024, 60.0, _request())
    receipt = _Receipt(response=response)
    transport._active[id(receipt)] = fetch
    transport._on_response(receipt)

    assert results == [("f1", False, reason)]


def test_a_queued_fetch_past_its_deadline_is_unreachable(transport,
                                                         monkeypatch):
    results = _collect_results(transport)
    monkeypatch.setattr(RNS.Identity, "recall", staticmethod(lambda h: None))
    monkeypatch.setattr(RNS.Transport, "request_path",
                        staticmethod(lambda h, *a, **k: None))

    transport.fetch_chunks("f1", HOLDER, HASH_A, 0, 1, timeout=0.0)
    transport.tick()

    assert ("f1", False, FETCH_UNREACHABLE) in results


def test_cancelling_a_fetch_drops_it(transport):
    results = _collect_results(transport)
    _active_fetch(transport)

    transport.cancel("f1")

    assert transport._active == {}
    assert results == []


# ---------------------------------------------------------------------------
# The fake transport's own contract
# ---------------------------------------------------------------------------

@pytest.fixture
def registry():
    return FakeFileRegistry()


def _fake_pair(registry, **kwargs):
    requester = FakeFileTransport(HOLDER, registry, **kwargs)
    holder = FakeFileTransport(OTHER_HOLDER, registry)
    return requester, holder


def _serve_bytes(holder, payload=b"chunk bytes"):
    seen = []

    def serve(requester, file_hash_hex, first, count, want_list):
        seen.append((requester, file_hash_hex, first, count, want_list))
        return payload

    holder.set_serve_callback(serve)
    holder.start_serving()
    return seen


def _fake_results(transport):
    results = []
    transport.set_result_callback(
        lambda fid, ok, payload, reason:
        results.append((fid, ok, payload, reason)))
    return results


def test_fake_fetch_reaches_the_holders_serve_callback(registry):
    requester, holder = _fake_pair(registry)
    seen = _serve_bytes(holder)
    results = _fake_results(requester)

    requester.fetch_chunks("f1", OTHER_HOLDER, HASH_A, 2, 3)
    requester.join_threads()

    assert seen == [(HOLDER, HASH_A, 2, 3, False)]
    assert results == [("f1", True, b"chunk bytes", None)]
    assert registry.fetch_log == [(HOLDER, OTHER_HOLDER, HASH_A, 2, 3, False)]


def test_fake_chunk_list_fetch_asks_for_the_list(registry):
    requester, holder = _fake_pair(registry)
    seen = _serve_bytes(holder, payload=b"h" * 32)
    results = _fake_results(requester)

    requester.fetch_chunk_list("f1", OTHER_HOLDER, HASH_A)
    requester.join_threads()

    assert seen == [(HOLDER, HASH_A, 0, 0, True)]
    assert results == [("f1", True, b"h" * 32, None)]


def test_fake_refusal_is_reported(registry):
    requester, holder = _fake_pair(registry)
    holder.set_serve_callback(lambda *args: None)
    holder.start_serving()
    results = _fake_results(requester)

    requester.fetch_chunks("f1", OTHER_HOLDER, HASH_A, 0, 1)
    requester.join_threads()

    assert results == [("f1", False, None, FETCH_REFUSED)]


def test_fake_holder_that_never_started_serving_is_unreachable(registry):
    requester, holder = _fake_pair(registry)
    results = _fake_results(requester)

    requester.fetch_chunks("f1", OTHER_HOLDER, HASH_A, 0, 1)
    requester.join_threads()

    assert results == [("f1", False, None, FETCH_UNREACHABLE)]


def test_fake_unreachable_holder_is_reported(registry):
    requester, holder = _fake_pair(registry, unreachable={OTHER_HOLDER})
    _serve_bytes(holder)
    results = _fake_results(requester)

    requester.fetch_chunks("f1", OTHER_HOLDER, HASH_A, 0, 1)
    requester.join_threads()

    assert results == [("f1", False, None, FETCH_UNREACHABLE)]


def test_fake_stalling_chunk_fails_only_the_range_that_holds_it(registry):
    requester, holder = _fake_pair(registry,
                                   stall_chunks={(OTHER_HOLDER, 5)})
    _serve_bytes(holder)
    results = _fake_results(requester)

    requester.fetch_chunks("f1", OTHER_HOLDER, HASH_A, 4, 2)
    requester.fetch_chunks("f2", OTHER_HOLDER, HASH_A, 0, 2)
    requester.join_threads()

    assert ("f1", False, None, FETCH_STALLED) in results
    assert ("f2", True, b"chunk bytes", None) in results


def test_fake_dropped_link_fails_what_it_carried(registry):
    requester, holder = _fake_pair(registry,
                                   holder_delays={OTHER_HOLDER: 0.3})
    _serve_bytes(holder)
    results = _fake_results(requester)

    requester.fetch_chunks("f1", OTHER_HOLDER, HASH_A, 0, 1)
    assert requester.drop_link(OTHER_HOLDER) is True
    requester.join_threads()

    assert results == [("f1", False, None, FETCH_LINK_CLOSED)]


def test_fake_serves_again_on_a_fresh_link(registry):
    requester, holder = _fake_pair(registry)
    _serve_bytes(holder)
    results = _fake_results(requester)
    requester.drop_link(OTHER_HOLDER)

    requester.fetch_chunks("f1", OTHER_HOLDER, HASH_A, 0, 1)
    requester.join_threads()

    assert results == [("f1", True, b"chunk bytes", None)]


def test_fake_oversized_answer_is_refused(registry):
    requester, holder = _fake_pair(registry)
    _serve_bytes(holder, payload=b"x" * (max_response_for(1) + 1))
    results = _fake_results(requester)

    requester.fetch_chunks("f1", OTHER_HOLDER, HASH_A, 0, 1)
    requester.join_threads()

    assert results == [("f1", False, None, FETCH_TOO_LARGE)]


def test_fake_cancelled_fetch_reports_nothing(registry):
    requester, holder = _fake_pair(registry,
                                   holder_delays={OTHER_HOLDER: 0.2})
    _serve_bytes(holder)
    results = _fake_results(requester)

    requester.fetch_chunks("f1", OTHER_HOLDER, HASH_A, 0, 1)
    requester.cancel("f1")
    requester.join_threads()

    assert results == []


def test_fake_validates_a_fetch_the_way_the_real_one_does(registry):
    requester, holder = _fake_pair(registry)
    _serve_bytes(holder)
    results = _fake_results(requester)

    requester.fetch_chunks("f1", OTHER_HOLDER, HASH_A, 0, 0)
    requester.fetch_chunks("f2", OTHER_HOLDER, "aa" * 16, 0, 1)
    requester.join_threads()

    assert results == [("f1", False, None, FETCH_BAD_PATH),
                       ("f2", False, None, FETCH_BAD_PATH)]


def test_fake_stops_serving_when_asked(registry):
    requester, holder = _fake_pair(registry)
    _serve_bytes(holder)
    holder.stop_serving()
    results = _fake_results(requester)

    requester.fetch_chunks("f1", OTHER_HOLDER, HASH_A, 0, 1)
    requester.join_threads()

    assert results == [("f1", False, None, FETCH_UNREACHABLE)]


def test_fake_progress_is_reported_before_the_result(registry):
    requester, holder = _fake_pair(registry)
    _serve_bytes(holder)
    events = []
    requester.set_progress_callback(
        lambda fid, progress: events.append(("progress", fid, progress)))
    requester.set_result_callback(
        lambda fid, ok, payload, reason: events.append(("result", fid, ok)))

    requester.fetch_chunks("f1", OTHER_HOLDER, HASH_A, 0, 1)
    requester.join_threads()

    assert events == [("progress", "f1", 1.0), ("result", "f1", True)]
