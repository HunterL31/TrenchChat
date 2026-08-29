"""
Family links -- degraded links.

Each tester dials its own shaper, so one peer's link can be made to behave
like a radio while the others stay fast. Frame loss is the point: as
devtools/testenv/README.md puts it, pending retry, missed-delivery hints and
timestamp-fallback sync "only run on a degraded link, and dropping frames is
the only way to exercise them without killing a process". The sync family reaches
those mechanisms by dropping links and killing processes; this family is the
lossy-wire half of the same coverage.

Profile names come from flows rather than string literals. An unknown name
answers 400 and leaves the link unshaped, which silently turned three of
these scenarios into broadband runs until set_link_profile started reading
the shaping back.

Message counts are deliberately small. At 1 kbps the app's own announces and
beacons are a real fraction of the link, so a large burst measures the
simulation rather than the application.

See docs/testenv-scenarios.md for the matrix these implement.
"""

from asserts import diff_report, settle, wait_until, ScenarioFailure
from flows import (
    go_offline, go_online, invite_only_channel, public_channel, set_link_profile,
    BROADBAND, CUSTOM, LORA_FAST, LORA_LONG, LOSSY, PACKET_RADIO, SATELLITE, SERIAL,
)
from scenario import PROBE, scenario

# Degraded links need far longer than a broadband exchange.
DEGRADED_TIMEOUT = 240.0

# The slowest profiles carry a few hundred bytes a second; give them room.
VERY_SLOW_TIMEOUT = 420.0


def _send_batch(peer, channel_hash: str, prefix: str, count: int) -> set[str]:
    contents = {f"{prefix}-{i:03d}" for i in range(count)}
    for content in sorted(contents):
        peer.send(channel_hash, content)
    return contents


@scenario("links1", "A lossy sender still reaches every peer")
def d1(env):
    a, b, c, d = env.peers("A", "B", "C", "D")
    ch = public_channel(a, [b, c, d], "d1-public")

    summary = set_link_profile(env, a, LOSSY)
    expected = _send_batch(a, ch, "d1", 10)

    everyone = [b, c, d]
    arrived, elapsed = settle(lambda: all(p.contents(ch) == expected for p in everyone),
                              "every peer to receive the lossy sender's batch",
                              DEGRADED_TIMEOUT)
    if not arrived:
        raise ScenarioFailure(f"lossy send did not converge over {summary}: "
                              f"{diff_report(everyone, ch, expected)}")
    return {"profile": summary, "converge_secs": round(elapsed, 1),
            "messages": len(expected)}


@scenario("links2", "A slow but lossless link delivers, just slowly", kind=PROBE)
def d2(env):
    """Serial 9600: slow enough to be a real constraint, bounded enough that a
    stall is distinguishable from the simulation working."""
    a, b = env.peers("A", "B")
    ch = public_channel(a, [b], "d2-public")

    summary = set_link_profile(env, b, SERIAL)
    expected = _send_batch(a, ch, "d2", 5)

    arrived, elapsed = settle(lambda: b.contents(ch) == expected,
                              "B to receive the batch over a 9600 baud link",
                              DEGRADED_TIMEOUT)
    notes = {"profile": summary, "delivered": arrived,
             "secs": round(elapsed, 1) if arrived else None,
             "held": len(b.contents(ch))}
    if not arrived:
        notes["surprise"] = "a slow but lossless link did not deliver"
    return notes


@scenario("links3", "Peers on four different links still converge")
def d3(env):
    a, b, c, d = env.peers("A", "B", "C", "D")
    ch = public_channel(a, [b, c, d], "d3-public")

    profiles = {}
    for peer, profile in ((a, BROADBAND), (b, SATELLITE), (c, LORA_FAST), (d, SERIAL)):
        profiles[peer.tag] = set_link_profile(env, peer, profile)

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
    return {"profiles": profiles, "converge_secs": round(elapsed, 1),
            "messages": len(expected)}


@scenario("links4", "A lossy peer toggling offline still catches up", kind=PROBE)
def d4(env):
    """The combination the offline mechanisms are actually for: a bad link and
    an intermittent one at the same time."""
    a, b = env.peers("A", "B")
    ch = public_channel(a, [b], "d4-public")
    summary = set_link_profile(env, b, LOSSY)

    first = _send_batch(a, ch, "d4-first", 5)
    go_offline(b)
    second = _send_batch(a, ch, "d4-second", 5)
    go_online(b)

    expected = first | second
    arrived, elapsed = settle(lambda: b.contents(ch) == expected,
                              "B to catch up over a lossy link", DEGRADED_TIMEOUT)
    notes = {"profile": summary, "recovered": arrived,
             "secs": round(elapsed, 1) if arrived else None,
             "held": len(b.contents(ch)), "expected": len(expected)}
    if not arrived:
        notes["surprise"] = "a lossy link plus an outage lost messages for good"
    return notes


@scenario("links5", "Every peer on a constrained link still converges", kind=PROBE)
def d5(env):
    """The documented "a tester on a slow profile falls behind" case, asserted
    rather than assumed. Announces and beacons compete with the payload here."""
    a, b, c, d = env.peers("A", "B", "C", "D")
    ch = public_channel(a, [b, c, d], "d5-public")

    for peer in (a, b, c, d):
        summary = set_link_profile(env, peer, LORA_FAST)

    expected = _send_batch(a, ch, "d5", 3)
    everyone = [b, c, d]
    arrived, elapsed = settle(lambda: all(p.contents(ch) == expected for p in everyone),
                              "every constrained peer to receive the batch",
                              DEGRADED_TIMEOUT)
    notes = {"profile": summary, "converged": arrived,
             "secs": round(elapsed, 1) if arrived else None,
             "views": diff_report(everyone, ch, expected)}
    if not arrived:
        notes["surprise"] = "a fully constrained mesh did not converge"
    return notes


