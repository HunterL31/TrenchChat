"""
Unit tests for the network map data-gathering logic.

These tests mock the RNS.Reticulum instance and RNS.Identity.recall so no
real Reticulum stack is needed.
"""

from unittest.mock import MagicMock, patch

from trenchchat.core import network_map
from trenchchat.core.network_map import NetworkMapMonitor, gather_network_data


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
    draws grey, so every kind (self, peer, transport, unknown, interface)
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


# ---------------------------------------------------------------------------
# Per-node detail
# ---------------------------------------------------------------------------

NOMAD_HEX = "ee" * 16
PROP_HEX = "ff" * 16


class _FakePresence:
    """PresenceManager stand-in: is_online() against a fixed set."""

    def __init__(self, online):
        self._online = set(online)

    def is_online(self, peer_hex: str) -> bool:
        return peer_hex in self._online


class _FakeNomad:
    """NodeBrowserManager stand-in: known_nodes() rows keyed node_hash."""

    def __init__(self, nodes):
        self._nodes = list(nodes)

    def known_nodes(self) -> list:
        return [{"node_hash": h} for h in self._nodes]


class _FakePropagation:
    """PropagationNodes stand-in: known_nodes() entries keyed hash."""

    def __init__(self, nodes):
        self._nodes = list(nodes)

    def known_nodes(self) -> list:
        return [{"hash": h, "hops": 1, "last_heard": 0.0, "selected": False}
                for h in self._nodes]


def _entry(dest_hex: str, via_hex: str, hops: int = 1, *,
           timestamp: float = 100.0, expires: float = 900.0,
           interface: str = "TestIface") -> dict:
    return {
        "hash": _peer_bytes(dest_hex),
        "via": _peer_bytes(via_hex),
        "hops": hops,
        "timestamp": timestamp,
        "expires": expires,
        "interface": interface,
    }


def _gather(path_table=None, interface_stats=None, **kwargs) -> dict:
    rns = _make_rns(path_table=path_table, interface_stats=interface_stats)
    with patch("trenchchat.core.network_map.RNS.Identity.recall", return_value=None):
        return gather_network_data(rns, SELF_HEX, **kwargs)


def _node(data: dict, node_id: str) -> dict:
    return next(n for n in data["nodes"] if n["id"] == node_id)


def test_direct_peer_carries_path_detail():
    """A 1-hop path has no relay, so via is None while the interface it was
    learned through and both path timestamps come straight from the entry."""
    data = _gather([_entry(PEER_HEX, PEER_HEX, 1, timestamp=1234.5,
                           expires=5678.5, interface="TesterLink")])

    peer = _node(data, PEER_HEX)
    assert peer["via"] is None
    assert peer["interface"] == "TesterLink"
    assert peer["last_heard"] == 1234.5
    assert peer["expires"] == 5678.5


def test_multi_hop_peer_carries_its_next_hop():
    data = _gather([_entry(PEER_HEX, TRANSPORT_HEX, 2)])
    assert _node(data, PEER_HEX)["via"] == TRANSPORT_HEX


def test_missing_path_keys_degrade_to_none():
    """A path table without timestamps (an older RNS, or an RPC answer that
    dropped them) must still produce a node, with the fields absent."""
    entry = {"hash": _peer_bytes(PEER_HEX), "via": _peer_bytes(PEER_HEX),
             "hops": 1}
    data = _gather([entry])

    peer = _node(data, PEER_HEX)
    assert peer["last_heard"] is None
    assert peer["expires"] is None
    assert peer["interface"] == ""


def test_rtt_is_reported_when_a_link_is_open():
    rns = _make_rns(path_table=[_entry(PEER_HEX, PEER_HEX, 1)])
    with patch("trenchchat.core.network_map.RNS.Identity.recall", return_value=None), \
            patch("trenchchat.core.network_map.rtt_ms_for", return_value=42.0):
        data = gather_network_data(rns, SELF_HEX)

    assert _node(data, PEER_HEX)["rtt_ms"] == 42.0


def test_rtt_is_none_without_a_link():
    data = _gather([_entry(PEER_HEX, PEER_HEX, 1)])
    assert _node(data, PEER_HEX)["rtt_ms"] is None


