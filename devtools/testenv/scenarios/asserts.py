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


def subscriber_views(peers, channel_hash: str) -> dict[str, list[str]]:
    """Each peer's subscriber set, by tag rather than raw hash.

    On an open-join channel this is the recipient list a send is addressed to,
    so a peer missing from one view is a peer that send will never reach.
    """
    by_hash = {p.hash: p.tag for p in peers}
    return {
        p.tag: sorted(by_hash.get(h, h[:8]) for h in p.subscribers(channel_hash))
        for p in peers
    }


def subscribers_converged(peers, channel_hash: str, *,
                          timeout: float = DEFAULT_TIMEOUT) -> float:
    """Every peer's subscriber set names every peer.

    The open-join counterpart to waiting for a roster: a send is addressed to
    whatever this set holds, so asserting fan-out before it has propagated
    tests the timing of the owner's broadcast rather than the fan-out.
    """
    everyone = {p.hash for p in peers}

    def known() -> bool:
        # Every peer must know every *other* peer. The owner is never in its
        # own subscriber set (that tracks inbound MT_SUBSCRIBE), while the
        # list it broadcasts does name itself, so "knows everyone else" is the
        # one condition that holds on both sides.
        return all(everyone - {p.hash} <= set(p.subscribers(channel_hash)) for p in peers)

    return wait_until(known, f"{[p.tag for p in peers]} to agree on the subscriber set",
                      timeout)


def rosters_identical(peers, channel_hash: str, *,
                      timeout: float = DEFAULT_TIMEOUT) -> dict[str, str]:
    """Every peer agrees on membership and roles. Returns the agreed roster."""
    def same() -> bool:
        rosters = [roster(p, channel_hash) for p in peers]
        return all(r == rosters[0] and r for r in rosters)

    wait_until(same, f"{[p.tag for p in peers]} rosters to match", timeout)
    return roster(peers[0], channel_hash)


def roster_views(peers, channel_hash: str) -> dict[str, dict[str, str]]:
    """Each peer's roster keyed by tag rather than raw hash, for failure detail."""
    by_hash = {p.hash: p.tag for p in peers}
    return {
        p.tag: {by_hash.get(h, h[:8]): role for h, role in roster(p, channel_hash).items()}
        for p in peers
    }


def voice_rosters(peers, channel_hash: str) -> dict[str, dict[str, str]]:
    """Each peer's voice roster as {tag: link_state}, for failure detail."""
    by_hash = {p.hash: p.tag for p in peers}
    return {
        p.tag: {by_hash.get(h, h[:8]): state
                for h, state in p.voice_link_states(channel_hash).items()}
        for p in peers
    }


def voice_rosters_agree(peers, channel_hash: str, expected,
                        timeout: float = DEFAULT_TIMEOUT) -> float:
    """Every peer's voice roster names exactly the expected participants.

    Roster convergence is eventually consistent by design: a joiner is learned
    from its own voice_join, and existing occupants from the voice_state each
    unicasts back. Asserting it needs polling, not a single read.
    """
    want = {p.hash for p in expected}
    return wait_until(
        lambda: all(set(p.voice_link_states(channel_hash)) == want for p in peers),
        f"{[p.tag for p in peers]} voice rosters to name {[p.tag for p in expected]}",
        timeout,
    )


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
