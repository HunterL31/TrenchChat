"""
Unit tests for the network map data-gathering logic.

These tests mock the RNS.Reticulum instance and RNS.Identity.recall so no
real Reticulum stack is needed.
"""

from unittest.mock import MagicMock, patch

from trenchchat.core.network_map import gather_network_data


SELF_HEX = "aa" * 16
PEER_HEX = "bb" * 16
TRANSPORT_HEX = "cc" * 16
UNKNOWN_HEX = "dd" * 16


def _make_rns(path_table=None, interface_stats=None):
    """Build a minimal mock RNS.Reticulum instance."""
    rns = MagicMock()
    rns.get_path_table.return_value = path_table or []
    rns.get_interface_stats.return_value = interface_stats or {"interfaces": []}
    return rns


def _peer_bytes(hex_id: str) -> bytes:
    return bytes.fromhex(hex_id)


# ---------------------------------------------------------------------------
# Basic structure
# ---------------------------------------------------------------------------

def test_self_node_always_present():
    rns = _make_rns()
    with patch("trenchchat.core.network_map.RNS.Identity.recall", return_value=None):
        data = gather_network_data(rns, SELF_HEX)
    ids = {n["id"] for n in data["nodes"]}
    assert SELF_HEX in ids


def test_self_node_kind_is_self():
    rns = _make_rns()
    with patch("trenchchat.core.network_map.RNS.Identity.recall", return_value=None):
        data = gather_network_data(rns, SELF_HEX)
    self_node = next(n for n in data["nodes"] if n["id"] == SELF_HEX)
    assert self_node["kind"] == "self"
    assert self_node["label"] == "This device"


def test_empty_path_table_returns_only_self():
    rns = _make_rns(path_table=[])
    with patch("trenchchat.core.network_map.RNS.Identity.recall", return_value=None):
        data = gather_network_data(rns, SELF_HEX)
    assert len(data["nodes"]) == 1
    assert data["edges"] == []


# ---------------------------------------------------------------------------
# Node classification
# ---------------------------------------------------------------------------

def test_direct_peer_classified_as_peer():
    """A 1-hop destination whose identity is known should be classified as 'peer'."""
    path_table = [
        {
            "hash": _peer_bytes(PEER_HEX),
            "via":  _peer_bytes(PEER_HEX),
            "hops": 1,
            "timestamp": 0.0,
            "expires": 0.0,
            "interface": "TestIface",
        }
    ]
    rns = _make_rns(path_table=path_table)
    mock_identity = MagicMock()

    def recall(dest_hash, **kwargs):
        return mock_identity

    with patch("trenchchat.core.network_map.RNS.Identity.recall", side_effect=recall):
        data = gather_network_data(rns, SELF_HEX)

    peer_node = next((n for n in data["nodes"] if n["id"] == PEER_HEX), None)
    assert peer_node is not None
    assert peer_node["kind"] == "peer"


def test_unknown_destination_classified_as_unknown():
    """A destination whose identity cannot be recalled should be 'unknown'."""
    path_table = [
        {
            "hash": _peer_bytes(UNKNOWN_HEX),
            "via":  _peer_bytes(UNKNOWN_HEX),
            "hops": 1,
            "timestamp": 0.0,
            "expires": 0.0,
            "interface": "TestIface",
        }
    ]
    rns = _make_rns(path_table=path_table)

    with patch("trenchchat.core.network_map.RNS.Identity.recall", return_value=None):
        data = gather_network_data(rns, SELF_HEX)

    unknown_node = next((n for n in data["nodes"] if n["id"] == UNKNOWN_HEX), None)
    assert unknown_node is not None
    assert unknown_node["kind"] == "unknown"


def test_multi_hop_via_node_classified_as_transport():
    """The next-hop (via) node for a multi-hop path should be 'transport'."""
    path_table = [
        {
            "hash": _peer_bytes(PEER_HEX),
            "via":  _peer_bytes(TRANSPORT_HEX),
            "hops": 2,
            "timestamp": 0.0,
            "expires": 0.0,
            "interface": "TestIface",
        }
    ]
    rns = _make_rns(path_table=path_table)

    with patch("trenchchat.core.network_map.RNS.Identity.recall", return_value=None):
        data = gather_network_data(rns, SELF_HEX)

    transport_node = next((n for n in data["nodes"] if n["id"] == TRANSPORT_HEX), None)
    assert transport_node is not None
    assert transport_node["kind"] == "transport"


# ---------------------------------------------------------------------------
# Edges
# ---------------------------------------------------------------------------

