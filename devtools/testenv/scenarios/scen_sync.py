"""
Family C -- offline behaviour and sync.

The reason this environment exists. All three offline mechanisms only run on
an interrupted link, and none of them is reachable from the pytest suite,
whose TestTransport delivers in-process, instantly and in order:

  1. pending retry        -- sender re-sends when the peer reappears
  2. missed-delivery hint -- a third party serves what the sender never could
  3. timestamp sync       -- a reconnecting peer pulls from any online peer

These run on public channels, so no tenure filtering is in play: what a
responder is willing to serve is not the variable under test here, reaching
the peer at all is.

See docs/testenv-scenarios.md for the matrix these implement.
"""

from asserts import (
    all_hold, diff_report, hold_for, roster, settle, wait_until, ScenarioFailure,
)
from flows import (
    go_offline, go_online, invite_and_accept, invite_only_channel, public_channel,
    BACKFILL_TIMEOUT, DISCOVERY_TIMEOUT,
)
from scenario import PROBE, scenario

# A backlog larger than one sync response, so completing it requires a
# truncated batch to chain its own follow-up request.
MAX_RESPONSE_MESSAGES = 50
TRUNCATING_BACKLOG = MAX_RESPONSE_MESSAGES + 10

# Reconciling several peers' disjoint history takes more than one exchange.
CONVERGE_TIMEOUT = 180.0

# full_sync is the only permission that changes what a responder will serve.
ADMIN_WITH_FULL_SYNC = ["send_message", "invite", "kick", "manage_roles", "full_sync"]
MEMBER_WITH_FULL_SYNC = ["send_message", "full_sync"]


def _send_batch(peer, channel_hash: str, prefix: str, count: int) -> set[str]:
    """Send count messages and return the content set, zero-padded so the
    ordering in a failure report is readable."""
    contents = {f"{prefix}-{i:03d}" for i in range(count)}
    for content in sorted(contents):
        peer.send(channel_hash, content)
    return contents


@scenario("C1", "A link-dropped peer receives what it missed")
def c1(env):
    a, b, c = env.peers("A", "B", "C")
    ch = public_channel(a, [b, c], "c1-public")

    go_offline(b)
    missed = _send_batch(a, ch, "c1", 3)
    all_hold([c], ch, missed, timeout=DISCOVERY_TIMEOUT)

    go_online(b)
    elapsed = wait_until(lambda: b.contents(ch) == missed,
                         "B to receive everything sent while it was offline",
                         BACKFILL_TIMEOUT)
    return {"recovery_secs": round(elapsed, 1)}


@scenario("C2", "A third party serves history the sender never delivered")
def c2(env):
    """Mechanism 2. B misses three messages, then A goes offline before B
    returns -- so whatever B ends up with came from C or D, not the sender."""
    a, b, c, d = env.peers("A", "B", "C", "D")
    ch = public_channel(a, [b, c, d], "c2-public")

    go_offline(b)
    missed = _send_batch(a, ch, "c2", 3)
    all_hold([c, d], ch, missed, timeout=DISCOVERY_TIMEOUT)

    go_offline(a)
    go_online(b)

    arrived, elapsed = settle(lambda: b.contents(ch) == missed,
                              "B to backfill from a peer other than the sender",
                              CONVERGE_TIMEOUT)
    status = b.sync_status(ch)
    if not arrived:
        raise ScenarioFailure(
            f"B did not recover from its peers: {diff_report([b], ch, missed)} | "
            f"sync status: {status}"
        )
    answered = [p.get("identity_hash", "")[:8] for p in status.get("peers", [])
                if p.get("state") == "answered"]
    return {"recovery_secs": round(elapsed, 1), "answered_by": answered,
            "sync_state": status.get("state")}


@scenario("C3", "A killed and restarted peer recovers from disk plus sync")
def c3(env):
    """Mechanism 3 from a cold start. Unlike C1's link drop, nothing survives
    in memory -- no pending queue, no sync status, no subscriber cache."""
    a, b, c = env.peers("A", "B", "C")
    ch = public_channel(a, [b, c], "c3-public")

    before = _send_batch(a, ch, "c3-pre", 2)
    all_hold([b, c], ch, before, timeout=DISCOVERY_TIMEOUT)

    env.orch.kill(b.tag)
    wait_until(lambda: not b.alive(), "B's process to die")

    during = _send_batch(a, ch, "c3-during", 3)
    all_hold([c], ch, before | during, timeout=DISCOVERY_TIMEOUT)

    env.orch.start(b.tag)
    env.wait_alive(b)

    expected = before | during
    elapsed = wait_until(lambda: b.contents(ch) == expected,
                         "B to hold its own history plus what it missed",
                         CONVERGE_TIMEOUT)
    return {"recovery_secs": round(elapsed, 1), "messages": len(expected)}


