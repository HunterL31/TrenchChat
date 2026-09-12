"""
Link quality classification for Reticulum paths.

Quality is assessed from the signals available without requiring an active
link to be open:

  - Hop count      (fewer hops = better)
  - RTT            (from an active RNS link, if one exists to this peer)
  - Path freshness (time remaining before the path entry expires)
  - Physical layer (RSSI / SNR / Q: only meaningful on radio interfaces)

The result is a four-tier enum that maps directly to a display colour in the
network map and will later be used to rank candidates for voice connections.

summarize_channel folds a roster's worth of those readings into the one answer
a channel header needs: how much of the channel this node can reach, and how
far away the reachable part is.
"""

from __future__ import annotations

import time
from enum import IntEnum

import RNS


class LinkQuality(IntEnum):
    """Four-tier link quality classification, ordered best → worst."""
    EXCELLENT = 4   # green: direct, low-latency, fresh path
    GOOD      = 3   # yellow: 1–2 hops or slightly stale
    FAIR      = 2   # orange: multi-hop or high latency
    POOR      = 1   # red: very stale, many hops, or no path data
    UNKNOWN   = 0   # grey: not enough information to classify


# Thresholds
_RTT_EXCELLENT_MS  =  50.0   # ≤ 50 ms
_RTT_GOOD_MS       = 200.0   # ≤ 200 ms
_RTT_FAIR_MS       = 800.0   # ≤ 800 ms

_PATH_FRESH_SECS   = 120     # path expires in > 2 min → fresh
_PATH_STALE_SECS   =  30     # path expires in < 30 s  → stale

_SNR_EXCELLENT_DB  =  10.0
_SNR_GOOD_DB       =   5.0
_SNR_FAIR_DB       =   0.0

_RSSI_EXCELLENT    = -70     # dBm
_RSSI_GOOD         = -85
_RSSI_FAIR         = -100


def rtt_ms_for(dest_hex: str) -> float | None:
    """Return the RTT in milliseconds for an active link to dest_hex, or None."""
    try:
        dest_hash = bytes.fromhex(dest_hex)
        for link in RNS.Transport.active_links:
            try:
                if (link.destination is not None
                        and link.destination.hash == dest_hash
                        and link.rtt is not None):
                    return link.rtt * 1000.0   # RNS stores RTT in seconds
            except Exception:
                continue
    except Exception:
        pass
    return None


def path_ttl_secs(dest_hex: str) -> float | None:
    """Return seconds until the path entry expires, or None if not in table."""
    try:
        dest_hash = bytes.fromhex(dest_hex)
        entry = RNS.Transport.path_table.get(dest_hash)
        if entry is not None:
            expires = entry[3]   # index 3 = expiry timestamp
            return max(0.0, expires - time.time())
    except Exception:
        pass
    return None


