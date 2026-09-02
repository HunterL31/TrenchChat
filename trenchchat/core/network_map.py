"""
Reticulum topology gathering for the network map.

Pure data functions consumed by the testenv API that backs the Flutter
client, plus a debounced notifier the API turns into a change event.
"""

import threading
import time
from typing import Callable

import RNS

from trenchchat.core.protocol import unpack_wire

from trenchchat.core.link_quality import LinkQuality, rtt_ms_for, score_path

_MAX_NODES = 120   # cap to keep the graph readable

# Announces, presence transitions and link changes all touch the map and all
# arrive in bursts; this is the shortest gap between two change notifications.
CHANGE_MIN_INTERVAL_SECS = 2.0


# ---------------------------------------------------------------------------
# Data gathering (pure functions)
# ---------------------------------------------------------------------------

def gather_network_data(rns: RNS.Reticulum, self_hex: str,
                        storage=None, directory=None, presence=None,
                        propagation=None, nomad=None) -> dict:
    """
    Query the RNS instance for the current network topology.

    storage: optional Storage instance; when provided, peer nodes are labelled
                with the display name stored in the members table (i.e. the name
                seen in channel member lists) in preference to the announce app_data.
    directory: optional UserDirectory; identities it contains (fed by
                trenchchat.user announces) are marked as TrenchChat clients.
    presence: optional PresenceManager; drives each node's online flag, which
                is None for every node without one.
    propagation: optional PropagationNodes (or a set of destination hexes);
                its nodes are flagged as LXMF propagation nodes.
    nomad: optional NodeBrowserManager (or a set of destination hexes);
                its nodes are flagged as Nomad Network nodes.

    Returns a dict with keys:
      nodes: list[dict] with id, identity_hex, label,
               kind ('self'|'transport'|'peer'|'unknown'|'interface'), hops,
               quality, trenchchat, via, interface, last_heard, expires,
               rtt_ms, online, nomad, propagation
      edges: list[dict] with src, dst, hops, direct (bool), quality,
               kind ('interface'|'path')
      interfaces: list[dict] with name, type, status, rxb, txb, bitrate
      stats: dict with node_count, path_count, interface_count, online_peer_count
    """
    nodes: dict[str, dict] = {}   # hash_hex -> node dict
    edges: list[dict] = []

    propagation_hashes = _propagation_hashes(propagation)
    nomad_hashes = _nomad_hashes(nomad)

    # --- self node ---
    nodes[self_hex] = {
        "id":           self_hex,
        "identity_hex": self_hex,
        "label":        "This device",
        "kind":         "self",
        "hops":         0,
        "quality":      int(LinkQuality.EXCELLENT),
        "trenchchat":   True,
        "via":          None,
        "interface":    None,
        "last_heard":   None,
        "expires":      None,
        "rtt_ms":       None,
        "online":       _online_for(self_hex, presence),
        "nomad":        self_hex in nomad_hashes,
        "propagation":  self_hex in propagation_hashes,
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
        path_iface: str = _coerce_text(entry.get("interface"))
        # A via that is the destination itself is a direct path, not a relay.
        next_hop = via_hex if (via_hex and via_hex != dest_hex
                               and via_hex != self_hex) else None

        # Resolve the identity behind this destination.
        identity = RNS.Identity.recall(dest_hash if isinstance(dest_hash, bytes)
                                       else bytes.fromhex(dest_hex))
        identity_hex: str | None = identity.hash.hex() if identity is not None else None

        # If we already have a node for this identity, reuse it, skip adding a
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
                    "via":          next_hop,
                    "interface":    path_iface,
                    "last_heard":   _as_float(entry.get("timestamp")),
                    "expires":      _as_float(entry.get("expires")),
                    "rtt_ms":       rtt_ms_for(dest_hex),
                    "online":       _online_for(identity_hex, presence),
                    "nomad":        dest_hex in nomad_hashes,
                    "propagation":  dest_hex in propagation_hashes,
                }
            elif identity_hex and nodes[dest_hex].get("identity_hex") is None:
                # A later path-table entry resolved the identity, backfill it.
                nodes[dest_hex]["identity_hex"] = identity_hex
                nodes[dest_hex]["label"] = _make_label(dest_hex, identity, kind, storage)
                nodes[dest_hex]["trenchchat"] = _is_trenchchat(
                    identity_hex, member_identities, directory)
                nodes[dest_hex]["online"] = _online_for(identity_hex, presence)
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
                        "id":           relay_id,
                        "identity_hex": via_identity_hex,
                        "label":        _make_label(relay_id, via_identity,
                                                    "transport", storage),
                        "kind":         "transport",
                        "hops":         1,
                        "quality":      int(score_path(relay_id, 1, None)),
                        "trenchchat":   _is_trenchchat(via_identity_hex,
                                                       member_identities, directory),
                        "via":          None,
                        "interface":    path_iface,
                        "last_heard":   None,
                        "expires":      None,
                        "rtt_ms":       rtt_ms_for(relay_id),
                        "online":       _online_for(via_identity_hex, presence),
                        "nomad":        relay_id in nomad_hashes,
                        "propagation":  relay_id in propagation_hashes,
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
                               "kind": "path", "quality": quality_val})
        else:
            pair = (self_hex, canonical_id)
            if pair not in seen_pairs:
                seen_pairs.add(pair)
                edges.append({"src": self_hex, "dst": canonical_id,
                               "hops": hops, "direct": hops <= 1,
                               "kind": "path", "quality": quality_val})

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
                "name":    iface_name,
                "type":    iface_type,
                "status":  iface_status,
                "rxb":     rxb,
                "txb":     txb,
                "bitrate": _as_int(iface.get("bitrate")),
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
                "id":           iface_id,
                "identity_hex": None,
                "label":        label,
                "kind":         "interface",
                "hops":         0,
                "quality":      iface_quality,
                "trenchchat":   False,
                "via":          None,
                "interface":    iface_name,
                "last_heard":   None,
                "expires":      None,
                "rtt_ms":       None,
                "online":       None,
                "nomad":        False,
                "propagation":  False,
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
            "node_count":        len(nodes),
            "path_count":        len(path_table),
            "interface_count":   len(interfaces),
            "online_peer_count": sum(1 for n in nodes.values()
                                     if n.get("online") is True),
        },
    }