def test_every_node_carries_an_identity_hex_key():
    """Including self, transports and interfaces, where it is None."""
    data = _gather(
        [_entry(PEER_HEX, TRANSPORT_HEX, 2)],
        {"interfaces": [{"name": "Hub", "short_name": "Hub", "type": "TCP",
                         "status": True, "rxb": 0, "txb": 0}]},
    )
    for node in data["nodes"]:
        assert "identity_hex" in node, f"node {node['id'][:12]} ({node['kind']})"
    assert _node(data, SELF_HEX)["identity_hex"] == SELF_HEX


def test_online_flag_follows_presence():
    ids = [f"{i:02x}" * 16 for i in (1, 2)]
    path_table = [_entry(h, h, 1) for h in ids]

    def recall(dest_hash, **kwargs):
        identity = MagicMock()
        identity.hash = dest_hash
        return identity

    rns = _make_rns(path_table=path_table)
    storage = MagicMock()
    storage.get_trenchchat_peer_identities.return_value = set()
    storage.get_display_name_for_identity.return_value = None
    with patch("trenchchat.core.network_map.RNS.Identity.recall", side_effect=recall):
        data = gather_network_data(rns, SELF_HEX, storage,
                                   presence=_FakePresence({ids[0], SELF_HEX}))

    online = {n["id"]: n["online"] for n in data["nodes"]}
    assert online[ids[0]] is True
    assert online[ids[1]] is False
    assert online[SELF_HEX] is True
    assert data["stats"]["online_peer_count"] == 2


def test_online_is_none_without_a_presence_source():
    data = _gather([_entry(PEER_HEX, PEER_HEX, 1)])
    assert _node(data, PEER_HEX)["online"] is None
    assert _node(data, SELF_HEX)["online"] is None
    assert data["stats"]["online_peer_count"] == 0


def test_online_is_none_for_an_unresolvable_identity():
    """No identity means no presence to look up, even with a presence source."""
    data = _gather([_entry(PEER_HEX, PEER_HEX, 1)],
                   presence=_FakePresence({PEER_HEX}))
    assert _node(data, PEER_HEX)["online"] is None


def test_propagation_and_nomad_flags():
    path_table = [_entry(h, h, 1) for h in (PEER_HEX, NOMAD_HEX, PROP_HEX)]
    data = _gather(path_table, nomad=_FakeNomad([NOMAD_HEX]),
                   propagation=_FakePropagation([PROP_HEX]))

    assert _node(data, NOMAD_HEX)["nomad"] is True
    assert _node(data, NOMAD_HEX)["propagation"] is False
    assert _node(data, PROP_HEX)["propagation"] is True
    assert _node(data, PROP_HEX)["nomad"] is False
    assert _node(data, PEER_HEX)["nomad"] is False
    assert _node(data, PEER_HEX)["propagation"] is False


def test_propagation_and_nomad_accept_plain_hash_sets():
    data = _gather([_entry(NOMAD_HEX, NOMAD_HEX, 1),
                    _entry(PROP_HEX, PROP_HEX, 1)],
                   nomad={NOMAD_HEX}, propagation={PROP_HEX})
    assert _node(data, NOMAD_HEX)["nomad"] is True
    assert _node(data, PROP_HEX)["propagation"] is True


def test_flags_default_false_without_sources():
    data = _gather([_entry(PEER_HEX, PEER_HEX, 1)])
    peer = _node(data, PEER_HEX)
    assert peer["nomad"] is False
    assert peer["propagation"] is False


def test_interface_bitrate_is_reported():
    data = _gather(interface_stats={"interfaces": [
        {"name": "Hub", "short_name": "Hub", "type": "TCP", "status": True,
         "rxb": 0, "txb": 0, "bitrate": 9600},
        {"name": "Radio", "short_name": "Radio", "type": "RNode",
         "status": True, "rxb": 0, "txb": 0},
    ]})

    rates = {i["name"]: i["bitrate"] for i in data["interfaces"]}
    assert rates["Hub"] == 9600
    assert rates["Radio"] is None


