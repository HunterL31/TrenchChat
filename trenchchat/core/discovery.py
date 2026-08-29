"""
Discovered-interface listing and pinning.

Wraps RNS 1.x interface discovery: reads the running instance's store of
interfaces announced on the mesh, and can pin a discovered network entry
point into the [interfaces] config section. Pins are written as
TCPClientInterface, which is wire-compatible with BackboneInterface servers
and editable in both clients' interface editors.
"""

import RNS

from trenchchat.core.interfaces_config import InterfaceConfigError, write_interface

# Discovered types that describe a network entry point this node can dial.
PINNABLE_TYPES = ("BackboneInterface", "TCPServerInterface")

_LISTED_KEYS = (
    "name", "type", "status", "hops", "value", "discovered", "last_heard",
    "heard_count", "reachable_on", "port", "transport_id", "network_id",
    "latitude", "longitude", "height", "transport", "config_entry",
)

# Best first; anything unrecognized or missing ranks worst.
_STATUS_RANK = {"available": 0, "unknown": 1, "stale": 2}
_WORST_STATUS_RANK = len(_STATUS_RANK)

_UNKNOWN_HOPS = float("inf")


def list_discovered_interfaces() -> list[dict]:
    """Return the RNS instance's discovered interfaces as JSON-safe dicts.

    Sorted best-match first by sort_discovered. Returns an empty list when
    discovery is unavailable or nothing has been discovered yet.
    """
    try:
        discovery = RNS.Discovery.InterfaceDiscovery(discover_interfaces=False)
        raw = discovery.list_discovered_interfaces()
    except Exception as e:
        RNS.log(f"TrenchChat [discovery]: could not list discovered interfaces: {e}",
                RNS.LOG_WARNING)
        return []
    return sort_discovered([_to_jsonable(info) for info in raw])


def sort_discovered(entries: list[dict]) -> list[dict]:
    """Return the entries ordered best match first.

    Pinnable entries come first, then better status, fewer hops, higher stamp
    value and more recent contact; name and discovery hash keep it stable.
    """
    return sorted(entries, key=discovery_sort_key)


def discovery_sort_key(entry: dict) -> tuple:
    """Best-match sort key for a discovered-interface dict."""
    return (
        0 if entry.get("pinnable") else 1,
        _STATUS_RANK.get(entry.get("status"), _WORST_STATUS_RANK),
        _as_number(entry.get("hops"), _UNKNOWN_HOPS),
        -_as_number(entry.get("value"), 0),
        -_as_number(entry.get("last_heard"), 0),
        str(entry.get("name") or ""),
        str(entry.get("discovery_hash") or ""),
    )


def _as_number(value, fallback: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    return float(value)


def _to_jsonable(info: dict) -> dict:
    out: dict = {}
    for key in _LISTED_KEYS:
        if key in info:
            value = info[key]
            out[key] = value.hex() if isinstance(value, bytes) else value
    discovery_hash = info.get("discovery_hash")
    if isinstance(discovery_hash, bytes):
        out["discovery_hash"] = discovery_hash.hex()
    elif discovery_hash is not None:
        out["discovery_hash"] = str(discovery_hash)
    out["pinnable"] = (info.get("type") in PINNABLE_TYPES
                       and bool(info.get("reachable_on")) and bool(info.get("port")))
    return out


def build_pinned_config(entry: dict) -> tuple[str, dict[str, str]]:
    """Build the interface name and config section for pinning a discovered
    network entry point. Raises InterfaceConfigError if the entry is not a
    dialable network interface."""
    if entry.get("type") not in PINNABLE_TYPES:
        raise InterfaceConfigError(
            f"discovered interfaces of type '{entry.get('type')}' cannot be pinned")
    host = str(entry.get("reachable_on") or "")
    port = str(entry.get("port") or "")
    if not host or not port:
        raise InterfaceConfigError("discovered interface has no reachable address")

    name = str(entry.get("name") or f"Discovered {host}")
    cfg = {
        "type": "TCPClientInterface",
        "enabled": "Yes",
        "target_host": host,
        "target_port": port,
    }
    transport_id = entry.get("transport_id")
    if transport_id:
        cfg["transport_identity"] = str(transport_id)
    return name, cfg


def pin_discovered_interface(config_path: str, discovery_hash_hex: str) -> str:
    """Pin the discovered interface with this discovery hash into the config.

    Overwrites a same-named section, so pinning again refreshes the entry.
    Returns the interface name written. Raises InterfaceConfigError if the
    hash is unknown or the entry cannot be pinned.
    """
    for entry in list_discovered_interfaces():
        if entry.get("discovery_hash") == discovery_hash_hex:
            name, cfg = build_pinned_config(entry)
            write_interface(config_path, name, cfg, is_new=False)
            return name
    raise InterfaceConfigError("no such discovered interface")