def test_direct_path_creates_direct_edge():
    path_table = [
        {
            "hash": _peer_bytes(PEER_HEX),
            "via":  _peer_bytes(PEER_HEX),
            "hops": 1,
            "timestamp": 0.0,
            "expires": 0.0,
            "interface": "TestIface",
        }
    ]
    rns = _make_rns(path_table=path_table)
    with patch("trenchchat.core.network_map.RNS.Identity.recall", return_value=None):
        data = gather_network_data(rns, SELF_HEX)

    assert any(
        e["src"] == SELF_HEX and e["dst"] == PEER_HEX and e["direct"]
        for e in data["edges"]
    )


def test_multi_hop_path_creates_two_edges():
    """A 2-hop path via a transport node should produce two edges."""
    path_table = [
        {
            "hash": _peer_bytes(PEER_HEX),
            "via":  _peer_bytes(TRANSPORT_HEX),
            "hops": 2,
            "timestamp": 0.0,
            "expires": 0.0,
            "interface": "TestIface",
        }
    ]
    rns = _make_rns(path_table=path_table)
    with patch("trenchchat.core.network_map.RNS.Identity.recall", return_value=None):
        data = gather_network_data(rns, SELF_HEX)

    edge_pairs = {(e["src"], e["dst"]) for e in data["edges"]}
    assert (SELF_HEX, TRANSPORT_HEX) in edge_pairs
    assert (TRANSPORT_HEX, PEER_HEX) in edge_pairs


def test_no_duplicate_edges():
    """Multiple paths through the same transport node should not duplicate the
    self→transport edge."""
    path_table = [
        {
            "hash": _peer_bytes(PEER_HEX),
            "via":  _peer_bytes(TRANSPORT_HEX),
            "hops": 2,
            "timestamp": 0.0,
            "expires": 0.0,
            "interface": "TestIface",
        },
        {
            "hash": _peer_bytes(UNKNOWN_HEX),
            "via":  _peer_bytes(TRANSPORT_HEX),
            "hops": 2,
            "timestamp": 0.0,
            "expires": 0.0,
            "interface": "TestIface",
        },
    ]
    rns = _make_rns(path_table=path_table)
    with patch("trenchchat.core.network_map.RNS.Identity.recall", return_value=None):
        data = gather_network_data(rns, SELF_HEX)

    self_to_transport = [
        e for e in data["edges"]
        if e["src"] == SELF_HEX and e["dst"] == TRANSPORT_HEX
    ]
    assert len(self_to_transport) == 1


# ---------------------------------------------------------------------------
# Interface stats
# ---------------------------------------------------------------------------

def test_interface_stats_included():
    iface_stats = {
        "interfaces": [
            {
                "name": "TCPInterface[Hub/1.2.3.4:4242]",
                "short_name": "Hub",
                "type": "TCPClientInterface",
                "status": True,
                "rxb": 1024,
                "txb": 512,
            }
        ]
    }
    rns = _make_rns(interface_stats=iface_stats)
    with patch("trenchchat.core.network_map.RNS.Identity.recall", return_value=None):
        data = gather_network_data(rns, SELF_HEX)

    assert len(data["interfaces"]) == 1
    iface = data["interfaces"][0]
    assert iface["name"] == "Hub"
    assert iface["status"] is True
    assert iface["rxb"] == 1024


def test_interface_stats_error_returns_empty():
    """If get_interface_stats() raises, interfaces should be an empty list."""
    rns = _make_rns()
    rns.get_interface_stats.side_effect = RuntimeError("rpc error")
    with patch("trenchchat.core.network_map.RNS.Identity.recall", return_value=None):
        data = gather_network_data(rns, SELF_HEX)
    assert data["interfaces"] == []


def test_path_table_error_returns_only_self():
    """If get_path_table() raises, we should still get the self node."""
    rns = _make_rns()
    rns.get_path_table.side_effect = RuntimeError("rpc error")
    with patch("trenchchat.core.network_map.RNS.Identity.recall", return_value=None):
        data = gather_network_data(rns, SELF_HEX)
    assert len(data["nodes"]) == 1
    assert data["nodes"][0]["kind"] == "self"


# ---------------------------------------------------------------------------
# Stats dict
# ---------------------------------------------------------------------------

def test_stats_counts_are_correct():
    path_table = [
        {
            "hash": _peer_bytes(PEER_HEX),
            "via":  _peer_bytes(PEER_HEX),
            "hops": 1,
            "timestamp": 0.0,
            "expires": 0.0,
            "interface": "TestIface",
        }
    ]
    iface_stats = {"interfaces": [
        {"name": "Hub", "short_name": "Hub", "type": "TCP",
         "status": True, "rxb": 0, "txb": 0}
    ]}
    rns = _make_rns(path_table=path_table, interface_stats=iface_stats)
    with patch("trenchchat.core.network_map.RNS.Identity.recall", return_value=None):
        data = gather_network_data(rns, SELF_HEX)

    assert data["stats"]["path_count"] == 1
    assert data["stats"]["interface_count"] == 1
    assert data["stats"]["node_count"] >= 2  # self + peer