def score_path(
    dest_hex: str,
    hops: int,
    via_hex: str | None = None,
    rssi: float | None = None,
    snr: float | None = None,
    q: float | None = None,
) -> LinkQuality:
    """
    Classify the quality of a path to dest_hex.

    Parameters
    ----------
    dest_hex : identity or destination hex of the remote node
    hops     : hop count from the path table
    via_hex  : next-hop hash (None for direct / interface paths)
    rssi     : physical layer RSSI in dBm, if available
    snr      : physical layer SNR in dB, if available
    q        : physical layer link quality 0–1, if available
    """
    # --- physical layer (radio) ---
    if q is not None:
        if q >= 0.8:
            return LinkQuality.EXCELLENT
        if q >= 0.5:
            return LinkQuality.GOOD
        if q >= 0.2:
            return LinkQuality.FAIR
        return LinkQuality.POOR

    if snr is not None:
        if snr >= _SNR_EXCELLENT_DB:
            return LinkQuality.EXCELLENT
        if snr >= _SNR_GOOD_DB:
            return LinkQuality.GOOD
        if snr >= _SNR_FAIR_DB:
            return LinkQuality.FAIR
        return LinkQuality.POOR

    if rssi is not None:
        if rssi >= _RSSI_EXCELLENT:
            return LinkQuality.EXCELLENT
        if rssi >= _RSSI_GOOD:
            return LinkQuality.GOOD
        if rssi >= _RSSI_FAIR:
            return LinkQuality.FAIR
        return LinkQuality.POOR

    # --- RTT from an active link ---
    rtt = rtt_ms_for(dest_hex)
    if rtt is not None:
        if rtt <= _RTT_EXCELLENT_MS and hops <= 1:
            return LinkQuality.EXCELLENT
        if rtt <= _RTT_GOOD_MS:
            return LinkQuality.GOOD
        if rtt <= _RTT_FAIR_MS:
            return LinkQuality.FAIR
        return LinkQuality.POOR

    # --- hop count + path freshness ---
    ttl = path_ttl_secs(dest_hex)

    if hops == 0:
        # Interface / self: always excellent
        return LinkQuality.EXCELLENT

    if hops == 1:
        if ttl is None or ttl >= _PATH_FRESH_SECS:
            return LinkQuality.EXCELLENT
        if ttl >= _PATH_STALE_SECS:
            return LinkQuality.GOOD
        return LinkQuality.FAIR

    if hops == 2:
        if ttl is not None and ttl >= _PATH_FRESH_SECS:
            return LinkQuality.GOOD
        return LinkQuality.FAIR

    # 3+ hops
    if ttl is not None and ttl >= _PATH_FRESH_SECS:
        return LinkQuality.FAIR
    return LinkQuality.POOR


def quality_label(quality: LinkQuality) -> str:
    """Short human-readable label for a quality tier."""
    return {
        LinkQuality.EXCELLENT: "Excellent",
        LinkQuality.GOOD:      "Good",
        LinkQuality.FAIR:      "Fair",
        LinkQuality.POOR:      "Poor",
        LinkQuality.UNKNOWN:   "Unknown",
    }[quality]


def _lower_median(values: list[int]) -> int:
    """Middle value, taking the lower of the two when the count is even."""
    ordered = sorted(values)
    return ordered[(len(ordered) - 1) // 2]


def summarize_channel(peers: list[dict]) -> dict:
    """Fold per-peer link readings into one answer for a whole channel.

    A channel send is unicast to every member, so the headline is how many of
    them this node has a path to at all: a member with none is queued for
    retry rather than reached. Over the members that are reachable, the hop
    count and the quality tier are medians rather than means. Hops are
    ordinal, a member with no path has no hop count to average in, and one
    member eleven hops out would drag the mean of 1, 1, 1, 11 to 3.5 while
    three quarters of the channel is a single hop away. The best-scored peer
    is kept as a detail, not the headline: the old best-peer reading showed
    full bars while every other member was unreachable.

    Each entry is expected to carry at least "quality" and "hops", with hops
    None when no path is known. Pure: it asks the network nothing.
    """
    reachable = [p for p in peers if p.get("hops") is not None]
    if not reachable:
        return {
            "level": int(LinkQuality.UNKNOWN),
            "level_label": quality_label(LinkQuality.UNKNOWN),
            "reachable": 0,
            "total": len(peers),
            "median_hops": None,
            "best_identity_hash": None,
            "best_hops": None,
        }

    level = LinkQuality(_lower_median([int(p.get("quality", 0)) for p in reachable]))
    best = min(reachable, key=lambda p: (-int(p.get("quality", 0)), int(p["hops"])))
    return {
        "level": int(level),
        "level_label": quality_label(level),
        "reachable": len(reachable),
        "total": len(peers),
        "median_hops": _lower_median([int(p["hops"]) for p in reachable]),
        "best_identity_hash": best.get("identity_hash"),
        "best_hops": int(best["hops"]),
    }