def _coerce_text(value) -> str:
    """An interface name off the path table, as text ("" when absent)."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


def _as_float(value) -> float | None:
    """A path-table timestamp as a float, or None when absent or malformed."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value) -> int | None:
    """An interface bitrate as an int, or None when absent or malformed."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _online_for(identity_hex: str | None, presence) -> bool | None:
    """Presence for an identity; None when unknown or no presence source."""
    if presence is None or identity_hex is None:
        return None
    try:
        return bool(presence.is_online(identity_hex))
    except Exception:
        return None


def _propagation_hashes(source) -> set[str]:
    """Destination hexes of every known LXMF propagation node.

    Takes a PropagationNodes, or any iterable of hexes.
    """
    if source is None:
        return set()
    try:
        if hasattr(source, "known_nodes"):
            return {str(entry["hash"]) for entry in source.known_nodes()}
        return {str(node_hex) for node_hex in source}
    except Exception:
        return set()


def _nomad_hashes(source) -> set[str]:
    """Destination hexes of every known Nomad Network node.

    Takes a NodeBrowserManager, or any iterable of hexes.
    """
    if source is None:
        return set()
    try:
        if hasattr(source, "known_nodes"):
            return {str(row["node_hash"]) for row in source.known_nodes()}
        return {str(node_hex) for node_hex in source}
    except Exception:
        return set()


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

    # 1. Storage lookup: name from any channel's member list
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


def _start_timer(delay: float, fn: Callable[[], None]):
    timer = threading.Timer(delay, fn)
    timer.daemon = True
    timer.start()
    return timer


class NetworkMapMonitor:
    """Tells a client the map has changed, without telling it constantly.

    Every announce, presence transition, link change and discovered node moves
    the topology, and they arrive in bursts: a single peer coming up fires
    several within a second. note_change() is safe to call from any RNS thread
    and fires at most once per interval: immediately when quiet, and once more
    after the interval for anything that arrived during it, however much did.
    """

    def __init__(self, min_interval_secs: float = CHANGE_MIN_INTERVAL_SECS,
                 clock: Callable[[], float] | None = None,
                 timer_factory: Callable[[float, Callable[[], None]], object] | None = None
                 ) -> None:
        self._min_interval = min_interval_secs
        self._clock = clock or time.monotonic
        self._timer_factory = timer_factory or _start_timer
        self._callbacks: list[Callable[[], None]] = []
        self._lock = threading.Lock()
        self._last_fire: float | None = None
        self._timer = None

    def add_change_callback(self, cb: Callable[[], None]) -> None:
        """Register a callback invoked (with no arguments) on a change."""
        self._callbacks.append(cb)

    def note_change(self) -> None:
        """Note that the topology moved. Safe to call from any thread."""
        now = self._clock()
        with self._lock:
            if self._timer is not None:
                return
            if (self._last_fire is not None
                    and now - self._last_fire < self._min_interval):
                delay = self._min_interval - (now - self._last_fire)
                self._timer = self._timer_factory(delay, self._on_timer)
                return
            self._last_fire = now
        self._fire()

    def _on_timer(self) -> None:
        with self._lock:
            self._timer = None
            self._last_fire = self._clock()
        self._fire()

    def _fire(self) -> None:
        for cb in list(self._callbacks):
            try:
                cb()
            except Exception as e:
                RNS.log(f"TrenchChat [netmap]: change callback error: {e}",
                        RNS.LOG_ERROR)
