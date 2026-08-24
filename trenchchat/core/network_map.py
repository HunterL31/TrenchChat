"""
Reticulum topology gathering for the network map.

Pure data functions with no Qt dependency, so every frontend can use them:
the Qt dialog in trenchchat/gui/network_map.py, and the headless testenv API
that backs the Flutter client. Importing the Qt module instead would drag
PyQt6 into environments that deliberately do not install it.
"""

import RNS

from trenchchat.core.protocol import unpack_wire

from trenchchat.core.link_quality import LinkQuality, score_path

_MAX_NODES = 120   # cap to keep the graph readable


# ---------------------------------------------------------------------------
# Data gathering (pure functions — testable without Qt)
# ---------------------------------------------------------------------------

def gather_network_data(rns: RNS.Reticulum, self_hex: str,
                        storage=None, directory=None) -> dict:
    """
    Query the RNS instance for the current network topology.

    storage   — optional Storage instance; when provided, peer nodes are labelled
                with the display name stored in the members table (i.e. the name
                seen in channel member lists) in preference to the announce app_data.
    directory — optional UserDirectory; identities it contains (fed by
                trenchchat.user announces) are marked as TrenchChat clients.

    Returns a dict with keys:
      nodes  — list[dict]: id, label, kind ('self'|'transport'|'peer'|'unknown'),
               hops, quality, trenchchat (bool: known TrenchChat client)
      edges  — list[dict]: src, dst, hops, direct (bool)
      interfaces — list[dict]: name, type, status, rxb, txb
      stats  — dict: node_count, path_count, interface_count
    """
    nodes: dict[str, dict] = {}   # hash_hex -> node dict
    edges: list[dict] = []

    # --- self node ---
    nodes[self_hex] = {
        "id":         self_hex,
        "label":      "This device",
        "kind":       "self",
        "hops":       0,
        "quality":    int(LinkQuality.EXCELLENT),
        "trenchchat": True,
    }

    # --- path table ---
    try:
        path_table = rns.get_path_table()
    except Exception:
        path_table = []

    # Identities in any channel member list are TrenchChat clients; fetched
    # once and combined with the announce directory in _is_trenchchat.
    member_identities: set[str] = set()
    if storage is not None:
        try:
            member_identities = storage.get_trenchchat_peer_identities()
        except Exception:
            member_identities = set()

    # Fetch interface stats once; reused both for routing multi-hop peers through
    # the correct interface diamond node and for drawing the interface nodes.
    try:
        _iface_stats: dict = rns.get_interface_stats()
    except Exception:
        _iface_stats = {}

    # Build a lookup from any interface name variant → synthetic node ID.
    # The path table uses the full name (e.g. "TCPInterface[TrenchChat Hub/…]")
    # while interface stats expose both a short_name ("TrenchChat Hub") and the
    # full name.  Index both so the match always works.
    iface_name_to_id: dict[str, str] = {}
    for iface in _iface_stats.get("interfaces", []):
        short = iface.get("short_name") or ""
        full  = iface.get("name") or ""
        node_id = f"__iface__{short or full}"
        if short:
            iface_name_to_id[short] = node_id
        if full:
            iface_name_to_id[full] = node_id

    # Collect all transport (next-hop) hashes so we can classify them.
    # Only count a via-hash as a transport node when it differs from the
    # destination itself (i.e. it is a true relay, not a direct 1-hop peer).
    transport_hashes: set[str] = set()
    for entry in path_table:
        dest_h = entry.get("hash")
        via = entry.get("via")
        if via and dest_h:
            via_hex = via.hex() if isinstance(via, bytes) else str(via)
            dest_h_hex = dest_h.hex() if isinstance(dest_h, bytes) else str(dest_h)
            if via_hex != self_hex and via_hex != dest_h_hex:
                transport_hashes.add(via_hex)

    seen_pairs: set[tuple] = set()
    # Maps identity_hex → the canonical node ID already added for that identity.
    # Multiple RNS destinations (lxmf.delivery, trenchchat.channel, …) can share
    # the same underlying identity; we collapse them into one graph node.
    identity_to_node: dict[str, str] = {self_hex: self_hex}

    for entry in path_table[:_MAX_NODES]:
        dest_hash = entry.get("hash")
        if dest_hash is None:
            continue
        dest_hex = dest_hash.hex() if isinstance(dest_hash, bytes) else str(dest_hash)
        hops = entry.get("hops", 0)
        via = entry.get("via")
        via_hex = via.hex() if isinstance(via, bytes) else (str(via) if via else None)
        # The interface name this path was learned through (may be None/empty)
        path_iface: str = entry.get("interface") or ""

        # Resolve the identity behind this destination.
        identity = RNS.Identity.recall(dest_hash if isinstance(dest_hash, bytes)
                                       else bytes.fromhex(dest_hex))
        identity_hex: str | None = identity.hash.hex() if identity is not None else None

        # If we already have a node for this identity, reuse it — skip adding a
        # duplicate node and redirect edges to the canonical one.
        if identity_hex and identity_hex in identity_to_node:
            canonical_id = identity_to_node[identity_hex]
        else:
            canonical_id = dest_hex
            # Classify
            if dest_hex == self_hex:
                kind = "self"
            elif dest_hex in transport_hashes:
                kind = "transport"
            elif identity is not None:
                kind = "peer"
            else:
                kind = "unknown"

            if dest_hex not in nodes:
                nodes[dest_hex] = {
                    "id":           dest_hex,
                    "identity_hex": identity_hex,
                    "label":        _make_label(dest_hex, identity, kind, storage),
                    "kind":         kind,
                    "hops":         hops,
                    "trenchchat":   _is_trenchchat(identity_hex, member_identities, directory),
                }
            elif identity_hex and nodes[dest_hex].get("identity_hex") is None:
                # A later path-table entry resolved the identity — backfill it.
                nodes[dest_hex]["identity_hex"] = identity_hex
                nodes[dest_hex]["label"] = _make_label(dest_hex, identity, kind, storage)
                nodes[dest_hex]["trenchchat"] = _is_trenchchat(
                    identity_hex, member_identities, directory)
            if identity_hex:
                identity_to_node[identity_hex] = dest_hex

        # Determine the relay node to route through for multi-hop paths.
        # Prefer the interface diamond node (if the path came through a known
        # interface) over creating a separate floating transport hash node.
        relay_id: str | None = None
        if via_hex and via_hex != self_hex and hops > 1:
            iface_node_id = iface_name_to_id.get(path_iface)
            if iface_node_id:
                relay_id = iface_node_id          # anchor to interface diamond
            else:
                relay_id = via_hex                # fall back to transport hash node
                if relay_id not in nodes:
                    via_identity = RNS.Identity.recall(bytes.fromhex(relay_id))
                    via_identity_hex = (via_identity.hash.hex()
                                        if via_identity is not None else None)
                    nodes[relay_id] = {
                        "id":         relay_id,
                        "label":      _make_label(relay_id, via_identity, "transport", storage),
                        "kind":       "transport",
                        "hops":       1,
                        "quality":    int(score_path(relay_id, 1, None)),
                        "trenchchat": _is_trenchchat(via_identity_hex,
                                                     member_identities, directory),
                    }

        # Score the quality of the path to this destination
        quality = score_path(dest_hex, hops, via_hex)
        quality_val = int(quality)

        if relay_id:
            # self → relay
            pair = (self_hex, relay_id)
            if pair not in seen_pairs:
                seen_pairs.add(pair)
                relay_quality = int(score_path(relay_id, 1, None))
                edges.append({"src": self_hex, "dst": relay_id, "hops": 1, "direct": True,
                               "kind": "interface" if relay_id.startswith("__iface__") else "path",
                               "quality": relay_quality})
            # relay → canonical peer node
            pair2 = (relay_id, canonical_id)
            if pair2 not in seen_pairs:
                seen_pairs.add(pair2)
                edges.append({"src": relay_id, "dst": canonical_id,
                               "hops": hops, "direct": False,
                               "quality": quality_val})
        else:
            pair = (self_hex, canonical_id)
            if pair not in seen_pairs:
                seen_pairs.add(pair)
                edges.append({"src": self_hex, "dst": canonical_id,
                               "hops": hops, "direct": hops <= 1,
                               "quality": quality_val})

        # Propagate the best quality score seen for this node
        if canonical_id in nodes:
            existing_q = nodes[canonical_id].get("quality", 0)
            nodes[canonical_id]["quality"] = max(existing_q, quality_val)

    # --- interface stats + interface nodes ---
    interfaces: list[dict] = []
    try:
        stats = _iface_stats  # already fetched above; reuse to avoid a second RPC
        for iface in stats.get("interfaces", []):
            iface_name = iface.get("short_name") or iface.get("name", "?")
            iface_type = iface.get("type", "")
            iface_status = iface.get("status", False)
            rxb = iface.get("rxb", 0)
            txb = iface.get("txb", 0)
            interfaces.append({
                "name":   iface_name,
                "type":   iface_type,
                "status": iface_status,
                "rxb":    rxb,
                "txb":    txb,
            })

            # Add the interface as a graph node so it appears on the map.
            # Use a stable synthetic ID so the layout doesn't reset on refresh.
            iface_id = f"__iface__{iface_name}"
            status_dot = "●" if iface_status else "○"
            type_short = (iface_type
                          .replace("ClientInterface", "")
                          .replace("Interface", "")
                          .strip())
            label = f"{status_dot} {iface_name}"
            if type_short:
                label += f" ({type_short})"
            iface_quality = int(
                LinkQuality.EXCELLENT if iface_status else LinkQuality.POOR
            )
            nodes[iface_id] = {
                "id":         iface_id,
                "label":      label,
                "kind":       "interface",
                "hops":       0,
                "quality":    iface_quality,
                "trenchchat": False,
            }
            # Edge: self → interface
            pair = (self_hex, iface_id)
            if pair not in seen_pairs:
                seen_pairs.add(pair)
                edges.append({
                    "src":     self_hex,
                    "dst":     iface_id,
                    "hops":    0,
                    "direct":  True,
                    "kind":    "interface",
                    "quality": iface_quality,
                })
    except Exception:
        pass

    return {
        "nodes":      list(nodes.values()),
        "edges":      edges,
        "interfaces": interfaces,
        "stats": {
            "node_count":      len(nodes),
            "path_count":      len(path_table),
            "interface_count": len(interfaces),
        },
    }


