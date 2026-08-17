"""
Polling assertions for scenarios.

Nothing here asserts on an immediate read. Every check polls until it holds
or a deadline passes: delivery runs over real RNS Links between separate
processes, so "not yet" and "never" are only distinguishable by waiting.
"""

import time

DEFAULT_TIMEOUT = 30.0
SLOW_TIMEOUT = 90.0
_INTERVAL = 0.5


class ScenarioFailure(AssertionError):
    """A scenario's expected result did not hold."""


def wait_until(pred, what: str, timeout: float = DEFAULT_TIMEOUT,
               interval: float = _INTERVAL) -> float:
    """Poll until pred() is truthy. Returns seconds elapsed, raises on timeout."""
    started = time.time()
    deadline = started + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            if pred():
                return time.time() - started
        except Exception as e:
            last_error = e
        time.sleep(interval)
    suffix = f" (last error: {last_error})" if last_error else ""
    raise ScenarioFailure(f"timed out after {timeout:.0f}s waiting for {what}{suffix}")


def settle(pred, what: str, timeout: float = DEFAULT_TIMEOUT,
           interval: float = _INTERVAL) -> tuple[bool, float]:
    """wait_until that reports instead of raising.

    For probe scenarios, where "it never happened" is a result to record
    rather than a failure.
    """
    try:
        return True, wait_until(pred, what, timeout, interval)
    except ScenarioFailure:
        return False, timeout


def hold_for(pred, what: str, secs: float, interval: float = _INTERVAL) -> None:
    """Assert pred() stays true for the whole window.

    The counterpart to wait_until: proves something did *not* arrive, rather
    than that it has not arrived yet.
    """
    deadline = time.time() + secs
    while time.time() < deadline:
        if not pred():
            raise ScenarioFailure(f"{what} stopped holding within {secs:.0f}s")
        time.sleep(interval)


# --- state readers ---


def roster(peer, channel_hash: str) -> dict[str, str]:
    """Members of a channel as {identity_hash: role}."""
    return {m["identity_hash"]: m["role"] for m in peer.members(channel_hash)}


def discovered_hashes(peer) -> set[str]:
    return {c["hash"] for c in peer.discovered()}

def joined_hashes(peer) -> set[str]:
    return {c["hash"] for c in peer.channels()}


# --- convergence ---


def all_hold(peers, channel_hash: str, expected: set[str], *,
             timeout: float = DEFAULT_TIMEOUT) -> dict[str, float]:
    """Every peer holds exactly *expected* message contents. Returns per-tag latency."""
    elapsed = {}
    for p in peers:
        elapsed[p.tag] = wait_until(
            lambda p=p: p.contents(channel_hash) == expected,
            f"{p.tag} to hold {sorted(expected)}", timeout,
        )
    return elapsed


def converged(peers, channel_hash: str, *, timeout: float = DEFAULT_TIMEOUT) -> set[str]:
    """Every peer agrees on the message set. Returns the agreed set."""
    def same() -> bool:
        sets = [p.contents(channel_hash) for p in peers]
        return all(s == sets[0] for s in sets)

    wait_until(same, f"{[p.tag for p in peers]} to converge on {channel_hash[:12]}", timeout)
    return peers[0].contents(channel_hash)


def rosters_identical(peers, channel_hash: str, *,
                      timeout: float = DEFAULT_TIMEOUT) -> dict[str, str]:
    """Every peer agrees on membership and roles. Returns the agreed roster."""
    def same() -> bool:
        rosters = [roster(p, channel_hash) for p in peers]
        return all(r == rosters[0] and r for r in rosters)

    wait_until(same, f"{[p.tag for p in peers]} rosters to match", timeout)
    return roster(peers[0], channel_hash)


def diff_report(peers, channel_hash: str, expected: set[str]) -> dict[str, dict]:
    """Per-peer missing/extra against an expected message set, for failure detail."""
    report = {}
    for p in peers:
        held = p.contents(channel_hash)
        report[p.tag] = {
            "held": len(held),
            "missing": sorted(expected - held),
            "extra": sorted(held - expected),
        }
    return report


def sync_settled(peer, channel_hash: str, *, timeout: float = SLOW_TIMEOUT) -> dict:
    """Wait for a channel to stop syncing. Returns the final status."""
    wait_until(
        lambda: peer.sync_status(channel_hash)["state"] != "syncing",
        f"{peer.tag} sync on {channel_hash[:12]} to settle", timeout,
    )
    return peer.sync_status(channel_hash)