def test_every_edge_carries_a_kind():
    """The map draws an interface edge differently from a path edge, so a
    missing kind silently draws the wrong one."""
    data = _mixed_topology_data()
    assert data["edges"]
    for edge in data["edges"]:
        assert edge.get("kind") in ("interface", "path"), (
            f"edge {edge['src'][:12]} -> {edge['dst'][:12]} has kind "
            f"{edge.get('kind')!r}"
        )
    iface_edges = [e for e in data["edges"] if e["dst"].startswith("__iface__")]
    assert iface_edges
    assert all(e["kind"] == "interface" for e in iface_edges)


def test_every_node_carries_the_detail_keys():
    data = _mixed_topology_data()
    keys = {"identity_hex", "via", "interface", "last_heard", "expires",
            "rtt_ms", "online", "nomad", "nomad_node_hash", "propagation"}
    for node in data["nodes"]:
        missing = keys - set(node)
        assert not missing, f"node {node['id'][:12]} ({node['kind']}): {missing}"


# ---------------------------------------------------------------------------
# One identity, several destinations
# ---------------------------------------------------------------------------

NODE_HEX = "1a" * 16          # nomadnetwork.node aspect
DELIVERY_HEX = "2b" * 16      # lxmf.delivery aspect of the same identity
SHARED_IDENTITY_HEX = "3c" * 16


def _shared_identity_recall(mapping: dict[str, str]):
    """recall() stand-in mapping destination hex → identity hex."""

    def recall(dest_hash, **kwargs):
        dest_hex = (dest_hash.hex() if isinstance(dest_hash, bytes)
                    else str(dest_hash))
        identity_hex = mapping.get(dest_hex)
        if identity_hex is None:
            return None
        identity = MagicMock()
        identity.hash = bytes.fromhex(identity_hex)
        return identity

    return recall


def _gather_shared(path_table, recall_map, interface_stats=None, **kwargs) -> dict:
    rns = _make_rns(path_table=path_table, interface_stats=interface_stats)
    with patch("trenchchat.core.network_map.RNS.Identity.recall",
               side_effect=_shared_identity_recall(recall_map)):
        return gather_network_data(rns, SELF_HEX, **kwargs)


def test_nomad_flag_survives_collapse_when_delivery_comes_first():
    """A Nomad node announces lxmf.delivery and nomadnetwork.node under one
    identity; the two collapse into one graph node, which must keep the badge
    and the page hash whichever entry created it."""
    recall_map = {DELIVERY_HEX: SHARED_IDENTITY_HEX,
                  NODE_HEX: SHARED_IDENTITY_HEX}
    data = _gather_shared(
        [_entry(DELIVERY_HEX, DELIVERY_HEX, 1), _entry(NODE_HEX, NODE_HEX, 1)],
        recall_map, nomad=_FakeNomad([NODE_HEX]))

    peers = [n for n in data["nodes"] if n["kind"] == "peer"]
    assert len(peers) == 1
    assert peers[0]["id"] == DELIVERY_HEX
    assert peers[0]["nomad"] is True
    assert peers[0]["nomad_node_hash"] == NODE_HEX


def test_nomad_flag_survives_collapse_when_the_node_entry_comes_first():
    recall_map = {DELIVERY_HEX: SHARED_IDENTITY_HEX,
                  NODE_HEX: SHARED_IDENTITY_HEX}
    data = _gather_shared(
        [_entry(NODE_HEX, NODE_HEX, 1), _entry(DELIVERY_HEX, DELIVERY_HEX, 1)],
        recall_map, nomad=_FakeNomad([NODE_HEX]))

    peers = [n for n in data["nodes"] if n["kind"] == "peer"]
    assert len(peers) == 1
    assert peers[0]["id"] == NODE_HEX
    assert peers[0]["nomad"] is True
    assert peers[0]["nomad_node_hash"] == NODE_HEX


def test_propagation_flag_survives_collapse():
    recall_map = {DELIVERY_HEX: SHARED_IDENTITY_HEX,
                  PROP_HEX: SHARED_IDENTITY_HEX}
    data = _gather_shared(
        [_entry(DELIVERY_HEX, DELIVERY_HEX, 1), _entry(PROP_HEX, PROP_HEX, 1)],
        recall_map, propagation=_FakePropagation([PROP_HEX]))

    peers = [n for n in data["nodes"] if n["kind"] == "peer"]
    assert len(peers) == 1
    assert peers[0]["propagation"] is True
    assert peers[0]["nomad_node_hash"] is None