def _is_trenchchat(identity_hex: str | None, member_identities: set[str],
                   directory) -> bool:
    """True when the identity is a known TrenchChat client: it announced as a
    trenchchat.user, or it appears in a channel member list we hold."""
    if identity_hex is None:
        return False
    if identity_hex in member_identities:
        return True
    if directory is not None:
        try:
            return bool(directory.contains(identity_hex))
        except Exception:
            pass
    return False


def _make_label(hex_id: str, identity, kind: str, storage=None) -> str:
    """Build a short human-readable label for a node.

    Preference order:
      1. Display name from the members table (name seen in channel member lists)
      2. Display name from LXMF announce app_data
      3. Identity hash prefix (matches what the rest of TrenchChat shows)
      4. Destination hash prefix (fallback when identity is unknown)
    """
    if kind == "self":
        return "This device"
    identity_hex: str | None = None
    if identity is not None:
        identity_hex = identity.hash.hex()

    # 1. Storage lookup — name from any channel's member list
    if storage is not None and identity_hex is not None:
        try:
            stored_name = storage.get_display_name_for_identity(identity_hex)
            if stored_name:
                return stored_name
        except Exception:
            pass

    # 2. LXMF announce app_data
    # LXMF encodes app_data as a msgpack list: [display_name_bytes, stamp_cost]
    if identity is not None:
        try:
            raw = RNS.Identity.recall_app_data(
                RNS.Destination.hash(identity.hash, "lxmf", "delivery")
            )
            if raw:
                # unpack_wire rather than a bare unpackb: this is announce
                # app_data off the network, and every other ingest in the
                # codebase goes through the same bounds.
                parsed = unpack_wire(raw)
                # List format: [display_name, stamp_cost, ...]
                if isinstance(parsed, list) and len(parsed) >= 1:
                    app_data = parsed[0]
                elif isinstance(parsed, dict):
                    app_data = parsed.get("display_name") or parsed.get("name")
                else:
                    app_data = None
                if isinstance(app_data, bytes):
                    app_data = app_data.decode(errors="replace")
                if app_data:
                    return str(app_data)
        except Exception:
            pass

    # 3 & 4. Hash prefix fallback
    fallback = identity_hex if identity_hex else hex_id
    return fallback[:12] + "…"
