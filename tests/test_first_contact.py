"""
Answering a peer the first time we hear them.

Our own re-announce is 15 minutes apart. Until a peer has heard it they cannot
recall our identity, so LXMF cannot verify anything we send them -- their
router quarantines our first message and drops it when it expires. That is not
a theoretical window: it is what a client joining a live mesh hits, and what
made invites from a freshly started client vanish until it was relaunched.

Answering *every* announce would fix it and leave two idle clients replying to
each other's replies for ever, so the rule is once per peer. These pin that
rule, the coalescing that keeps meeting a crowd to one announce, and the bound
that stops a stream of minted identities growing the set without limit.
"""

import pytest

from trenchchat.network.announce import (
    FIRST_CONTACT_COALESCE_SECS, FirstContactAnnouncer,
)

SELF_HEX = "aa" * 16
PEER_A = "bb" * 16
PEER_B = "cc" * 16


class StubRouter:
    def __init__(self):
        self.announces: list = []
        self.user_announces: list = []

    def announce(self, attached_interface=None):
        self.announces.append(attached_interface)

    def announce_user(self, attached_interface=None):
        self.user_announces.append(attached_interface)


class StubChannels:
    def __init__(self):
        self.announces: list = []

    def announce_all_owned(self, attached_interface=None):
        self.announces.append(attached_interface)


@pytest.fixture
def router() -> StubRouter:
    return StubRouter()


@pytest.fixture
def channels() -> StubChannels:
    return StubChannels()


@pytest.fixture
def announcer(router, channels) -> FirstContactAnnouncer:
    return FirstContactAnnouncer(router, channels, SELF_HEX)


def test_a_new_peer_is_answered(announcer, router, channels):
    assert announcer.note_peer(PEER_A, iface="eth", now=100.0) is True
    assert announcer.tick(now=100.0) is False  # still coalescing

    assert announcer.tick(now=100.0 + FIRST_CONTACT_COALESCE_SECS) is True
    assert router.announces == ["eth"]
    assert router.user_announces == ["eth"]
    assert channels.announces == ["eth"]


def test_the_same_peer_is_answered_only_once(announcer, router):
    announcer.note_peer(PEER_A, now=100.0)
    announcer.tick(now=110.0)

    assert announcer.note_peer(PEER_A, now=120.0) is False
    assert announcer.tick(now=130.0) is False
    assert len(router.announces) == 1


def test_meeting_a_crowd_costs_one_announce(announcer, router):
    """Four testers announcing at once must not draw four announces back."""
    for peer in (PEER_A, PEER_B, "dd" * 16, "ee" * 16):
        announcer.note_peer(peer, iface="eth", now=100.0)

    assert announcer.tick(now=110.0) is True
    assert router.announces == ["eth"]


def test_peers_on_different_interfaces_fall_back_to_broadcast(announcer, router):
    """Two interfaces cannot be targeted at once, so answer on all of them."""
    announcer.note_peer(PEER_A, iface="eth", now=100.0)
    announcer.note_peer(PEER_B, iface="radio", now=100.5)

    assert announcer.tick(now=110.0) is True
    assert router.announces == [None]


def test_a_later_peer_gets_its_own_announce(announcer, router):
    announcer.note_peer(PEER_A, iface="eth", now=100.0)
    announcer.tick(now=110.0)

    announcer.note_peer(PEER_B, iface="eth", now=200.0)
    assert announcer.tick(now=210.0) is True
    assert len(router.announces) == 2


def test_our_own_announce_is_never_answered(announcer, router):
    assert announcer.note_peer(SELF_HEX, now=100.0) is False
    assert announcer.tick(now=110.0) is False
    assert router.announces == []


def test_an_empty_hash_is_ignored(announcer, router):
    assert announcer.note_peer("", now=100.0) is False
    assert announcer.tick(now=110.0) is False
    assert router.announces == []


def test_nothing_is_sent_without_a_peer(announcer, router):
    assert announcer.tick(now=100.0) is False
    assert router.announces == []


def test_the_answered_set_is_bounded(router, channels):
    """Identities are free to mint, so remembering every one is not an option.

    Answering an evicted peer a second time is the accepted cost.
    """
    announcer = FirstContactAnnouncer(router, channels, SELF_HEX, max_answered=4)
    for i in range(10):
        announcer.note_peer(f"{i:032x}", now=100.0 + i)
        announcer.tick(now=200.0 + i)

    # The oldest were evicted, so the very first peer reads as new again.
    assert announcer.note_peer(f"{0:032x}", now=300.0) is True


def test_an_announce_failure_does_not_kill_the_ticker(channels):
    """A send that raises must not take the loop driving it down with it."""
    class Broken:
        def announce(self, attached_interface=None):
            raise RuntimeError("interface went away")

        def announce_user(self, attached_interface=None):
            raise AssertionError("should not be reached")

    announcer = FirstContactAnnouncer(Broken(), channels, SELF_HEX)
    announcer.note_peer(PEER_A, now=100.0)

    assert announcer.tick(now=110.0) is True
    assert announcer.note_peer(PEER_A, now=120.0) is False


# ---------------------------------------------------------------------------
# Hearing an identity arrive as a path response
# ---------------------------------------------------------------------------

from trenchchat.network.announce import PathResponseHandler  # noqa: E402


class StubIdentity:
    def __init__(self, hex_hash: str):
        self.hash = bytes.fromhex(hex_hash)


def test_path_responses_are_asked_for():
    """RNS gates path-response delivery on this exact attribute.

    Without it the handler is simply never called, and a held first message
    waits for an announce that may be fifteen minutes away -- silently, which
    is what made this hard to see in the first place.
    """
    assert PathResponseHandler.receive_path_responses is True
    assert PathResponseHandler.aspect_filter == "lxmf.delivery"


def test_a_resolved_identity_is_reported():
    seen = []
    handler = PathResponseHandler(seen.append)

    handler.received_announce(b"\x00" * 16, StubIdentity(PEER_A), b"", b"")

    assert seen == [PEER_A]


def test_an_unresolvable_announce_is_ignored():
    seen = []
    handler = PathResponseHandler(seen.append)

    handler.received_announce(b"\x00" * 16, None, b"", b"")

    assert seen == []


def test_a_raising_callback_does_not_escape_into_rns():
    def boom(_peer_hex):
        raise RuntimeError("handler blew up")

    handler = PathResponseHandler(boom)

    handler.received_announce(b"\x00" * 16, StubIdentity(PEER_A), b"", b"")