def test_large_path_table_is_capped():
    """150 destinations: the node count respects the _MAX_NODES readability cap."""
    from trenchchat.core.network_map import _MAX_NODES

    path_table = [
        {
            "hash": bytes.fromhex(f"{i:032x}"),
            "via":  bytes.fromhex(f"{i:032x}"),
            "hops": 1,
            "timestamp": 0.0,
            "expires": 0.0,
            "interface": "TestIface",
        }
        for i in range(1, 151)
    ]
    rns = _make_rns(path_table=path_table)
    with patch("trenchchat.core.network_map.RNS.Identity.recall", return_value=None):
        data = gather_network_data(rns, SELF_HEX)

    assert len(data["nodes"]) <= _MAX_NODES + 1   # capped entries + self
    assert data["stats"]["path_count"] == 150     # stats still report the real total


def _mixed_topology_data():
    """Direct peers, multi-hop paths via transports, and an interface, with
    every quality-bearing shape the map draws."""
    path_table = []
    for i in range(1, 21):
        dest = bytes.fromhex(f"{i:032x}")
        if i % 3 == 0:
            via = bytes.fromhex(f"{i + 100:032x}")
            hops = 2 + i % 3
        else:
            via = dest
            hops = 1
        path_table.append({
            "hash": dest, "via": via, "hops": hops,
            "timestamp": 0.0, "expires": 0.0, "interface": "TestIface",
        })
    iface_stats = {"interfaces": [
        {"name": "Hub", "short_name": "Hub", "type": "TCP",
         "status": True, "rxb": 0, "txb": 0}
    ]}
    rns = _make_rns(path_table=path_table, interface_stats=iface_stats)
    with patch("trenchchat.core.network_map.RNS.Identity.recall", return_value=None):
        return gather_network_data(rns, SELF_HEX)


def test_every_node_carries_a_quality_tier():
    """The map colors every node by node["quality"]; a missing key silently
    draws grey, so every kind — self, peer, transport, unknown, interface —
    must carry a tier in 0..4."""
    data = _mixed_topology_data()
    kinds_seen = set()
    for node in data["nodes"]:
        kinds_seen.add(node["kind"])
        assert node.get("quality") in range(5), (
            f"node {node['id'][:12]} ({node['kind']}) has quality "
            f"{node.get('quality')!r}"
        )
    assert {"self", "transport", "unknown", "interface"} <= kinds_seen


class _FakeDirectory:
    """UserDirectory stand-in: contains() against a fixed set."""

    def __init__(self, known):
        self._known = set(known)

    def contains(self, peer_hex: str) -> bool:
        return peer_hex in self._known


def test_trenchchat_flag_from_directory_and_member_list():
    """A node is a TrenchChat client if it announced as trenchchat.user or
    sits in a channel member list; other resolvable identities are not."""
    ids = [f"{i:02x}" * 16 for i in (1, 2, 3)]
    path_table = [
        {"hash": bytes.fromhex(h), "via": bytes.fromhex(h), "hops": 1,
         "timestamp": 0.0, "expires": 0.0, "interface": "TestIface"}
        for h in ids
    ]
    rns = _make_rns(path_table=path_table)
    storage = MagicMock()
    storage.get_trenchchat_peer_identities.return_value = {ids[1]}
    storage.get_display_name_for_identity.return_value = None

    def recall(dest_hash, **kwargs):
        identity = MagicMock()
        identity.hash = dest_hash
        return identity

    with patch("trenchchat.core.network_map.RNS.Identity.recall", side_effect=recall):
        data = gather_network_data(rns, SELF_HEX, storage, _FakeDirectory({ids[0]}))

    flags = {n["id"]: n["trenchchat"] for n in data["nodes"]}
    assert flags[SELF_HEX] is True
    assert flags[ids[0]] is True     # trenchchat.user announce
    assert flags[ids[1]] is True     # channel member
    assert flags[ids[2]] is False    # plain LXMF node


def test_every_node_carries_a_trenchchat_flag():
    """The peers-only filter reads node["trenchchat"]; every node must carry
    it, and infrastructure is never flagged."""
    data = _mixed_topology_data()
    for node in data["nodes"]:
        assert isinstance(node.get("trenchchat"), bool), (
            f"node {node['id'][:12]} ({node['kind']}) has trenchchat "
            f"{node.get('trenchchat')!r}"
        )
        if node["kind"] == "interface":
            assert node["trenchchat"] is False
        if node["kind"] == "self":
            assert node["trenchchat"] is True


def test_every_edge_carries_a_quality_tier():
    data = _mixed_topology_data()
    assert data["edges"]
    for edge in data["edges"]:
        assert edge.get("quality") in range(5), (
            f"edge {edge['src'][:12]} -> {edge['dst'][:12]} has quality "
            f"{edge.get('quality')!r}"
        )