@scenario("C4", "A backlog larger than one sync response completes")
def c4(env):
    """A response capped at MAX_RESPONSE_MESSAGES carries F_SYNC_TRUNCATED and
    the requester must chain its own follow-up. Without that chaining D stops
    at 50 of 60."""
    a, d = env.peers("A", "D")
    ch = public_channel(a, [d], "c4-public")

    go_offline(d)
    backlog = _send_batch(a, ch, "c4", TRUNCATING_BACKLOG)

    go_online(d)
    arrived, elapsed = settle(lambda: d.contents(ch) == backlog,
                              f"D to backfill all {TRUNCATING_BACKLOG} messages",
                              CONVERGE_TIMEOUT)
    held = len(d.contents(ch))
    if not arrived:
        raise ScenarioFailure(
            f"D holds {held}/{TRUNCATING_BACKLOG}"
            + (" -- stopped exactly at the response cap, so the truncated batch "
               "never chained a follow-up" if held == MAX_RESPONSE_MESSAGES else "")
        )
    return {"recovery_secs": round(elapsed, 1), "messages": held,
            "sync_state": d.sync_status(ch).get("state")}


@scenario("C5", "Two peers offline for different windows both catch up")
def c5(env):
    """Disjoint history: nobody holds everything B and C each need, so a
    channel-wide watermark would strand one of them."""
    a, b, c = env.peers("A", "B", "C")
    ch = public_channel(a, [b, c], "c5-public")

    go_offline(b)
    first = _send_batch(a, ch, "c5-first", 5)
    all_hold([c], ch, first, timeout=DISCOVERY_TIMEOUT)
    go_online(b)
    all_hold([b], ch, first, timeout=BACKFILL_TIMEOUT)

    go_offline(c)
    second = _send_batch(a, ch, "c5-second", 5)
    all_hold([b], ch, first | second, timeout=DISCOVERY_TIMEOUT)
    go_online(c)

    expected = first | second
    everyone = [a, b, c]
    arrived, elapsed = settle(lambda: all(p.contents(ch) == expected for p in everyone),
                              "both peers to hold every message", CONVERGE_TIMEOUT)
    if not arrived:
        raise ScenarioFailure(f"disjoint history not reconciled: "
                              f"{diff_report(everyone, ch, expected)}")
    return {"recovery_secs": round(elapsed, 1), "messages": len(expected)}


@scenario("C7", "Sync status reports a channel as settled", kind=PROBE)
def c7(env):
    """SyncStatusTracker is what a client shows the user. Records the state a
    channel lands in after a normal offline round trip."""
    a, b = env.peers("A", "B")
    ch = public_channel(a, [b], "c7-public")

    go_offline(b)
    missed = _send_batch(a, ch, "c7", 3)
    go_online(b)
    all_hold([b], ch, missed, timeout=BACKFILL_TIMEOUT)

    settled, elapsed = settle(lambda: b.sync_status(ch)["state"] != "syncing",
                              "B's sync state to settle", CONVERGE_TIMEOUT)
    status = b.sync_status(ch)
    notes = {
        "state": status.get("state"),
        "received_count": status.get("received_count"),
        "settled": settled,
        "settle_secs": round(elapsed, 1) if settled else None,
    }
    if status.get("state") != "synced":
        notes["surprise"] = (f"channel settled as {status.get('state')} with every "
                             f"message present")
    return notes


