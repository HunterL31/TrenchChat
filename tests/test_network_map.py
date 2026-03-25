"""
Unit tests for the network map data-gathering logic.

These tests mock the RNS.Reticulum instance and RNS.Identity.recall so no
real Reticulum stack is needed.  Only the pure data-gathering function
gather_network_data() is tested here — no Qt widgets are instantiated.
"""

import math
from unittest.mock import MagicMock, patch

from trenchchat.gui.network_map import gather_network_data, _fmt_bytes


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
    with patch("trenchchat.gui.network_map.RNS.Identity.recall", return_value=None):
        data = gather_network_data(rns, SELF_HEX)
    ids = {n["id"] for n in data["nodes"]}
    assert SELF_HEX in ids


def test_self_node_kind_is_self():
    rns = _make_rns()
    with patch("trenchchat.gui.network_map.RNS.Identity.recall", return_value=None):
        data = gather_network_data(rns, SELF_HEX)
    self_node = next(n for n in data["nodes"] if n["id"] == SELF_HEX)
    assert self_node["kind"] == "self"
    assert self_node["label"] == "This device"


def test_empty_path_table_returns_only_self():
    rns = _make_rns(path_table=[])
    with patch("trenchchat.gui.network_map.RNS.Identity.recall", return_value=None):
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

    with patch("trenchchat.gui.network_map.RNS.Identity.recall", side_effect=recall):
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

    with patch("trenchchat.gui.network_map.RNS.Identity.recall", return_value=None):
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

    with patch("trenchchat.gui.network_map.RNS.Identity.recall", return_value=None):
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
    with patch("trenchchat.gui.network_map.RNS.Identity.recall", return_value=None):
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
    with patch("trenchchat.gui.network_map.RNS.Identity.recall", return_value=None):
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
    with patch("trenchchat.gui.network_map.RNS.Identity.recall", return_value=None):
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
    with patch("trenchchat.gui.network_map.RNS.Identity.recall", return_value=None):
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
    with patch("trenchchat.gui.network_map.RNS.Identity.recall", return_value=None):
        data = gather_network_data(rns, SELF_HEX)
    assert data["interfaces"] == []


def test_path_table_error_returns_only_self():
    """If get_path_table() raises, we should still get the self node."""
    rns = _make_rns()
    rns.get_path_table.side_effect = RuntimeError("rpc error")
    with patch("trenchchat.gui.network_map.RNS.Identity.recall", return_value=None):
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
    with patch("trenchchat.gui.network_map.RNS.Identity.recall", return_value=None):
        data = gather_network_data(rns, SELF_HEX)

    assert data["stats"]["path_count"] == 1
    assert data["stats"]["interface_count"] == 1
    assert data["stats"]["node_count"] >= 2  # self + peer


# ---------------------------------------------------------------------------
# _fmt_bytes helper
# ---------------------------------------------------------------------------

def test_fmt_bytes_bytes():
    assert _fmt_bytes(500) == "500 B"

def test_fmt_bytes_kb():
    assert "KB" in _fmt_bytes(2048)

def test_fmt_bytes_mb():
    assert "MB" in _fmt_bytes(2 * 1024 * 1024)


# ---------------------------------------------------------------------------
# Dense graph — layout separation
# ---------------------------------------------------------------------------

def test_dense_graph_nodes_do_not_overlap():
    """30 direct peers connected to a single interface should not overlap.

    After the spring layout settles, every pair of nodes must be at least
    one node-diameter apart (using the peer radius as the minimum unit).
    This verifies that the adaptive repulsion/edge-length scaling keeps the
    graph readable with many connections.
    """
    from trenchchat.gui.network_map import _SpringLayout, _REPULSION_BASE, _MIN_EDGE_LEN_BASE, _MIN_EDGE_LEN_MAX

    num_peers = 30
    iface_id = "__iface__TestHub"
    self_id = "aa" * 16
    peer_ids = [f"{i:02x}" * 16 for i in range(1, num_peers + 1)]

    node_ids = [self_id, iface_id] + peer_ids
    n = len(node_ids)
    repulsion = _REPULSION_BASE * (1.0 + n / 15.0)
    min_edge_len = min(_MIN_EDGE_LEN_BASE + n * 4.0, _MIN_EDGE_LEN_MAX)

    edges = (
        [{"src": self_id, "dst": iface_id}]
        + [{"src": iface_id, "dst": pid} for pid in peer_ids]
    )

    layout = _SpringLayout(node_ids, 800.0, 600.0,
                           repulsion=repulsion, min_edge_len=min_edge_len)
    layout.pin(self_id, 400.0, 300.0)
    layout.step(edges, 800.0, 600.0, iterations=200)
    positions = layout.positions()

    min_separation = 9 * 2  # _NODE_R_PEER * 2 — nodes must not visually overlap
    ids = list(positions.keys())
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            ax, ay = positions[ids[i]]
            bx, by = positions[ids[j]]
            dist = math.sqrt((ax - bx) ** 2 + (ay - by) ** 2)
            assert dist >= min_separation, (
                f"Nodes {ids[i][:8]} and {ids[j][:8]} overlap: "
                f"distance {dist:.1f} < {min_separation}"
            )
