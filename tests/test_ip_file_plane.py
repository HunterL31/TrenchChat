"""
IPFileTransport: the file plane over a real direct session.

Two peers with a real QUIC session between them, so what is exercised here is
the REQ/RESP streams themselves: a range asked for and answered, a refusal that
arrives as one, the identity the serve callback is handed, and every bound the
serving side holds a peer to. The engine above it is tests/test_files.py; this
is the plane under it.
"""

import os
import threading
import time

import pytest

from trenchchat.core.files import FileManager
from trenchchat.core.permissions import PRESET_PRIVATE, ROLE_MEMBER, ROLE_OWNER
from trenchchat.core.protocol import FILE_CHUNK_BYTES
from trenchchat.network.base import DIRECT_FILE_REQUEST_MAX_CHUNKS
from trenchchat.network.file_transport import (
    FETCH_REFUSED, FETCH_STALLED, R_COUNT, R_FILE_HASH, R_FIRST,
)
from trenchchat.network.ip.file_plane import (
    FILE_OP, MAX_CONCURRENT_SERVES_PER_SESSION, IPFileTransport,
)
from trenchchat.network.link_client import FETCH_UNREACHABLE

FILE_HASH = "ab" * 32
CHUNK = b"x" * FILE_CHUNK_BYTES


class Results:
    """Everything a plane reported, waited on rather than slept for."""

    def __init__(self):
        self.done: dict[str, tuple] = {}
        self._event = threading.Event()

    def record(self, fetch_id, ok, payload, reason):
        self.done[fetch_id] = (ok, payload, reason)
        self._event.set()

    def wait(self, fetch_id: str, timeout: float = 10.0) -> tuple:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if fetch_id in self.done:
                return self.done[fetch_id]
            self._event.wait(0.05)
            self._event.clear()
        raise AssertionError(f"{fetch_id} never finished")


@pytest.fixture
def pair(peer_factory):
    """Two peers with a session, each with a file plane on it."""
    alice = peer_factory("alice", direct=True)
    bob = peer_factory("bob", direct=True)
    planes = {}
    for peer in (alice, bob):
        plane = IPFileTransport(peer.ip_transport)
        plane.start_serving()
        planes[peer.name] = plane
    yield alice, bob, planes["alice"], planes["bob"]
    for plane in planes.values():
        plane.stop_serving()


def served(payload: bytes):
    """A serve callback that always answers with these bytes, recording who asked."""
    asked: list[tuple] = []

    def _serve(requester_hex, file_hash_hex, first, count, want_list):
        asked.append((requester_hex, file_hash_hex, first, count, want_list))
        return payload

    return _serve, asked


def test_a_range_is_pulled_over_the_session(pair):
    alice, bob, alice_plane, bob_plane = pair
    serve, asked = served(CHUNK * 3)
    alice_plane.set_serve_callback(serve)
    results = Results()
    bob_plane.set_result_callback(results.record)

    bob_plane.fetch_chunks("f1", alice.identity.hash_hex, FILE_HASH, 0, 3)
    ok, payload, reason = results.wait("f1")

    assert ok and reason is None
    assert payload == CHUNK * 3
    assert asked == [(bob.identity.hash_hex, FILE_HASH, 0, 3, False)], \
        "the serve callback was not given the identity the session proved"


def test_a_refusal_arrives_as_one(pair):
    alice, bob, alice_plane, bob_plane = pair
    alice_plane.set_serve_callback(lambda *args: None)
    results = Results()
    bob_plane.set_result_callback(results.record)

    bob_plane.fetch_chunks("f2", alice.identity.hash_hex, FILE_HASH, 0, 1)
    ok, payload, reason = results.wait("f2")

    assert not ok and payload is None and reason == FETCH_REFUSED


def test_a_holder_with_no_session_is_unreachable(pair):
    _alice, _bob, _alice_plane, bob_plane = pair
    results = Results()
    bob_plane.set_result_callback(results.record)

    bob_plane.fetch_chunks("f3", "cd" * 16, FILE_HASH, 0, 1)

    assert results.wait("f3")[2] == FETCH_UNREACHABLE
    assert bob_plane.can_reach("cd" * 16) is False