def test_nomad_node_without_a_path_entry_is_matched_by_identity():
    """The node aspect's path has expired but the browser still knows it; the
    peer it shares an identity with gets the badge and a dialable hash."""
    recall_map = {DELIVERY_HEX: SHARED_IDENTITY_HEX,
                  NODE_HEX: SHARED_IDENTITY_HEX}
    data = _gather_shared([_entry(DELIVERY_HEX, DELIVERY_HEX, 1)],
                          recall_map, nomad=_FakeNomad([NODE_HEX]))

    assert NODE_HEX not in {n["id"] for n in data["nodes"]}
    peer = _node(data, DELIVERY_HEX)
    assert peer["nomad"] is True
    assert peer["nomad_node_hash"] == NODE_HEX


def test_unmatched_nomad_hash_flags_nothing():
    """An unrecallable node hash with no path entry stays off the map."""
    data = _gather_shared([_entry(DELIVERY_HEX, DELIVERY_HEX, 1)],
                          {DELIVERY_HEX: SHARED_IDENTITY_HEX},
                          nomad=_FakeNomad([NODE_HEX]))

    assert all(n["nomad"] is False for n in data["nodes"])
    assert all(n["nomad_node_hash"] is None for n in data["nodes"])


def test_plain_peer_has_no_nomad_node_hash():
    data = _gather([_entry(PEER_HEX, PEER_HEX, 1)])
    assert _node(data, PEER_HEX)["nomad_node_hash"] is None


def test_nomad_node_with_its_own_entry_records_its_own_hash():
    data = _gather([_entry(NOMAD_HEX, NOMAD_HEX, 1)],
                   nomad=_FakeNomad([NOMAD_HEX]))
    assert _node(data, NOMAD_HEX)["nomad_node_hash"] == NOMAD_HEX


# ---------------------------------------------------------------------------
# Multi-hop paths are drawn through their relay
# ---------------------------------------------------------------------------

HUB_ID = "__iface__Hub"
RELAY_VIA_HEX = "4d" * 16         # the destination a path table names as via
RELAY_DELIVERY_HEX = "5e" * 16    # the same relay's lxmf.delivery aspect
RELAY_IDENTITY_HEX = "6f" * 16
OTHER_PEER_HEX = "7a" * 16
OTHER_RELAY_HEX = "8b" * 16


def _hub_stats() -> dict:
    """Interface stats for one hub, whose diamond is HUB_ID."""
    return {"interfaces": [
        {"name": "TCPInterface[Hub/1.2.3.4:4242]", "short_name": "Hub",
         "type": "TCPClientInterface", "status": True, "rxb": 0, "txb": 0},
    ]}


def _pairs(data: dict) -> set[tuple[str, str]]:
    return {(e["src"], e["dst"]) for e in data["edges"]}


def test_multi_hop_peer_hangs_off_its_relay_not_the_interface():
    """The path goes self → interface → relay → peer, one link per real hop,
    so the peer is never drawn as a child of the interface it came through."""
    data = _gather([_entry(PEER_HEX, TRANSPORT_HEX, 2, interface="Hub")],
                   _hub_stats())

    assert _pairs(data) == {(SELF_HEX, HUB_ID), (HUB_ID, TRANSPORT_HEX),
                            (TRANSPORT_HEX, PEER_HEX)}
    relay = _node(data, TRANSPORT_HEX)
    assert relay["kind"] == "transport"
    assert relay["hops"] == 1


def test_the_relay_edge_is_direct_and_the_peer_edge_is_not():
    data = _gather([_entry(PEER_HEX, TRANSPORT_HEX, 3, interface="Hub")],
                   _hub_stats())
    by_pair = {(e["src"], e["dst"]): e for e in data["edges"]}

    relay_edge = by_pair[(HUB_ID, TRANSPORT_HEX)]
    assert relay_edge["hops"] == 1
    assert relay_edge["direct"] is True
    assert relay_edge["kind"] == "path"

    peer_edge = by_pair[(TRANSPORT_HEX, PEER_HEX)]
    assert peer_edge["hops"] == 3
    assert peer_edge["direct"] is False
    assert peer_edge["kind"] == "path"


