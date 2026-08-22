"""
Windowed bandwidth rates over the Reticulum interfaces.

A BandwidthMonitor samples the cumulative rx/tx byte counters RNS keeps per
interface and answers "how many bytes moved in the last 10s / 1m / 5m".
Sampling is read-only -- nothing here touches the network.
"""

import threading
import time

import RNS

# Windows the report covers, in seconds. The ring buffer is sized to the
# largest of these.
BANDWIDTH_WINDOWS_SECS = (10, 60, 300)

# How often the background sampler ticks.
SAMPLE_INTERVAL_SECS = 1.0

# Samples arriving closer together than this are dropped, so an aggressively
# polling client cannot grow the buffer past ~4 samples per second.
MIN_SAMPLE_GAP_SECS = 0.25

_MAX_AGE_SLACK_SECS = 5.0


def _read_rns_interfaces() -> list[tuple[str, int, int]]:
    """(name, rxb, txb) for every live RNS interface."""
    result = []
    for iface in list(RNS.Transport.interfaces):
        try:
            result.append((str(iface), int(iface.rxb), int(iface.txb)))
        except (AttributeError, TypeError, ValueError):
            continue
    return result


class BandwidthMonitor:
    """Keeps a short history of interface byte counters and reports
    windowed transfer rates.

    sample_source is injectable for tests; the default reads the live RNS
    transport interfaces.
    """

    def __init__(self, sample_source=None,
                 windows_secs: tuple = BANDWIDTH_WINDOWS_SECS):
        self._source = sample_source or _read_rns_interfaces
        self._windows = tuple(sorted(windows_secs))
        self._max_age = max(self._windows) + _MAX_AGE_SLACK_SECS
        # (ts, total_rx, total_tx, {name: (rxb, txb)})
        self._samples: list[tuple] = []
        self._lock = threading.Lock()

    def sample(self, now: float | None = None) -> None:
        """Record one reading of every interface's cumulative counters."""
        now = time.time() if now is None else now
        per_iface = {name: (rxb, txb) for name, rxb, txb in self._source()}
        total_rx = sum(rx for rx, _tx in per_iface.values())
        total_tx = sum(tx for _rx, tx in per_iface.values())
        with self._lock:
            if self._samples and now - self._samples[-1][0] < MIN_SAMPLE_GAP_SECS:
                return
            self._samples.append((now, total_rx, total_tx, per_iface))
            cutoff = now - self._max_age
            while self._samples and self._samples[0][0] < cutoff:
                self._samples.pop(0)

    def rates(self, now: float | None = None) -> dict:
        """Windowed rates from the samples held. Takes a fresh sample first,
        so a polling client always sees current counters."""
        self.sample(now)
        now = time.time() if now is None else now
        with self._lock:
            samples = list(self._samples)

        if not samples:
            return {"sampled_at": now, "totals": {"rx": 0, "tx": 0}, "windows": []}

        latest = samples[-1]
        windows = []
        for window_secs in self._windows:
            oldest = self._oldest_within(samples, latest[0] - window_secs)
            span = latest[0] - oldest[0]
            rx, tx = _window_delta(oldest[3], latest[3])
            windows.append({
                "secs": window_secs,
                "span_secs": round(span, 2),
                "rx_bytes": rx,
                "tx_bytes": tx,
                "rx_per_sec": round(rx / span, 1) if span > 0 else None,
                "tx_per_sec": round(tx / span, 1) if span > 0 else None,
            })

        return {
            "sampled_at": latest[0],
            "totals": {"rx": latest[1], "tx": latest[2]},
            "windows": windows,
        }

    @staticmethod
    def _oldest_within(samples: list[tuple], cutoff: float) -> tuple:
        for s in samples:
            if s[0] >= cutoff:
                return s
        return samples[-1]


def _counter_delta(older: int, newer: int) -> int:
    """Bytes moved between two cumulative readings.

    A reconnected interface restarts its counters at zero, making the raw
    delta negative; everything since the reset is then the closest truth.
    """
    return newer if newer < older else newer - older


def _window_delta(older: dict, newer: dict) -> tuple[int, int]:
    """Summed per-interface (rx, tx) deltas, so one interface resetting its
    counters can't drag the whole window negative."""
    rx = tx = 0
    for name, (new_rx, new_tx) in newer.items():
        old_rx, old_tx = older.get(name, (0, 0))
        rx += _counter_delta(old_rx, new_rx)
        tx += _counter_delta(old_tx, new_tx)
    return rx, tx