@scenario("C8", "Granting full_sync re-asks for history already withheld")
def c8(env):
    """Entitlement changed, so the next request must re-ask from the start
    rather than resuming from a watermark that already ran past the withheld
    rows. Needs an invite-only channel: full_sync does nothing on a public one."""
    a, d = env.peers("A", "D")
    ch = a.create_channel("c8-private", "invite")

    backlog = _send_batch(a, ch, "c8", 3)
    a.invite(ch, d.hash)
    invite_and_accept(a, d, ch)

    withheld, _ = settle(lambda: d.contents(ch) == backlog,
                         "history to arrive without full_sync", 20.0)
    if withheld:
        raise ScenarioFailure("history reached a member with no full_sync grant")

    a.set_permissions(ch, admin=ADMIN_WITH_FULL_SYNC, member=MEMBER_WITH_FULL_SYNC)
    arrived, elapsed = settle(lambda: d.contents(ch) == backlog,
                              "the backlog to arrive once full_sync is granted",
                              CONVERGE_TIMEOUT)
    if not arrived:
        raise ScenarioFailure(
            f"granting full_sync did not re-open the withheld history: "
            f"{diff_report([d], ch, backlog)}"
        )
    return {"backfill_secs": round(elapsed, 1), "messages": len(backlog)}


@scenario("C9", "A kicked member's sync request is refused")
def c9(env):
    a, b, c = env.peers("A", "B", "C")
    ch = invite_only_channel(a, [b, c], "c9-private",
                             permissions=(ADMIN_WITH_FULL_SYNC, MEMBER_WITH_FULL_SYNC))

    before = _send_batch(a, ch, "c9-before", 2)
    all_hold([b, c], ch, before, timeout=DISCOVERY_TIMEOUT)

    if not a.set_roles(ch, remove_members=[c.hash]):
        raise ScenarioFailure("the kick was rejected")
    wait_until(lambda: c.hash not in roster(a, ch), "C to be dropped",
               DISCOVERY_TIMEOUT)

    after = _send_batch(a, ch, "c9-after", 2)
    all_hold([b], ch, before | after, timeout=DISCOVERY_TIMEOUT)

    hold_for(lambda: c.contents(ch) == before,
             "a kicked member's history to stay frozen at the kick", 20.0)
    return {"frozen_at": len(before)}


@scenario("C10", "A total partition heals when the hub returns")
def c10(env):
    """The hub is the only transport between testers, so killing it isolates
    all four at once -- every peer keeps writing locally with nothing
    reachable."""
    a, b, c, d = env.peers("A", "B", "C", "D")
    ch = public_channel(a, [b, c, d], "c10-public")

    a.send(ch, "before-partition")
    all_hold([b, c, d], ch, {"before-partition"}, timeout=DISCOVERY_TIMEOUT)

    env.orch.hub_kill()
    wait_until(lambda: not env.orch.status()["hub"]["alive"], "the hub to die")

    expected = {"before-partition"}
    for p in (a, b, c, d):
        content = f"partitioned-{p.tag}"
        p.send(ch, content)
        expected.add(content)

    env.orch.hub_start()
    wait_until(lambda: env.orch.status()["hub"]["alive"], "the hub to come back")

    everyone = [a, b, c, d]
    arrived, elapsed = settle(lambda: all(p.contents(ch) == expected for p in everyone),
                              "all four to reconcile after the partition",
                              CONVERGE_TIMEOUT)
    if not arrived:
        raise ScenarioFailure(f"partition did not heal: "
                              f"{diff_report(everyone, ch, expected)}")
    return {"heal_secs": round(elapsed, 1), "messages": len(expected)}


@scenario("C11", "Four peers offline at once reconcile four histories")
def c11(env):
    """The hardest sync case: every peer writes in isolation, so on return
    each of them is both a requester and the only source of its own history.

    Currently fails, and left strict because the expectation is the design's
    own: any online member can serve any gap. Every peer ends up missing
    precisely the *first* message each other peer sent during the partition,
    never the second, and it does not heal given 420s. See
    docs/testenv-scenarios.md.
    """
    a, b, c, d = env.peers("A", "B", "C", "D")
    ch = public_channel(a, [b, c, d], "c11-public")

    a.send(ch, "seed")
    all_hold([b, c, d], ch, {"seed"}, timeout=DISCOVERY_TIMEOUT)

    everyone = [a, b, c, d]
    for p in everyone:
        go_offline(p)

    expected = {"seed"}
    for p in everyone:
        for i in range(2):
            content = f"{p.tag}-alone-{i}"
            p.send(ch, content)
            expected.add(content)

    for p in everyone:
        go_online(p)

    arrived, elapsed = settle(lambda: all(p.contents(ch) == expected for p in everyone),
                              "all four to reconcile", CONVERGE_TIMEOUT)
    if not arrived:
        raise ScenarioFailure(f"four-way reconcile incomplete: "
                              f"{diff_report(everyone, ch, expected)}")
    return {"reconcile_secs": round(elapsed, 1), "messages": len(expected)}
