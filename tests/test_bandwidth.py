"""
BandwidthMonitor windowed-rate arithmetic and the /bandwidth endpoint.
"""

import sys
import warnings
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from trenchchat.core.bandwidth import BandwidthMonitor, MIN_SAMPLE_GAP_SECS

_TESTENV_DIR = Path(__file__).resolve().parents[1] / "devtools" / "testenv"
if str(_TESTENV_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTENV_DIR))

try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient

    from api import TOKEN_HEADER, create_app
    _HAVE_BACKEND_DEPS = True
except ImportError:  # pragma: no cover - depends on the local install
    _HAVE_BACKEND_DEPS = False
    TOKEN_HEADER = "x-tc-token"

needs_backend = pytest.mark.skipif(
    not _HAVE_BACKEND_DEPS,
    reason="install devtools/testenv/requirements.txt to exercise the API",
)

TOKEN = "test-token-not-a-real-one"
AUTH = {TOKEN_HEADER: TOKEN}


class FakeSource:
    def __init__(self):
        self.interfaces: dict[str, tuple[int, int]] = {}

    def __call__(self):
        return [(name, rx, tx) for name, (rx, tx) in self.interfaces.items()]


def _window(report: dict, secs: int) -> dict:
    return next(w for w in report["windows"] if w["secs"] == secs)


class TestBandwidthMonitor:
    def test_rates_over_windows(self):
        src = FakeSource()
        mon = BandwidthMonitor(sample_source=src, windows_secs=(10, 60))

        src.interfaces["Link"] = (0, 0)
        mon.sample(now=1000.0)
        src.interfaces["Link"] = (600, 300)
        mon.sample(now=1030.0)
        src.interfaces["Link"] = (700, 350)
        mon.sample(now=1040.0)

        report = mon.rates(now=1040.0)

        assert report["totals"] == {"rx": 700, "tx": 350}
        w10 = _window(report, 10)
        assert (w10["rx_bytes"], w10["tx_bytes"]) == (100, 50)
        assert w10["rx_per_sec"] == pytest.approx(10.0)
        w60 = _window(report, 60)
        assert (w60["rx_bytes"], w60["tx_bytes"]) == (700, 350)
        assert w60["rx_per_sec"] == pytest.approx(700 / 40, abs=0.1)

    def test_counter_reset_does_not_go_negative(self):
        """A reconnected interface restarts at zero; the window must count
        what moved since the reset, never a negative delta."""
        src = FakeSource()
        mon = BandwidthMonitor(sample_source=src, windows_secs=(60,))

        src.interfaces["Link"] = (5000, 4000)
        mon.sample(now=1000.0)
        src.interfaces["Link"] = (250, 100)
        mon.sample(now=1010.0)

        w = _window(mon.rates(now=1010.0), 60)
        assert (w["rx_bytes"], w["tx_bytes"]) == (250, 100)

    def test_reset_on_one_interface_leaves_the_other_counting(self):
        src = FakeSource()
        mon = BandwidthMonitor(sample_source=src, windows_secs=(60,))

        src.interfaces = {"A": (1000, 1000), "B": (9000, 9000)}
        mon.sample(now=1000.0)
        src.interfaces = {"A": (1500, 1200), "B": (30, 20)}
        mon.sample(now=1010.0)

        w = _window(mon.rates(now=1010.0), 60)
        assert (w["rx_bytes"], w["tx_bytes"]) == (500 + 30, 200 + 20)

    def test_interface_appearing_mid_window_counts_fully(self):
        src = FakeSource()
        mon = BandwidthMonitor(sample_source=src, windows_secs=(60,))

        src.interfaces = {"A": (100, 100)}
        mon.sample(now=1000.0)
        src.interfaces = {"A": (100, 100), "B": (40, 60)}
        mon.sample(now=1010.0)

        w = _window(mon.rates(now=1010.0), 60)
        assert (w["rx_bytes"], w["tx_bytes"]) == (40, 60)

    def test_samples_closer_than_the_gap_are_dropped(self):
        src = FakeSource()
        mon = BandwidthMonitor(sample_source=src, windows_secs=(10,))

        src.interfaces["Link"] = (0, 0)
        mon.sample(now=1000.0)
        src.interfaces["Link"] = (100, 100)
        mon.sample(now=1000.0 + MIN_SAMPLE_GAP_SECS / 2)

        report = mon.rates(now=1000.0 + MIN_SAMPLE_GAP_SECS / 2)
        assert report["totals"] == {"rx": 0, "tx": 0}, \
            "a sample inside the minimum gap should have been dropped"

    def test_history_older_than_the_largest_window_is_pruned(self):
        src = FakeSource()
        mon = BandwidthMonitor(sample_source=src, windows_secs=(10,))

        src.interfaces["Link"] = (0, 0)
        mon.sample(now=1000.0)
        src.interfaces["Link"] = (100, 100)
        for i in range(30):
            mon.sample(now=1100.0 + i)

        assert len(mon._samples) <= 31
        w = _window(mon.rates(now=1129.0), 10)
        assert w["rx_bytes"] == 0, "the pre-jump sample should have aged out"

    def test_no_samples_reports_empty(self):
        mon = BandwidthMonitor(sample_source=lambda: [], windows_secs=(10,))
        report = mon.rates(now=1000.0)
        assert report["totals"] == {"rx": 0, "tx": 0}


@needs_backend
class TestBandwidthEndpoint:
    def test_get_bandwidth_returns_windowed_rates(self):
        src = FakeSource()
        src.interfaces["Link"] = (1234, 567)

        backend = MagicMock()
        backend.invite_mgr.list_pending_invites.return_value = []
        backend.bandwidth = BandwidthMonitor(sample_source=src)

        with TestClient(create_app(backend, token=TOKEN),
                        base_url="http://127.0.0.1:8801") as client:
            body = client.get("/bandwidth", headers=AUTH).json()

        assert body["totals"] == {"rx": 1234, "tx": 567}
        assert [w["secs"] for w in body["windows"]] == [10, 60, 300]