def test_peers_sharing_a_relay_share_one_relay_node():
    data = _gather([_entry(PEER_HEX, TRANSPORT_HEX, 2, interface="Hub"),
                    _entry(OTHER_PEER_HEX, TRANSPORT_HEX, 3, interface="Hub")],
                   _hub_stats())

    relays = [n for n in data["nodes"] if n["kind"] == "transport"]
    assert [n["id"] for n in relays] == [TRANSPORT_HEX]
    assert _pairs(data) == {(SELF_HEX, HUB_ID), (HUB_ID, TRANSPORT_HEX),
                            (TRANSPORT_HEX, PEER_HEX),
                            (TRANSPORT_HEX, OTHER_PEER_HEX)}


def test_two_relays_behind_one_interface_each_hold_their_own_peers():
    data = _gather([_entry(PEER_HEX, TRANSPORT_HEX, 2, interface="Hub"),
                    _entry(OTHER_PEER_HEX, OTHER_RELAY_HEX, 2, interface="Hub")],
                   _hub_stats())

    assert _pairs(data) == {(SELF_HEX, HUB_ID),
                            (HUB_ID, TRANSPORT_HEX), (TRANSPORT_HEX, PEER_HEX),
                            (HUB_ID, OTHER_RELAY_HEX),
                            (OTHER_RELAY_HEX, OTHER_PEER_HEX)}


def test_a_relay_that_is_also_a_peer_stays_one_node():
    """The relay's own delivery path arrives first, so its node already exists
    when the path through it does: it is reused, drawn as the transport it is,
    and parents the peer behind it."""
    recall_map = {RELAY_DELIVERY_HEX: RELAY_IDENTITY_HEX,
                  RELAY_VIA_HEX: RELAY_IDENTITY_HEX}
    data = _gather_shared(
        [_entry(RELAY_DELIVERY_HEX, RELAY_DELIVERY_HEX, 1, interface="Hub"),
         _entry(PEER_HEX, RELAY_VIA_HEX, 2, interface="Hub")],
        recall_map, _hub_stats())

    assert RELAY_VIA_HEX not in {n["id"] for n in data["nodes"]}
    relay = _node(data, RELAY_DELIVERY_HEX)
    assert relay["kind"] == "transport"
    assert _pairs(data) == {(SELF_HEX, HUB_ID),
                            (HUB_ID, RELAY_DELIVERY_HEX),
                            (RELAY_DELIVERY_HEX, PEER_HEX)}


def test_a_relay_that_is_also_a_peer_stays_one_node_in_either_order():
    """Same topology, the path through the relay listed first: the node the
    relay entry created is the one its delivery path collapses into."""
    recall_map = {RELAY_DELIVERY_HEX: RELAY_IDENTITY_HEX,
                  RELAY_VIA_HEX: RELAY_IDENTITY_HEX}
    data = _gather_shared(
        [_entry(PEER_HEX, RELAY_VIA_HEX, 2, interface="Hub"),
         _entry(RELAY_DELIVERY_HEX, RELAY_DELIVERY_HEX, 1, interface="Hub")],
        recall_map, _hub_stats())

    assert RELAY_DELIVERY_HEX not in {n["id"] for n in data["nodes"]}
    relay = _node(data, RELAY_VIA_HEX)
    assert relay["kind"] == "transport"
    assert _pairs(data) == {(SELF_HEX, HUB_ID),
                            (HUB_ID, RELAY_VIA_HEX), (RELAY_VIA_HEX, PEER_HEX)}


def test_a_relay_with_its_own_path_entry_is_not_also_drawn_off_self():
    """Its 1-hop entry would hang it off self as well, drawing two routes to
    one neighbour; the interface chain owns it in either table order."""
    recall_map = {RELAY_DELIVERY_HEX: RELAY_IDENTITY_HEX,
                  RELAY_VIA_HEX: RELAY_IDENTITY_HEX}
    delivery_first = [
        _entry(RELAY_DELIVERY_HEX, RELAY_DELIVERY_HEX, 1, interface="Hub"),
        _entry(PEER_HEX, RELAY_VIA_HEX, 2, interface="Hub"),
        _entry(OTHER_PEER_HEX, OTHER_PEER_HEX, 1, interface="Hub"),
    ]
    for table in (delivery_first, list(reversed(delivery_first))):
        data = _gather_shared(table, recall_map, _hub_stats())

        relay_id = next(n["id"] for n in data["nodes"]
                        if n["kind"] == "transport")
        pairs = _pairs(data)
        assert (HUB_ID, relay_id) in pairs
        assert (SELF_HEX, relay_id) not in pairs
        assert (relay_id, PEER_HEX) in pairs
        # A 1-hop peer that is nobody's relay keeps its own direct edge.
        assert (SELF_HEX, OTHER_PEER_HEX) in pairs


