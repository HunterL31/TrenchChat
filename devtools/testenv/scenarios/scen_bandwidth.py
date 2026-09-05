"""
Family bw -- what the mesh pays for TrenchChat when nothing much is happening.

Every figure here is read off the testers' Reticulum interface byte counters
(rxb/txb per interface, via /reticulum/interfaces), so it is what actually
crossed the wire, framing included. The scenario is a probe: it records and
never fails.

The heartbeat re-announce is set to an hour once the channel is up, so the
idle window measures the application's own background traffic (presence
beacons, and whatever sync does without a trigger) rather than the test
environment's announce cadence, which is orders of magnitude denser than the
real client's three-hourly re-announce. Beacons still fire on the test
environment's 30s silence threshold, six times the real client's 180s;
divide the idle figures by six for the deployed cadence.
"""

from asserts import all_hold, hold_for, settle
from flows import go_offline, go_online, public_channel, DISCOVERY_TIMEOUT
from scenario import PROBE, scenario

IDLE_SECS = 300.0
QUIET_BEFORE_MEASURING_SECS = 90.0
AWAY_SECS = 60.0
AFTER_RETURN_SECS = 90.0
LONG_HEARTBEAT_SECS = 3600.0


def _counters(peer) -> tuple[int, int]:
    """(rx, tx) bytes summed over the tester's live Reticulum interfaces."""
    rx = tx = 0
    for iface in peer._get("/reticulum/interfaces"):
        rx += int(iface.get("rxb") or 0)
        tx += int(iface.get("txb") or 0)
    return rx, tx


def _snapshot(peers) -> dict:
    return {p.tag: _counters(p) for p in peers}


def _delta(before: dict, after: dict) -> dict:
    return {tag: (after[tag][0] - before[tag][0], after[tag][1] - before[tag][1])
            for tag in before}


def _record(out: dict, phase: str, delta: dict, secs: float | None = None) -> None:
    for tag, (rx, tx) in delta.items():
        out[f"{phase}_{tag}_rx_bytes"] = rx
        out[f"{phase}_{tag}_tx_bytes"] = tx
        if secs:
            out[f"{phase}_{tag}_tx_bytes_per_min"] = round(tx * 60.0 / secs, 1)
    out[f"{phase}_total_bytes"] = sum(rx + tx for rx, tx in delta.values())


def _wait(secs: float, what: str) -> None:
    hold_for(lambda: True, what, secs)


@scenario("bw1", "Bytes on the wire: idle, a few messages, one peer away", kind=PROBE)
def bw1(env):
    """Four peers on one public channel, measured phase by phase.

    idle: nothing happens for IDLE_SECS. activity: each peer sends one
    message. away: B drops its link, A and C write while it is gone, B comes
    back and recovers. B's own counters restart with its interface, so its
    away figure is what it moved after reconnecting.

    Changing a tester's heartbeat restarts it, so every tester is restarted
    once before the quiet window and the window is long enough for the
    startup syncs and announces that follow to drain. An announce round is
    not measured here for the same reason: there is no way to make a tester
    announce without restarting it, and a restart is a startup, not an
    announce.
    """
    a, b, c, d = env.peers("A", "B", "C", "D")
    everyone = [a, b, c, d]
    ch = public_channel(a, [b, c, d], "bw1-public")
    expected = set()
    for i in range(5):
        content = f"bw1-seed-{i}"
        a.send(ch, content)
        expected.add(content)
    all_hold([b, c, d], ch, expected, timeout=DISCOVERY_TIMEOUT)

    for p in everyone:
        env.orch.set_heartbeat(p.tag, LONG_HEARTBEAT_SECS)
        env.wait_alive(p)
    all_hold(everyone, ch, expected, timeout=DISCOVERY_TIMEOUT)
    _wait(QUIET_BEFORE_MEASURING_SECS, "the restart burst to drain")

    out: dict = {}

    before = _snapshot(everyone)
    _wait(IDLE_SECS, "an idle window")
    _record(out, "idle", _delta(before, _snapshot(everyone)), IDLE_SECS)

    before = _snapshot(everyone)
    for p in everyone:
        content = f"bw1-{p.tag}-says-hi"
        p.send(ch, content)
        expected.add(content)
    all_hold(everyone, ch, expected, timeout=DISCOVERY_TIMEOUT)
    _wait(20.0, "message follow-ups to drain")
    _record(out, "activity", _delta(before, _snapshot(everyone)))

    before = _snapshot([a, c, d])
    go_offline(b)
    for i in range(3):
        content = f"bw1-while-b-away-{i}"
        a.send(ch, content)
        expected.add(content)
    _wait(AWAY_SECS, "B to stay away")
    content = "bw1-c-while-b-away"
    c.send(ch, content)
    expected.add(content)
    go_online(b)
    b_after_return = _counters(b)
    recovered, elapsed = settle(lambda: b.contents(ch) == expected,
                                "B to recover everything", 240.0)
    _wait(AFTER_RETURN_SECS, "recovery follow-ups to drain")
    delta = _delta(before, _snapshot([a, c, d]))
    b_now = _counters(b)
    delta[b.tag] = (b_now[0] - b_after_return[0], b_now[1] - b_after_return[1])
    _record(out, "away", delta)
    out["away_recovered"] = recovered
    out["away_recovery_secs"] = round(elapsed, 1)
    out["away_messages_missed"] = 4
    return out