def test_the_chunk_list_comes_back_whole(pair):
    alice, _bob, alice_plane, bob_plane = pair
    digests = os.urandom(32 * 200)
    alice_plane.set_serve_callback(lambda *args: digests)
    results = Results()
    bob_plane.set_result_callback(results.record)

    bob_plane.fetch_chunk_list("f4", alice.identity.hash_hex, FILE_HASH)
    ok, payload, _reason = results.wait("f4")

    assert ok and payload == digests


def test_a_range_over_the_direct_ceiling_is_refused(pair):
    """The serving side's own bound, called the way a bad client would call it:
    straight at the handler, with no engine above it to keep it honest."""
    _alice, bob, alice_plane, _bob_plane = pair
    serve, asked = served(CHUNK)
    alice_plane.set_serve_callback(serve)

    ok, body = alice_plane._serve(bob.identity.hash_hex, {
        R_FILE_HASH: bytes.fromhex(FILE_HASH),
        R_FIRST: 0,
        R_COUNT: DIRECT_FILE_REQUEST_MAX_CHUNKS + 1,
    })

    assert not ok and body == {}
    assert asked == [], "a range over the ceiling reached the core layer"


def test_an_oversized_answer_is_refused_rather_than_sent(pair):
    """A serve callback that answers with more than the path allows is the
    holder's own bug, and it is caught before it becomes the requester's."""
    _alice, bob, alice_plane, _bob_plane = pair
    alice_plane.set_serve_callback(
        lambda *args: b"\x00" * (DIRECT_FILE_REQUEST_MAX_CHUNKS + 1)
        * FILE_CHUNK_BYTES)

    ok, body = alice_plane._serve(bob.identity.hash_hex, {
        R_FILE_HASH: bytes.fromhex(FILE_HASH), R_FIRST: 0, R_COUNT: 1,
    })

    assert not ok and body == {}


def test_a_malformed_request_is_refused(pair):
    _alice, bob, alice_plane, _bob_plane = pair
    serve, asked = served(CHUNK)
    alice_plane.set_serve_callback(serve)

    for payload in ({}, {R_FILE_HASH: b"short"},
                    {R_FILE_HASH: bytes.fromhex(FILE_HASH), R_FIRST: -1,
                     R_COUNT: 1},
                    {R_FILE_HASH: bytes.fromhex(FILE_HASH), R_FIRST: 0,
                     R_COUNT: 0}):
        assert alice_plane._serve(bob.identity.hash_hex, payload) == (False, {})
    assert asked == []


def test_only_so_many_ranges_are_served_at_once(pair):
    """A peer that does not wait for its answers is bounded; one that does is
    never touched by this."""
    _alice, bob, alice_plane, _bob_plane = pair
    holding = threading.Event()
    release = threading.Event()

    def _slow_serve(*_args):
        holding.set()
        release.wait(5.0)
        return CHUNK

    alice_plane.set_serve_callback(_slow_serve)
    request = {R_FILE_HASH: bytes.fromhex(FILE_HASH), R_FIRST: 0, R_COUNT: 1}
    threads = [threading.Thread(
        target=alice_plane._serve, args=(bob.identity.hash_hex, request),
        daemon=True) for _ in range(MAX_CONCURRENT_SERVES_PER_SESSION)]
    for thread in threads:
        thread.start()
    assert holding.wait(5.0)
    while sum(1 for t in threads if t.is_alive()) < \
            MAX_CONCURRENT_SERVES_PER_SESSION:
        time.sleep(0.01)

    assert alice_plane._serve(bob.identity.hash_hex, request) == (False, {})
    release.set()
    for thread in threads:
        thread.join(timeout=5.0)
    assert alice_plane._serve(bob.identity.hash_hex, request)[0] is True


def test_a_fetch_with_no_answer_stalls_rather_than_hangs(pair):
    alice, _bob, alice_plane, bob_plane = pair
    release = threading.Event()
    alice_plane.set_serve_callback(lambda *args: release.wait(10.0) and CHUNK)
    results = Results()
    bob_plane.set_result_callback(results.record)

    bob_plane.fetch_chunks("f5", alice.identity.hash_hex, FILE_HASH, 0, 1,
                           timeout=0.1)
    time.sleep(0.2)
    bob_plane.tick()

    assert results.wait("f5", timeout=2.0)[2] == FETCH_STALLED
    release.set()