def test_a_relay_on_an_unknown_interface_hangs_off_self():
    """No interface node to hang the relay from, so self holds it and the
    chain keeps its shape."""
    data = _gather([_entry(PEER_HEX, TRANSPORT_HEX, 2, interface="Elsewhere")],
                   _hub_stats())

    assert _pairs(data) == {(SELF_HEX, HUB_ID), (SELF_HEX, TRANSPORT_HEX),
                            (TRANSPORT_HEX, PEER_HEX)}
    assert _node(data, TRANSPORT_HEX)["kind"] == "transport"


def test_one_hop_peer_keeps_its_direct_edge_to_self():
    """A 1-hop peer has no relay to route through, whatever interface it came
    through, so it hangs off self and adds no transport node."""
    data = _gather([_entry(PEER_HEX, PEER_HEX, 1, interface="Hub")],
                   _hub_stats())

    assert _pairs(data) == {(SELF_HEX, HUB_ID), (SELF_HEX, PEER_HEX)}
    assert next(e for e in data["edges"]
                if e["dst"] == PEER_HEX)["direct"] is True
    assert not [n for n in data["nodes"] if n["kind"] == "transport"]


def test_the_relay_carries_the_same_detail_keys_as_a_peer():
    data = _gather([_entry(PEER_HEX, TRANSPORT_HEX, 2, interface="Hub")],
                   _hub_stats())
    keys = {"identity_hex", "via", "interface", "last_heard", "expires",
            "rtt_ms", "online", "nomad", "nomad_node_hash", "propagation",
            "quality", "trenchchat"}

    assert not keys - set(_node(data, TRANSPORT_HEX))


# ---------------------------------------------------------------------------
# Relevance-ranked selection under the node cap
# ---------------------------------------------------------------------------

ANON_HEXES = [f"{i:02x}" * 16 for i in (0x11, 0x12, 0x13)]
MEMBER_DEST_HEX = "91" * 16
MEMBER_ALT_DEST_HEX = "92" * 16     # second destination of the same identity
MEMBER_IDENTITY_HEX = "93" * 16
ONLINE_DEST_HEX = "94" * 16
ONLINE_IDENTITY_HEX = "95" * 16
FRIEND_DEST_HEX = "96" * 16
FRIEND_IDENTITY_HEX = "97" * 16


def _fake_storage(members=(), friends=()) -> MagicMock:
    storage = MagicMock()
    storage.get_trenchchat_peer_identities.return_value = set(members)
    storage.get_display_name_for_identity.return_value = None
    storage.get_friend_hashes.return_value = set(friends)
    return storage


def _ids(data: dict) -> set[str]:
    return {n["id"] for n in data["nodes"]}


def test_a_channel_member_last_in_the_table_survives_the_cap(monkeypatch):
    """A relearned path is re-inserted at the end of the RNS path table, so
    taking the head drops the peers most recently heard from. The member is
    drawn and the anonymous destinations ahead of it give up their slots."""
    monkeypatch.setattr(network_map, "_MAX_NODES", 2)
    path_table = [_entry(h, h, 1) for h in ANON_HEXES]
    path_table.append(_entry(MEMBER_DEST_HEX, MEMBER_DEST_HEX, 1))

    data = _gather_shared(path_table, {MEMBER_DEST_HEX: MEMBER_IDENTITY_HEX},
                          storage=_fake_storage(members={MEMBER_IDENTITY_HEX}))

    ids = _ids(data)
    assert MEMBER_DEST_HEX in ids
    assert len(ids & set(ANON_HEXES)) == 1


