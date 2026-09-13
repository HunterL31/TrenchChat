"""
IPScreenTransport: screen share requests over a real direct session.

Two peers with a QUIC session between them, so started, watch and update
travel the real exchange over a real socket, and every bound the plane keeps
is reached the way a peer reaches it. The manager above it is
tests/test_screen.py; this is the plane under it, with its callbacks stubbed.
"""

import threading
import time

import pytest

from tests.test_screen_wire import jpeg
from trenchchat.network.ip.screen_plane import (
    A_STARTED, A_UPDATE, CONTROL_RATE_LIMIT, IPScreenTransport,
    K_ACTION, K_CHANNEL, K_DATA, K_FPS, K_HEIGHT, K_TILE_SHIFT, K_WIDTH,
    REASON_FORBIDDEN, REASON_MALFORMED, REASON_NOT_WATCHING,
    REASON_RATE_LIMITED, SCREEN_OP,
)
from trenchchat.network.screen_wire import (
    KIND_FULL, MAX_UPDATE_BYTES, ScreenUpdate, pack_update,
)

CHANNEL = "ab" * 16


def wait_until(predicate, message: str, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(message)


class Seen:
    """What a plane handed up, and what its callbacks were told to answer."""

    def __init__(self, *, allow_share=True, watch_reason=None, take_updates=True):
        self.lock = threading.Lock()
        self.started: list[tuple[str, dict]] = []
        self.stopped: list[tuple[str, str]] = []
        self.watches: list[tuple[str, str, int, int]] = []
        self.unwatches: list[str] = []
        self.updates: list[tuple[str, ScreenUpdate]] = []
        self.allow_share = allow_share
        self.watch_reason = watch_reason
        self.take_updates = take_updates

    def install(self, plane: IPScreenTransport) -> IPScreenTransport:
        plane.set_started_callback(self._started)
        plane.set_stopped_callback(self._stopped)
        plane.set_watch_callback(self._watch)
        plane.set_unwatch_callback(self._unwatch)
        plane.set_update_callback(self._update)
        return plane

    def _started(self, peer, info):
        with self.lock:
            self.started.append((peer, info))
        return self.allow_share

    def _stopped(self, peer, channel):
        with self.lock:
            self.stopped.append((peer, channel))

    def _watch(self, peer, channel, w, h):
        with self.lock:
            self.watches.append((peer, channel, w, h))
        return self.watch_reason

    def _unwatch(self, peer):
        with self.lock:
            self.unwatches.append(peer)

    def _update(self, peer, update):
        with self.lock:
            self.updates.append((peer, update))
        return self.take_updates


class Answer:
    """One request's answer, as the sender's callback receives it."""

    def __init__(self):
        self.event = threading.Event()
        self.ok = None
        self.body = None

    def __call__(self, ok, body):
        self.ok, self.body = ok, body
        self.event.set()

    def wait(self, timeout: float = 5.0):
        assert self.event.wait(timeout), "no answer arrived"
        return self.ok, self.body


@pytest.fixture
def pair(peer_factory):
    alice = peer_factory("alice", direct=True)
    bob = peer_factory("bob", direct=True)
    seen_a, seen_b = Seen(), Seen()
    plane_a = seen_a.install(IPScreenTransport(alice.ip_transport))
    plane_b = seen_b.install(IPScreenTransport(bob.ip_transport))
    yield alice, bob, plane_a, plane_b, seen_a, seen_b


def full_update(seq: int = 1) -> ScreenUpdate:
    return ScreenUpdate(seq=seq, width=200, height=100, kind=KIND_FULL,
                        entries=[(0, 0, jpeg(200, 100))])


def test_started_reaches_the_peer_with_its_fields(pair):
    alice, bob, plane_a, _plane_b, _seen_a, seen_b = pair
    answer = Answer()
    assert plane_a.send_started(bob.identity.hash_hex, CHANNEL, 640, 480, 7, 15,
                                answer)
    assert answer.wait() == (True, {})
    assert seen_b.started == [(alice.identity.hash_hex, {
        "channel": CHANNEL, "width": 640, "height": 480, "tile_shift": 7,
        "fps": 15})]


def test_a_started_the_peer_refuses_is_answered_forbidden(pair):
    alice, bob, plane_a, _plane_b, _seen_a, seen_b = pair
    seen_b.allow_share = False
    answer = Answer()
    plane_a.send_started(bob.identity.hash_hex, CHANNEL, 640, 480, 7, 15, answer)
    ok, body = answer.wait()
    assert (ok, body.get("r")) == (False, REASON_FORBIDDEN)


def test_a_malformed_started_never_reaches_the_callback(pair):
    alice, bob, _plane_a, _plane_b, _seen_a, seen_b = pair
    answer = Answer()
    alice.ip_transport.send_request(bob.identity.hash_hex, SCREEN_OP, {
        K_ACTION: A_STARTED, K_CHANNEL: b"short", K_WIDTH: 640, K_HEIGHT: 480,
        K_TILE_SHIFT: 7, K_FPS: 15}, answer)
    assert answer.wait()[1].get("r") == REASON_MALFORMED
    answer = Answer()
    alice.ip_transport.send_request(bob.identity.hash_hex, SCREEN_OP, {
        K_ACTION: A_STARTED, K_CHANNEL: bytes.fromhex(CHANNEL), K_WIDTH: 99999,
        K_HEIGHT: 480, K_TILE_SHIFT: 7, K_FPS: 15}, answer)
    assert answer.wait()[1].get("r") == REASON_MALFORMED
    assert seen_b.started == []


def test_watch_and_unwatch_reach_the_sharer(pair):
    alice, bob, plane_a, _plane_b, _seen_a, seen_b = pair
    answer = Answer()
    plane_a.send_watch(bob.identity.hash_hex, CHANNEL, 1280, 720, answer)
    assert answer.wait() == (True, {})
    assert seen_b.watches == [(alice.identity.hash_hex, CHANNEL, 1280, 720)]
    answer = Answer()
    plane_a.send_unwatch(bob.identity.hash_hex, answer)
    assert answer.wait()[0] is True
    assert seen_b.unwatches == [alice.identity.hash_hex]


def test_a_refused_watch_carries_its_reason(pair):
    alice, bob, plane_a, _plane_b, _seen_a, seen_b = pair
    seen_b.watch_reason = "full"
    answer = Answer()
    plane_a.send_watch(bob.identity.hash_hex, CHANNEL, 1280, 720, answer)
    assert answer.wait() == (False, {"r": "full"})


def test_an_update_arrives_parsed_and_its_answer_is_the_credit(pair):
    alice, bob, plane_a, _plane_b, _seen_a, seen_b = pair
    answer = Answer()
    assert plane_a.send_update(bob.identity.hash_hex, full_update(4), answer)
    assert answer.wait() == (True, {})
    peer, update = seen_b.updates[0]
    assert peer == alice.identity.hash_hex
    assert (update.seq, update.kind, update.width) == (4, KIND_FULL, 200)


def test_an_update_nobody_is_watching_is_refused_without_ending_the_session(pair):
    alice, bob, plane_a, _plane_b, _seen_a, seen_b = pair
    seen_b.take_updates = False
    answer = Answer()
    plane_a.send_update(bob.identity.hash_hex, full_update(), answer)
    assert answer.wait()[1].get("r") == REASON_NOT_WATCHING
    assert alice.ip_transport.can_reach(bob.identity.hash_hex)


def test_an_update_whose_image_lies_about_its_size_is_refused(pair):
    alice, bob, _plane_a, _plane_b, _seen_a, seen_b = pair
    lying = ScreenUpdate(seq=1, width=200, height=100, kind=KIND_FULL,
                         entries=[(0, 0, jpeg(1900, 1000))])
    answer = Answer()
    alice.ip_transport.send_request(bob.identity.hash_hex, SCREEN_OP, {
        K_ACTION: A_UPDATE, K_DATA: pack_update(lying)}, answer)
    assert answer.wait()[1].get("r") == REASON_MALFORMED
    assert seen_b.updates == []
    assert alice.ip_transport.can_reach(bob.identity.hash_hex)


def test_an_oversize_update_is_refused_on_the_way_out_and_on_the_way_in(pair):
    alice, bob, plane_a, _plane_b, _seen_a, seen_b = pair
    huge = ScreenUpdate(seq=1, width=1920, height=1080,
                        entries=[(tx, ty, b"\xff\xd8" + b"x" * 200_000)
                                 for ty in range(9) for tx in range(15)])
    assert not plane_a.send_update(bob.identity.hash_hex, huge)
    answer = Answer()
    alice.ip_transport.send_request(bob.identity.hash_hex, SCREEN_OP, {
        K_ACTION: A_UPDATE, K_DATA: b"\x01" * (MAX_UPDATE_BYTES + 1)}, answer)
    assert answer.wait(15.0)[1].get("r") == REASON_MALFORMED
    assert seen_b.updates == []


def test_control_actions_are_rate_limited_and_updates_are_not(pair):
    alice, bob, plane_a, _plane_b, _seen_a, seen_b = pair
    # One at a time, so the transport's own in-flight ceiling is not what
    # refuses them.
    results = []
    for _ in range(CONTROL_RATE_LIMIT + 5):
        answer = Answer()
        plane_a.send_watch(bob.identity.hash_hex, CHANNEL, 640, 480, answer)
        results.append(answer.wait(10.0))
    refused = [body.get("r") for ok, body in results if not ok]
    assert refused and set(refused) == {REASON_RATE_LIMITED}
    assert len(seen_b.watches) == CONTROL_RATE_LIMIT
    answer = Answer()
    plane_a.send_update(bob.identity.hash_hex, full_update(), answer)
    assert answer.wait()[0] is True


def test_nothing_goes_without_a_session(pair):
    alice, bob, plane_a, _plane_b, _seen_a, _seen_b = pair
    stranger = "cd" * 16
    assert not plane_a.send_started(stranger, CHANNEL, 640, 480, 7, 15)
    assert not plane_a.send_watch(stranger, CHANNEL, 640, 480)
    assert not plane_a.send_update(stranger, full_update())
    assert not plane_a.can_reach(stranger)
    assert plane_a.can_reach(bob.identity.hash_hex)


def test_a_stopped_plane_answers_nothing(pair):
    alice, bob, plane_a, plane_b, _seen_a, seen_b = pair
    plane_b.stop()
    answer = Answer()
    plane_a.send_started(bob.identity.hash_hex, CHANNEL, 640, 480, 7, 15, answer)
    assert answer.wait() == (False, {})
    assert seen_b.started == []
