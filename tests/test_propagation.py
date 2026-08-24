"""
Choosing the propagation node offline direct messages are left with.

A conversation has no other member to hold a message for an absent peer, so
this choice is what stands between "sent" and "nowhere". These tests use a
stub router: what LXMF does with the node is its own business, and what
matters here is which node gets chosen, and when.
"""

import time

import pytest

from trenchchat.config import Config
from trenchchat.core.propagation import PropagationNodes

NODE_NEAR = "11" * 16
NODE_FAR = "22" * 16


class StubRouter:
    def __init__(self):
        self.selected: bytes | None = None

    def set_outbound_propagation_node(self, destination_hash: bytes) -> None:
        self.selected = destination_hash


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(data_dir=tmp_path)


@pytest.fixture
def router() -> StubRouter:
    return StubRouter()


@pytest.fixture
def nodes(config, router) -> PropagationNodes:
    return PropagationNodes(config, router)


def test_the_first_node_heard_is_selected(nodes, router):
    assert nodes.selected is None
    nodes.record_node(NODE_NEAR, hops=3)
    assert nodes.selected == NODE_NEAR
    assert router.selected == bytes.fromhex(NODE_NEAR)


def test_a_nearer_node_takes_over(nodes):
    nodes.record_node(NODE_FAR, hops=5)
    nodes.record_node(NODE_NEAR, hops=1)
    assert nodes.selected == NODE_NEAR


def test_a_further_node_does_not_take_over(nodes):
    nodes.record_node(NODE_NEAR, hops=1)
    nodes.record_node(NODE_FAR, hops=6)
    assert nodes.selected == NODE_NEAR


def test_a_pinned_node_wins_over_anything_heard(config, nodes):
    nodes.record_node(NODE_NEAR, hops=1)
    assert nodes.pin(NODE_FAR) is True
    assert nodes.selected == NODE_FAR

    nodes.record_node(NODE_NEAR, hops=0)
    assert nodes.selected == NODE_FAR
    assert config.outbound_propagation_node == NODE_FAR


def test_unpinning_returns_to_automatic(nodes):
    nodes.record_node(NODE_NEAR, hops=1)
    nodes.pin(NODE_FAR)
    assert nodes.pin("") is True
    assert nodes.selected == NODE_NEAR


def test_a_malformed_pin_is_refused(nodes):
    assert nodes.pin("zz") is False
    assert nodes.pinned == ""


def test_a_pinned_node_is_applied_on_startup(config, router):
    config.outbound_propagation_node = NODE_FAR
    started = PropagationNodes(config, router)
    assert started.selected == NODE_FAR
    assert router.selected == bytes.fromhex(NODE_FAR)


def test_known_nodes_are_listed_nearest_first(nodes):
    nodes.record_node(NODE_FAR, hops=5)
    nodes.record_node(NODE_NEAR, hops=1)
    listed = nodes.known_nodes()
    assert [n["hash"] for n in listed] == [NODE_NEAR, NODE_FAR]
    assert listed[0]["selected"] is True


def test_a_silent_node_is_pruned_and_replaced(config, router):
    nodes = PropagationNodes(config, router, ttl_secs=0.05)
    nodes.record_node(NODE_NEAR, hops=1)
    assert nodes.selected == NODE_NEAR

    time.sleep(0.1)
    nodes.record_node(NODE_FAR, hops=9)
    nodes.prune()
    assert nodes.known_nodes()[0]["hash"] == NODE_FAR
    assert nodes.selected == NODE_FAR


def test_selection_fires_a_callback(nodes):
    seen = []
    nodes.add_selection_callback(seen.append)
    nodes.record_node(NODE_NEAR, hops=2)
    nodes.record_node(NODE_NEAR, hops=2)
    assert seen == [NODE_NEAR]


# ---------------------------------------------------------------------------
# Asking for held mail
#
# The cadence is the whole feature: mail is pulled, and a node never says it
# has any. Asking too rarely loses the message that arrived a moment after we
# did; asking constantly is a link per attempt on a radio.
# ---------------------------------------------------------------------------

from trenchchat.core.propagation import (  # noqa: E402
    PropagationCollector, SETTLING_ASK_INTERVAL_SECS, SETTLING_WINDOW_SECS,
    STEADY_ASK_INTERVAL_SECS,
)


class StubCollectRouter:
    """Records every request the collector makes."""

    def __init__(self, started: bool = True):
        self.asks = 0
        self.started = started

    def request_propagation_sync(self, identity) -> bool:
        self.asks += 1
        return self.started


class StubNodes:
    def __init__(self, selected: str | None = NODE_NEAR):
        self.selected = selected
        self.reselects = 0

    def reselect(self):
        self.reselects += 1
        return self.selected


class StubIdentity:
    rns_identity = object()


@pytest.fixture
def collect_router() -> StubCollectRouter:
    return StubCollectRouter()


def make_collector(router, nodes, **kwargs) -> PropagationCollector:
    return PropagationCollector(router, StubIdentity(), nodes, **kwargs)


def test_a_fresh_process_asks_straight_away(collect_router):
    """Starting up is the case mail is most likely to be waiting for."""
    collector = make_collector(collect_router, StubNodes())

    assert collector.tick() is True
    assert collect_router.asks == 1


def test_a_fresh_process_keeps_asking_while_settling(collect_router):
    """A sender can still be uploading as we arrive -- LXMF makes them generate
    a proof-of-work stamp first -- so one empty answer means nothing, and the
    cadence deliberately does not slow down because an ask came back empty."""
    collector = make_collector(collect_router, StubNodes())
    now = 1000.0

    collector.tick(now)
    assert collector.tick(now + SETTLING_ASK_INTERVAL_SECS - 1) is False
    assert collector.tick(now + SETTLING_ASK_INTERVAL_SECS + 1) is True
    assert collect_router.asks == 2


def test_asking_settles_down_once_the_window_passes(collect_router):
    """Past the window, asking is paced by the steady interval measured from
    the last ask -- one link every few minutes, not one every fifteen seconds
    for the life of the process."""
    collector = make_collector(collect_router, StubNodes())
    last_ask = 1000.0
    collector.tick(last_ask)

    settled = last_ask + SETTLING_WINDOW_SECS + 1
    assert collector.tick(settled) is False
    assert collector.tick(last_ask + STEADY_ASK_INTERVAL_SECS - 1) is False
    assert collector.tick(last_ask + STEADY_ASK_INTERVAL_SECS + 1) is True
    assert collect_router.asks == 2


def test_coming_back_reopens_the_window(collect_router):
    """A link returning, or a node being chosen, is the same situation as a
    fresh start: ask now, and keep asking for a while."""
    collector = make_collector(collect_router, StubNodes())
    now = 1000.0
    collector.tick(now)
    settled = now + SETTLING_WINDOW_SECS + 1
    assert collector.tick(settled) is False

    assert collector.collect_now(settled + 10) is True
    assert collector.tick(settled + 10 + SETTLING_ASK_INTERVAL_SECS + 1) is True


def test_nothing_is_asked_without_a_node(collect_router):
    collector = make_collector(collect_router, StubNodes(selected=None))

    assert collector.tick() is False
    assert collect_router.asks == 0


def test_an_explicit_ask_reselects_when_no_node_is_held(collect_router):
    """The user asking is also a reason to look for a node again."""
    nodes = StubNodes(selected=None)
    collector = make_collector(collect_router, nodes)

    collector.collect_now()

    assert nodes.reselects == 1
    assert collect_router.asks == 1