def test_an_online_peer_outranks_an_offline_anonymous_one(monkeypatch):
    monkeypatch.setattr(network_map, "_MAX_NODES", 1)
    path_table = [_entry(ANON_HEXES[0], ANON_HEXES[0], 1),
                  _entry(ONLINE_DEST_HEX, ONLINE_DEST_HEX, 1)]

    data = _gather_shared(path_table, {ONLINE_DEST_HEX: ONLINE_IDENTITY_HEX},
                          presence=_FakePresence({ONLINE_IDENTITY_HEX}))

    assert _ids(data) == {SELF_HEX, ONLINE_DEST_HEX}


def test_an_accepted_friend_outranks_an_anonymous_destination(monkeypatch):
    """Friends rank with channel members, whatever source supplies them."""
    monkeypatch.setattr(network_map, "_MAX_NODES", 1)
    path_table = [_entry(ANON_HEXES[0], ANON_HEXES[0], 1),
                  _entry(FRIEND_DEST_HEX, FRIEND_DEST_HEX, 1)]

    for friends in ({FRIEND_IDENTITY_HEX},
                    _fake_storage(friends={FRIEND_IDENTITY_HEX})):
        data = _gather_shared(path_table,
                              {FRIEND_DEST_HEX: FRIEND_IDENTITY_HEX},
                              friends=friends)
        assert _ids(data) == {SELF_HEX, FRIEND_DEST_HEX}


def test_two_destinations_of_one_identity_cost_one_node_slot(monkeypatch):
    """The cap counts drawn nodes, not path-table entries: a peer answering to
    two destinations collapses into one node and leaves room for another."""
    monkeypatch.setattr(network_map, "_MAX_NODES", 2)
    recall_map = {MEMBER_DEST_HEX: MEMBER_IDENTITY_HEX,
                  MEMBER_ALT_DEST_HEX: MEMBER_IDENTITY_HEX}
    path_table = [_entry(MEMBER_DEST_HEX, MEMBER_DEST_HEX, 1),
                  _entry(MEMBER_ALT_DEST_HEX, MEMBER_ALT_DEST_HEX, 1),
                  _entry(ANON_HEXES[0], ANON_HEXES[0], 1)]

    data = _gather_shared(path_table, recall_map,
                          storage=_fake_storage(members={MEMBER_IDENTITY_HEX}))

    assert _ids(data) == {SELF_HEX, MEMBER_DEST_HEX, ANON_HEXES[0]}


def test_an_admitted_peer_keeps_the_relay_it_travels_through(monkeypatch):
    """The relay is drawn on demand for the peer behind it, so a cap tight
    enough to admit only that peer never leaves it parentless."""
    monkeypatch.setattr(network_map, "_MAX_NODES", 1)
    path_table = [_entry(ANON_HEXES[0], ANON_HEXES[0], 1),
                  _entry(MEMBER_DEST_HEX, TRANSPORT_HEX, 2)]

    data = _gather_shared(path_table, {MEMBER_DEST_HEX: MEMBER_IDENTITY_HEX},
                          storage=_fake_storage(members={MEMBER_IDENTITY_HEX}))

    assert ANON_HEXES[0] not in _ids(data)
    assert _node(data, TRANSPORT_HEX)["kind"] == "transport"
    assert _pairs(data) == {(SELF_HEX, TRANSPORT_HEX),
                            (TRANSPORT_HEX, MEMBER_DEST_HEX)}


def _below_cap_table() -> list[dict]:
    return [_entry(ANON_HEXES[0], ANON_HEXES[0], 1),
            _entry(ANON_HEXES[1], TRANSPORT_HEX, 2),
            _entry(MEMBER_ALT_DEST_HEX, MEMBER_ALT_DEST_HEX, 1),
            _entry(MEMBER_DEST_HEX, MEMBER_DEST_HEX, 1)]


def test_ranking_does_not_reshape_a_table_that_fits():
    """Everything fits, so the graph is the one the table order draws, whatever
    the ranking sources say about the entries in it."""
    recall_map = {MEMBER_DEST_HEX: MEMBER_IDENTITY_HEX,
                  MEMBER_ALT_DEST_HEX: MEMBER_IDENTITY_HEX}
    plain = _gather_shared(_below_cap_table(), recall_map)
    ranked = _gather_shared(
        _below_cap_table(), recall_map,
        storage=_fake_storage(members={MEMBER_IDENTITY_HEX}),
        presence=_FakePresence({MEMBER_IDENTITY_HEX}))

    assert ([n["id"] for n in ranked["nodes"]]
            == [n["id"] for n in plain["nodes"]])
    assert _pairs(ranked) == _pairs(plain)


