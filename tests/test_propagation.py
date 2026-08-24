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
