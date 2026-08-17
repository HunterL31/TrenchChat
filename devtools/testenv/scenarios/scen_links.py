"""
Family D -- degraded links.

Each tester dials its own shaper, so one peer's link can be made to behave
like a radio while the others stay fast. Frame loss is the point: the retry
and hint paths only run when delivery actually fails, and dropping frames is
the only way to exercise them without killing a process.

Message counts are deliberately small. At 977 bps the app's own announces and
beacons are a real fraction of the link, so a large burst measures the
simulation rather than the application.

See docs/testenv-scenarios.md for the matrix these implement.
"""

from asserts import diff_report, settle, ScenarioFailure
from flows import public_channel
from scenario import PROBE, scenario

# Degraded links need far longer than a broadband exchange.
DEGRADED_TIMEOUT = 240.0

FLAKY = "flaky"
SATELLITE = "satellite"
SERIAL = "serial9600"
LORA_FAST = "lora_fast"
BROADBAND = "broadband"


def _send_batch(peer, channel_hash: str, prefix: str, count: int) -> set[str]:
    contents = {f"{prefix}-{i:03d}" for i in range(count)}
    for content in sorted(contents):
        peer.send(channel_hash, content)
    return contents


@scenario("D1", "A lossy sender still reaches every peer")
def d1(env):
    a, b, c, d = env.peers("A", "B", "C", "D")
    ch = public_channel(a, [b, c, d], "d1-public")

    env.orch.link_profile(a.tag, FLAKY)
    expected = _send_batch(a, ch, "d1", 10)

    everyone = [b, c, d]
    arrived, elapsed = settle(lambda: all(p.contents(ch) == expected for p in everyone),
                              "every peer to receive the lossy sender's batch",
                              DEGRADED_TIMEOUT)
    if not arrived:
        raise ScenarioFailure(f"lossy send did not converge: "
                              f"{diff_report(everyone, ch, expected)}")
    return {"converge_secs": round(elapsed, 1), "messages": len(expected)}


@scenario("D2", "A slow link delivers, just slowly", kind=PROBE)
def d2(env):
    """Serial 9600 rather than LoRa: slow enough to be a real constraint,
    bounded enough that a stall is distinguishable from the simulation."""
    a, b = env.peers("A", "B")
    ch = public_channel(a, [b], "d2-public")

    env.orch.link_profile(b.tag, SERIAL)
    expected = _send_batch(a, ch, "d2", 5)

    arrived, elapsed = settle(lambda: b.contents(ch) == expected,
                              "B to receive the batch over a 9600 baud link",
                              DEGRADED_TIMEOUT)
    notes = {"delivered": arrived, "secs": round(elapsed, 1) if arrived else None,
             "held": len(b.contents(ch))}
    if not arrived:
        notes["surprise"] = "a slow but lossless link did not deliver"
    return notes


@scenario("D3", "Peers on four different links still converge")
def d3(env):
    a, b, c, d = env.peers("A", "B", "C", "D")
    ch = public_channel(a, [b, c, d], "d3-public")

    env.orch.link_profile(a.tag, BROADBAND)
    env.orch.link_profile(b.tag, SATELLITE)
    env.orch.link_profile(c.tag, LORA_FAST)
    env.orch.link_profile(d.tag, SERIAL)

    expected = set()
    for peer in (a, b, c, d):
        expected |= _send_batch(peer, ch, peer.tag.lower(), 1)

    everyone = [a, b, c, d]
    arrived, elapsed = settle(lambda: all(p.contents(ch) == expected for p in everyone),
                              "four differently-shaped links to converge",
                              DEGRADED_TIMEOUT)
    if not arrived:
        raise ScenarioFailure(f"mixed-profile convergence incomplete: "
                              f"{diff_report(everyone, ch, expected)}")
    return {"converge_secs": round(elapsed, 1), "messages": len(expected)}


@scenario("D4", "A flaky peer toggling offline still catches up", kind=PROBE)
def d4(env):
    """The combination the offline mechanisms are actually for: a bad link and
    an intermittent one at the same time."""
    a, b = env.peers("A", "B")
    ch = public_channel(a, [b], "d4-public")
    env.orch.link_profile(b.tag, FLAKY)

    first = _send_batch(a, ch, "d4-first", 5)
    b.go_offline()
    second = _send_batch(a, ch, "d4-second", 5)
    b.go_online()

    expected = first | second
    arrived, elapsed = settle(lambda: b.contents(ch) == expected,
                              "B to catch up over a flaky link", DEGRADED_TIMEOUT)
    notes = {"recovered": arrived, "secs": round(elapsed, 1) if arrived else None,
             "held": len(b.contents(ch)), "expected": len(expected)}
    if not arrived:
        notes["surprise"] = "a flaky link plus an outage lost messages for good"
    return notes


@scenario("D5", "Every peer on a constrained link still converges", kind=PROBE)
def d5(env):
    """The documented 'a tester on a slow profile falls behind' case, asserted
    rather than assumed. Announces and beacons compete with the payload here."""
    a, b, c, d = env.peers("A", "B", "C", "D")
    ch = public_channel(a, [b, c, d], "d5-public")

    for peer in (a, b, c, d):
        env.orch.link_profile(peer.tag, LORA_FAST)

    expected = _send_batch(a, ch, "d5", 3)
    everyone = [b, c, d]
    arrived, elapsed = settle(lambda: all(p.contents(ch) == expected for p in everyone),
                              "every constrained peer to receive the batch",
                              DEGRADED_TIMEOUT)
    notes = {"converged": arrived, "secs": round(elapsed, 1) if arrived else None,
             "views": diff_report(everyone, ch, expected)}
    if not arrived:
        notes["surprise"] = "a fully constrained mesh did not converge"
    return notes
