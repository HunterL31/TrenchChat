"""
Tests for trenchchat.core.link_quality.

All tests are pure-logic: RNS.Transport is patched so no network stack is needed.
"""

from unittest.mock import patch, MagicMock
import time
import pytest

from trenchchat.core.link_quality import (
    LinkQuality, score_path, quality_label,
    _RTT_EXCELLENT_MS, _RTT_GOOD_MS, _RTT_FAIR_MS,
    _PATH_FRESH_SECS, _PATH_STALE_SECS,
)

DEST_HEX = "aa" * 16   # 32-char fake hex


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_no_links():
    """Patch Transport so no active links and no path table entries exist."""
    return patch("trenchchat.core.link_quality.RNS.Transport",
                 active_links=[], path_table={})


def _mock_path_table(dest_hex: str, ttl_secs: float):
    """Patch Transport with a path table entry expiring in ttl_secs."""
    dest_bytes = bytes.fromhex(dest_hex)
    expires = time.time() + ttl_secs
    path_table = {dest_bytes: (0, None, 1, expires, None, None)}
    return patch("trenchchat.core.link_quality.RNS.Transport",
                 active_links=[], path_table=path_table)


def _mock_active_link(dest_hex: str, rtt_secs: float):
    """Patch Transport with an active link whose RTT is rtt_secs."""
    dest_bytes = bytes.fromhex(dest_hex)
    link = MagicMock()
    link.destination = MagicMock()
    link.destination.hash = dest_bytes
    link.rtt = rtt_secs
    return patch("trenchchat.core.link_quality.RNS.Transport",
                 active_links=[link], path_table={})


# ---------------------------------------------------------------------------
# Physical layer signals
# ---------------------------------------------------------------------------

class TestPhysicalLayer:
    def test_q_excellent(self):
        with _mock_no_links():
            assert score_path(DEST_HEX, 1, q=0.9) == LinkQuality.EXCELLENT

    def test_q_good(self):
        with _mock_no_links():
            assert score_path(DEST_HEX, 1, q=0.6) == LinkQuality.GOOD

    def test_q_fair(self):
        with _mock_no_links():
            assert score_path(DEST_HEX, 1, q=0.3) == LinkQuality.FAIR

    def test_q_poor(self):
        with _mock_no_links():
            assert score_path(DEST_HEX, 1, q=0.1) == LinkQuality.POOR

    def test_snr_excellent(self):
        with _mock_no_links():
            assert score_path(DEST_HEX, 1, snr=15.0) == LinkQuality.EXCELLENT

    def test_snr_poor(self):
        with _mock_no_links():
            assert score_path(DEST_HEX, 1, snr=-5.0) == LinkQuality.POOR

    def test_rssi_excellent(self):
        with _mock_no_links():
            assert score_path(DEST_HEX, 1, rssi=-60) == LinkQuality.EXCELLENT

    def test_rssi_poor(self):
        with _mock_no_links():
            assert score_path(DEST_HEX, 1, rssi=-110) == LinkQuality.POOR

    def test_q_takes_priority_over_snr(self):
        # q=0.1 (POOR) should win over snr=20 (EXCELLENT)
        with _mock_no_links():
            assert score_path(DEST_HEX, 1, q=0.1, snr=20.0) == LinkQuality.POOR


# ---------------------------------------------------------------------------
# RTT from active links
# ---------------------------------------------------------------------------

class TestRTT:
    def test_rtt_excellent(self):
        rtt_secs = (_RTT_EXCELLENT_MS - 10) / 1000.0
        with _mock_active_link(DEST_HEX, rtt_secs):
            assert score_path(DEST_HEX, 1) == LinkQuality.EXCELLENT

    def test_rtt_good(self):
        rtt_secs = (_RTT_GOOD_MS - 10) / 1000.0
        with _mock_active_link(DEST_HEX, rtt_secs):
            assert score_path(DEST_HEX, 2) == LinkQuality.GOOD

    def test_rtt_fair(self):
        rtt_secs = (_RTT_FAIR_MS - 10) / 1000.0
        with _mock_active_link(DEST_HEX, rtt_secs):
            assert score_path(DEST_HEX, 2) == LinkQuality.FAIR

    def test_rtt_poor(self):
        rtt_secs = (_RTT_FAIR_MS + 200) / 1000.0
        with _mock_active_link(DEST_HEX, rtt_secs):
            assert score_path(DEST_HEX, 3) == LinkQuality.POOR

    def test_rtt_excellent_requires_1_hop(self):
        # Low RTT but 2 hops → GOOD, not EXCELLENT
        rtt_secs = (_RTT_EXCELLENT_MS - 10) / 1000.0
        with _mock_active_link(DEST_HEX, rtt_secs):
            assert score_path(DEST_HEX, 2) == LinkQuality.GOOD


# ---------------------------------------------------------------------------
# Hop count + path freshness
# ---------------------------------------------------------------------------

class TestHopsAndFreshness:
    def test_zero_hops_excellent(self):
        with _mock_no_links():
            assert score_path(DEST_HEX, 0) == LinkQuality.EXCELLENT

    def test_one_hop_fresh_excellent(self):
        with _mock_path_table(DEST_HEX, _PATH_FRESH_SECS + 60):
            assert score_path(DEST_HEX, 1) == LinkQuality.EXCELLENT

    def test_one_hop_slightly_stale_good(self):
        with _mock_path_table(DEST_HEX, (_PATH_STALE_SECS + _PATH_FRESH_SECS) / 2):
            assert score_path(DEST_HEX, 1) == LinkQuality.GOOD

    def test_one_hop_very_stale_fair(self):
        with _mock_path_table(DEST_HEX, _PATH_STALE_SECS / 2):
            assert score_path(DEST_HEX, 1) == LinkQuality.FAIR

    def test_two_hops_fresh_good(self):
        with _mock_path_table(DEST_HEX, _PATH_FRESH_SECS + 60):
            assert score_path(DEST_HEX, 2) == LinkQuality.GOOD

    def test_two_hops_stale_fair(self):
        with _mock_path_table(DEST_HEX, _PATH_STALE_SECS):
            assert score_path(DEST_HEX, 2) == LinkQuality.FAIR

    def test_three_hops_fresh_fair(self):
        with _mock_path_table(DEST_HEX, _PATH_FRESH_SECS + 60):
            assert score_path(DEST_HEX, 3) == LinkQuality.FAIR

    def test_three_hops_stale_poor(self):
        with _mock_path_table(DEST_HEX, _PATH_STALE_SECS / 2):
            assert score_path(DEST_HEX, 3) == LinkQuality.POOR

    def test_no_path_table_one_hop_excellent(self):
        # No path table entry → treated as fresh
        with _mock_no_links():
            assert score_path(DEST_HEX, 1) == LinkQuality.EXCELLENT


# ---------------------------------------------------------------------------
# quality_label
# ---------------------------------------------------------------------------

class TestQualityLabel:
    def test_all_tiers_have_labels(self):
        for tier in LinkQuality:
            label = quality_label(tier)
            assert isinstance(label, str) and len(label) > 0

    def test_excellent_label(self):
        assert quality_label(LinkQuality.EXCELLENT) == "Excellent"

    def test_unknown_label(self):
        assert quality_label(LinkQuality.UNKNOWN) == "Unknown"