def test_selection_is_deterministic(monkeypatch):
    monkeypatch.setattr(network_map, "_MAX_NODES", 3)
    path_table = [_entry(f"{i:02x}" * 16, f"{i:02x}" * 16, 1,
                         timestamp=float(i))
                  for i in range(0x20, 0x2a)]
    recall_map = {MEMBER_DEST_HEX: MEMBER_IDENTITY_HEX}
    path_table.append(_entry(MEMBER_DEST_HEX, MEMBER_DEST_HEX, 1))

    runs = [_gather_shared(path_table, recall_map,
                           storage=_fake_storage(members={MEMBER_IDENTITY_HEX}))
            for _ in range(3)]

    assert runs[0] == runs[1] == runs[2]
    assert MEMBER_DEST_HEX in _ids(runs[0])


def test_path_count_reports_the_whole_table_under_the_cap(monkeypatch):
    monkeypatch.setattr(network_map, "_MAX_NODES", 2)
    path_table = [_entry(f"{i:02x}" * 16, f"{i:02x}" * 16, 1)
                  for i in range(0x30, 0x38)]

    data = _gather_shared(path_table, {})

    assert data["stats"]["path_count"] == 8
    assert data["stats"]["node_count"] == 3     # self + the two admitted


# ---------------------------------------------------------------------------
# NetworkMapMonitor
# ---------------------------------------------------------------------------

class _FakeTimer:
    """threading.Timer stand-in the test fires by hand."""

    def __init__(self, delay: float, fn):
        self.delay = delay
        self.fn = fn


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _monitor(interval: float = 2.0):
    clock = _Clock()
    timers: list[_FakeTimer] = []

    def factory(delay, fn):
        timer = _FakeTimer(delay, fn)
        timers.append(timer)
        return timer

    monitor = NetworkMapMonitor(min_interval_secs=interval, clock=clock,
                                timer_factory=factory)
    return monitor, clock, timers


def test_monitor_fires_immediately_when_quiet():
    monitor, _clock, timers = _monitor()
    fired = []
    monitor.add_change_callback(lambda: fired.append(1))

    monitor.note_change()

    assert len(fired) == 1
    assert timers == []


def test_monitor_coalesces_a_burst_into_one_trailing_fire():
    """Ten announces inside the interval are one change, not ten."""
    monitor, clock, timers = _monitor(interval=2.0)
    fired = []
    monitor.add_change_callback(lambda: fired.append(1))

    monitor.note_change()
    for i in range(10):
        clock.now = 0.1 * (i + 1)
        monitor.note_change()

    assert len(fired) == 1
    assert len(timers) == 1
    # Scheduled by the first of the burst, for what was left of the interval.
    assert timers[0].delay == 1.9

    clock.now = 2.0
    timers[0].fn()

    assert len(fired) == 2
    assert len(timers) == 1


def test_monitor_fires_again_once_the_interval_has_passed():
    monitor, clock, timers = _monitor(interval=2.0)
    fired = []
    monitor.add_change_callback(lambda: fired.append(1))

    monitor.note_change()
    clock.now = 5.0
    monitor.note_change()

    assert len(fired) == 2
    assert timers == []


def test_monitor_schedules_the_trailing_fire_for_the_remaining_interval():
    monitor, clock, timers = _monitor(interval=2.0)
    monitor.add_change_callback(lambda: None)

    monitor.note_change()
    clock.now = 1.5
    monitor.note_change()

    assert len(timers) == 1
    assert timers[0].delay == 0.5


def test_monitor_callback_exception_does_not_stop_the_others():
    monitor, _clock, _timers = _monitor()
    fired = []

    def boom():
        raise RuntimeError("callback blew up")

    monitor.add_change_callback(boom)
    monitor.add_change_callback(lambda: fired.append(1))

    monitor.note_change()

    assert fired == [1]