def test_a_session_that_ends_fails_what_it_was_carrying(pair):
    alice, _bob, alice_plane, bob_plane = pair
    release = threading.Event()
    alice_plane.set_serve_callback(lambda *args: release.wait(10.0) and CHUNK)
    results = Results()
    bob_plane.set_result_callback(results.record)

    bob_plane.fetch_chunks("f6", alice.identity.hash_hex, FILE_HASH, 0, 1)
    time.sleep(0.2)
    assert bob_plane.drop_link(alice.identity.hash_hex) is True

    assert results.wait("f6", timeout=2.0)[0] is False
    release.set()


def test_an_unknown_operation_is_refused_without_a_handler(pair):
    """Every plane registers its own op; a session carries no others."""
    alice, _bob, _alice_plane, bob_plane = pair
    answered: list = []

    request_id = bob_plane._transport.send_request(
        alice.identity.hash_hex, "not-an-op", {},
        lambda ok, body: answered.append((ok, body)))
    assert request_id is not None
    deadline = time.time() + 5.0
    while not answered and time.time() < deadline:
        time.sleep(0.02)

    assert answered == [(False, {})]


def test_the_file_op_is_the_only_one_the_plane_claims(pair):
    _alice, _bob, alice_plane, _bob_plane = pair
    assert FILE_OP in alice_plane._transport._request_handlers
    alice_plane.stop_serving()
    assert FILE_OP not in alice_plane._transport._request_handlers


# ---------------------------------------------------------------------------
# End to end, with the real engine on top
# ---------------------------------------------------------------------------

def _shared_channel(owner, member):
    """An invite-only channel both peers are members of, mirrored on each."""
    perms = dict(PRESET_PRIVATE)
    ch_hash = owner.channel_mgr.create_channel("direct-files", "",
                                               permissions=perms)
    for peer in (owner, member):
        peer.storage.upsert_channel(ch_hash, "direct-files", "",
                                    owner.identity.hash_hex, perms, time.time())
        peer.storage.subscribe(ch_hash)
        peer.storage.set_channel_permissions(ch_hash, perms)
        peer.storage.upsert_member(ch_hash, owner.identity.hash_hex, "Alice",
                                   role=ROLE_OWNER)
        peer.storage.upsert_member(ch_hash, member.identity.hash_hex, "Bob",
                                   role=ROLE_MEMBER)
    return ch_hash


def test_a_file_moves_between_two_managers_over_the_session(peer_factory):
    """The whole pull over real streams: manifest, chunk list, ranges, digest."""
    alice = peer_factory("alice", direct=True)
    bob = peer_factory("bob", direct=True)
    ch_hash = _shared_channel(alice, bob)
    alice.presence_mgr.record_seen(bob.identity.hash_hex)
    bob.presence_mgr.record_seen(alice.identity.hash_hex)

    planes = {}
    managers = {}
    for peer in (alice, bob):
        plane = IPFileTransport(peer.ip_transport)
        planes[peer.name] = plane
        managers[peer.name] = FileManager(
            peer.identity, peer.storage, peer.presence_mgr,
            transport=plane, direct_transport=plane)
    try:
        data = os.urandom(FILE_CHUNK_BYTES * 5 + 17)
        manifest = managers["alice"].share(ch_hash, "survey.bin", data)
        assert manifest is not None
        sent_at = time.time()
        for peer in (alice, bob):
            peer.storage.insert_message(
                ch_hash, alice.identity.hash_hex, "Alice", "here", sent_at,
                "m1", None, None, sent_at, manifest=manifest)

        assert managers["bob"].request_download(ch_hash, "m1") is not None
        deadline = time.time() + 20.0
        while time.time() < deadline:
            status = managers["bob"].download_status(manifest["hash"].hex())
            if status and status["state"] == "done":
                break
            time.sleep(0.05)
        assert managers["bob"].file_bytes(manifest["hash"].hex()) == data
    finally:
        for manager in managers.values():
            manager.stop()