@scenario("links6", "Sync recovers a lossy peer without the link ever dropping")
def d6(env):
    """The README's stated reason for the lossy profile. The sync family reaches the
    retry and hint paths by dropping links and killing processes; this reaches
    them the way a real bad radio does -- the link stays nominally up the whole
    time and simply loses 15% of frames. Invite-only, because hints and sync
    only serve invite-only channels now."""
    a, b, c = env.peers("A", "B", "C")
    ch = invite_only_channel(a, [b, c], "d6-private")

    summary = set_link_profile(env, b, LOSSY)
    if not b.net_status()["online"]:
        raise ScenarioFailure("B's link went down; this scenario needs it up but lossy")

    expected = _send_batch(a, ch, "d6", 15)

    arrived, elapsed = settle(lambda: b.contents(ch) == expected,
                              "the lossy peer to receive every message",
                              DEGRADED_TIMEOUT)
    if not arrived:
        raise ScenarioFailure(
            f"a peer on {summary} never converged though its link stayed up: "
            f"{diff_report([b], ch, expected)} | sync: {b.sync_status(ch)}"
        )
    if not b.net_status()["online"]:
        raise ScenarioFailure("B's link dropped during the run, so this proves nothing")
    return {"profile": summary, "converge_secs": round(elapsed, 1),
            "messages": len(expected), "sync_state": b.sync_status(ch).get("state")}


@scenario("links7", "Packet radio, the slowest profile, still converges", kind=PROBE)
def d7(env):
    """AX.25 1200 baud with 5% loss -- the worst link TrenchChat claims to
    support for text. Nothing else in the suite touches it."""
    a, b = env.peers("A", "B")
    ch = public_channel(a, [b], "d7-public")
    summary = set_link_profile(env, b, PACKET_RADIO)

    expected = _send_batch(a, ch, "d7", 3)
    arrived, elapsed = settle(lambda: b.contents(ch) == expected,
                              "B to receive three messages over packet radio",
                              VERY_SLOW_TIMEOUT)
    notes = {"profile": summary, "delivered": arrived,
             "secs": round(elapsed, 1) if arrived else None,
             "held": len(b.contents(ch)), "expected": len(expected)}
    if not arrived:
        notes["surprise"] = "packet radio did not carry three short messages"
    return notes


@scenario("links8", "LoRa SF10 carries text at 1 kbps", kind=PROBE)
def d8(env):
    """The slowest LoRa profile, and the one the README warns a tester falls
    behind on. links5 uses SF7; this is the order-of-magnitude slower case."""
    a, b = env.peers("A", "B")
    ch = public_channel(a, [b], "d8-public")
    summary = set_link_profile(env, b, LORA_LONG)

    expected = _send_batch(a, ch, "d8", 3)
    arrived, elapsed = settle(lambda: b.contents(ch) == expected,
                              "B to receive three messages over LoRa SF10",
                              VERY_SLOW_TIMEOUT)
    notes = {"profile": summary, "delivered": arrived,
             "secs": round(elapsed, 1) if arrived else None,
             "held": len(b.contents(ch))}
    if not arrived:
        notes["surprise"] = "LoRa SF10 did not carry three short messages"
    return notes


@scenario("links9", "A custom profile applies exactly the shaping asked for")
def d9(env):
    """The custom profile takes explicit bitrate/latency/jitter/loss overrides.
    Nothing else exercises that path, and a silently-ignored override would
    make every scenario built on it meaningless -- which is precisely what a
    mistyped profile name already did once."""
    a, b = env.peers("A", "B")
    ch = public_channel(a, [b], "d9-public")

    summary = set_link_profile(env, b, CUSTOM, bitrate_bps=32000,
                               latency_ms=120, jitter_ms=20, loss_pct=8)
    for fragment in ("32", "120", "20", "8"):
        if fragment not in summary:
            raise ScenarioFailure(
                f"custom override missing from the applied shaping: {summary!r}"
            )

    expected = _send_batch(a, ch, "d9", 5)
    arrived, elapsed = settle(lambda: b.contents(ch) == expected,
                              "B to receive the batch over a custom link",
                              DEGRADED_TIMEOUT)
    if not arrived:
        raise ScenarioFailure(f"custom-shaped link did not converge ({summary}): "
                              f"{diff_report([b], ch, expected)}")
    return {"profile": summary, "converge_secs": round(elapsed, 1)}


@scenario("links10", "Retuning a link mid-run takes effect immediately")
def d10(env):
    """Shaping applies live; only the bitrate hint written into the tester's
    RNS config waits for a restart. A scenario that changes a profile part-way
    depends on the live half actually being live."""
    a, b = env.peers("A", "B")
    ch = public_channel(a, [b], "d10-public")

    fast = set_link_profile(env, b, BROADBAND)
    first = _send_batch(a, ch, "d10-fast", 3)
    quick = wait_until(lambda: b.contents(ch) == first,
                       "the broadband batch to arrive", DEGRADED_TIMEOUT)

    slow = set_link_profile(env, b, SERIAL)
    second = _send_batch(a, ch, "d10-slow", 3)
    expected = first | second
    slower = wait_until(lambda: b.contents(ch) == expected,
                        "the batch after retuning to arrive", DEGRADED_TIMEOUT)

    return {"broadband": fast, "broadband_secs": round(quick, 1),
            "retuned": slow, "retuned_secs": round(slower, 1)}
