"""
Family A -- public (open-join) channels.

Membership on a public channel is a subscription, not a member-list
document: SubscriptionManager tracks who is subscribed, no tenure is
recorded, and the owner's roster row is the only one that exists. That
makes this the cheapest family to run and the right one to prove the
harness on.

See docs/testenv-scenarios.md for the matrix these implement.
"""

from asserts import (
    all_hold, diff_report, discovered_hashes, hold_for, joined_hashes, settle,
    subscriber_views, subscribers_converged, wait_until, ScenarioFailure,
)
from scenario import PROBE, scenario

# Announces drive discovery and (family A's real subject) backfill. worker.py
# runs the heartbeat at 10s, so anything announce-triggered needs room for
# more than one cycle.
DISCOVERY_TIMEOUT = 60.0
BACKFILL_TIMEOUT = 90.0

# How long a "never arrives" claim is held open before it counts as proven.
NEGATIVE_HOLD_SECS = 15.0


def _await_discovery(peers, channel_hash: str, timeout: float = DISCOVERY_TIMEOUT):
    for p in peers:
        wait_until(lambda p=p: channel_hash in discovered_hashes(p),
                   f"{p.tag} to discover the channel", timeout)


def _join_all(peers, channel_hash: str, owner=None):
    """Join every peer, and wait for the owner to have registered them.

    Joining only sets the joiner's own state; the owner learns of it from an
    inbound MT_SUBSCRIBE that arrives separately. Until that lands the owner
    addresses its sends to a set the joiner isn't in, so any fan-out assertion
    made before this point is testing subscribe latency, not fan-out.
    """
    _await_discovery(peers, channel_hash)
    for p in peers:
        if not p.join(channel_hash):
            raise ScenarioFailure(f"{p.tag} failed to join {channel_hash[:12]}")
        wait_until(lambda p=p: channel_hash in joined_hashes(p),
                   f"{p.tag} to show the channel as joined")
    if owner is not None:
        for p in peers:
            wait_until(lambda p=p: p.hash in owner.subscribers(channel_hash),
                       f"{owner.tag} to register {p.tag} as a subscriber",
                       DISCOVERY_TIMEOUT)


@scenario("A1", "A creates a public channel; B, C, D discover it unjoined")
def a1(env):
    a, b, c, d = env.peers("A", "B", "C", "D")
    ch = a.create_channel("a1-public", "public")

    _await_discovery([b, c, d], ch)
    for p in (b, c, d):
        if ch in joined_hashes(p):
            raise ScenarioFailure(f"{p.tag} is subscribed to a channel it never joined")
    if ch not in joined_hashes(a):
        raise ScenarioFailure("creator is not subscribed to its own channel")
    return {"channel": ch[:12]}


@scenario("A2", "B joins; A's subscriber set updates so A's next send reaches B")
def a2(env):
    a, b = env.peers("A", "B")
    ch = a.create_channel("a2-public", "public")
    _join_all([b], ch, a)

    if ch in discovered_hashes(b):
        raise ScenarioFailure("channel still listed as undiscovered/unjoined for B")

    a.send(ch, "after-join")
    elapsed = wait_until(lambda: b.contents(ch) == {"after-join"},
                         "B to receive A's message", DISCOVERY_TIMEOUT)
    return {"delivery_secs": round(elapsed, 1)}


@scenario("A3", "A sends to two subscribers; a non-subscriber gets nothing")
def a3(env):
    a, b, c, d = env.peers("A", "B", "C", "D")
    ch = a.create_channel("a3-public", "public")
    _join_all([b, c], ch, a)

    expected = {f"a3-{i}" for i in range(3)}
    for content in sorted(expected):
        a.send(ch, content)

    latency = all_hold([b, c], ch, expected, timeout=DISCOVERY_TIMEOUT)
    hold_for(lambda: d.contents(ch) == set(), "D to stay empty", NEGATIVE_HOLD_SECS)
    return {"delivery_secs": {k: round(v, 1) for k, v in latency.items()}}


@scenario("A4", "A subscriber's send reaches the owner and other subscribers")
def a4(env):
    a, b, c, d = env.peers("A", "B", "C", "D")
    ch = a.create_channel("a4-public", "public")
    _join_all([b, c], ch, a)

    # B addresses its send to the subscriber list A broadcast, so that has to
    # have landed before a fan-out assertion means anything.
    subscribers_converged([a, b, c], ch, timeout=DISCOVERY_TIMEOUT)

    a.send(ch, "seed")
    all_hold([b, c], ch, {"seed"}, timeout=DISCOVERY_TIMEOUT)

    b.send(ch, "from-B")
    expected = {"seed", "from-B"}
    all_hold([a, c], ch, expected, timeout=DISCOVERY_TIMEOUT)
    hold_for(lambda: d.contents(ch) == set(), "D to stay empty", NEGATIVE_HOLD_SECS)
    return {}


@scenario("A5", "A late public-channel joiner has no sync trigger", kind=PROBE)
def a5(env):
    """Matrix row A5. join_public_channel() only subscribes -- no
    channel_joined callback fires, so nothing requests history. Measures how
    long backfill actually takes and what triggers it."""
    a, b, c, d = env.peers("A", "B", "C", "D")
    ch = a.create_channel("a5-public", "public")
    _join_all([b, c], ch, a)

    backlog = {f"a5-{i}" for i in range(5)}
    for content in sorted(backlog):
        a.send(ch, content)
    all_hold([b, c], ch, backlog, timeout=DISCOVERY_TIMEOUT)

    _await_discovery([d], ch)
    d.join(ch)
    immediate = len(d.contents(ch))

    arrived, elapsed = settle(lambda: d.contents(ch) == backlog,
                              "D to backfill the pre-join history", BACKFILL_TIMEOUT)
    notes = {
        "held_at_join": immediate,
        "backfilled": arrived,
        "backfill_secs": round(elapsed, 1) if arrived else None,
        "final_count": len(d.contents(ch)),
        "sync_state": d.sync_status(ch).get("state"),
    }
    if immediate > 0:
        notes["surprise"] = "history was present the instant D joined"
    return notes


@scenario("A6", "full_sync makes no difference on a public channel", kind=PROBE)
def a6(env):
    """Matrix row A6. Public channels never open tenure, so the tenure filter
    full_sync gates never engages. Runs both channels side by side; the
    finding is whether they differ."""
    a, d = env.peers("A", "D")
    plain = a.create_channel("a6-plain", "public")
    granted = a.create_channel("a6-fullsync", "public")

    perms = a.permissions(granted)
    a.set_permissions(granted, admin=perms["admin"],
                      member=sorted(set(perms["member"]) | {"full_sync"}))

    backlog = {f"a6-{i}" for i in range(5)}
    for ch in (plain, granted):
        for content in sorted(backlog):
            a.send(ch, content)

    _await_discovery([d], plain)
    _await_discovery([d], granted)
    d.join(plain)
    d.join(granted)

    got_plain, plain_secs = settle(lambda: d.contents(plain) == backlog,
                                   "D to backfill the plain channel", BACKFILL_TIMEOUT)
    got_granted, granted_secs = settle(lambda: d.contents(granted) == backlog,
                                       "D to backfill the full_sync channel",
                                       BACKFILL_TIMEOUT)
    notes = {
        "member_perms_granted": a.permissions(granted)["member"],
        "plain_backfilled": got_plain,
        "plain_secs": round(plain_secs, 1) if got_plain else None,
        "fullsync_backfilled": got_granted,
        "fullsync_secs": round(granted_secs, 1) if got_granted else None,
    }
    if got_plain != got_granted:
        notes["surprise"] = "full_sync changed the outcome on a public channel"
    return notes


@scenario("A7", "Leaving stops delivery without erasing received history")
def a7(env):
    a, b, c = env.peers("A", "B", "C")
    ch = a.create_channel("a7-public", "public")
    _join_all([b, c], ch, a)

    a.send(ch, "before-leave")
    all_hold([b, c], ch, {"before-leave"}, timeout=DISCOVERY_TIMEOUT)

    if not b.leave(ch):
        raise ScenarioFailure("B failed to leave")
    wait_until(lambda: ch not in joined_hashes(b), "B to drop the subscription")

    for content in ("after-1", "after-2"):
        a.send(ch, content)
    all_hold([c], ch, {"before-leave", "after-1", "after-2"}, timeout=DISCOVERY_TIMEOUT)
    hold_for(lambda: b.contents(ch) == {"before-leave"},
             "B to keep its history and receive nothing new", NEGATIVE_HOLD_SECS)
    return {}


@scenario("A8", "All four subscribers converge on every message")
def a8(env):
    a, b, c, d = env.peers("A", "B", "C", "D")
    ch = a.create_channel("a8-public", "public")
    _join_all([b, c, d], ch, a)

    everyone = [a, b, c, d]
    roster_secs = subscribers_converged(everyone, ch, timeout=DISCOVERY_TIMEOUT)

    a.send(ch, "seed")
    all_hold([b, c, d], ch, {"seed"}, timeout=DISCOVERY_TIMEOUT)

    expected = {"seed"}
    for p in (a, b, c, d):
        for i in range(2):
            content = f"{p.tag}-{i}"
            p.send(ch, content)
            expected.add(content)

    arrived, _ = settle(lambda: all(p.contents(ch) == expected for p in everyone),
                        "all four to hold every message", BACKFILL_TIMEOUT)
    if not arrived:
        raise ScenarioFailure(
            f"did not converge on {len(expected)} messages: "
            f"{diff_report(everyone, ch, expected)} | "
            f"subscriber views: {subscriber_views(everyone, ch)}"
        )
    return {"messages": len(expected), "roster_settle_secs": round(roster_secs, 1)}


@scenario("A10", "A subscriber offline across a roster change catches up", kind=PROBE)
def a10(env):
    """Written to prove SubscriptionManager._send_raw's missing retry queue
    strands a subscriber that misses a broadcast. It does not: _send_raw only
    drops when the path is unresolved, and for a peer whose path is already
    known, LXMF's own outbound retry redelivers once the link returns.
    Kept as the regression guard for that recovery.
    """
    a, b, c, d = env.peers("A", "B", "C", "D")
    ch = a.create_channel("a10-public", "public")
    _join_all([b, c], ch, a)
    _await_discovery([d], ch)

    a.send(ch, "seed")
    all_hold([b, c], ch, {"seed"}, timeout=DISCOVERY_TIMEOUT)

    c.go_offline()
    wait_until(lambda: not c.net_status()["online"], "C's link to drop")

    # The broadcast C is meant to miss: A re-sends the subscriber list to
    # everyone each time someone joins, and D joining is the last one.
    d.join(ch)
    wait_until(lambda: d.hash in a.subscribers(ch), "A to register D as a subscriber",
               DISCOVERY_TIMEOUT)
    wait_until(lambda: d.hash in b.subscribers(ch), "B to learn about D",
               DISCOVERY_TIMEOUT)

    c.go_online()
    wait_until(lambda: c.net_status()["online"], "C's link to come back", 60.0)

    knows_d, learn_secs = settle(lambda: d.hash in c.subscribers(ch),
                                 "C to learn about D after reconnecting", 60.0)

    c.send(ch, "from-C-after-reconnect")
    reached, reach_secs = settle(lambda: "from-C-after-reconnect" in d.contents(ch),
                                 "C's message to reach D", 60.0)
    notes = {
        "c_knows_d_after_reconnect": knows_d,
        "c_learned_after_secs": round(learn_secs, 1) if knows_d else None,
        "c_message_reached_d": reached,
        "reached_after_secs": round(reach_secs, 1) if reached else None,
        "subscriber_views": subscriber_views([a, b, c, d], ch),
    }
    if not knows_d or not reached:
        notes["surprise"] = ("a subscriber that missed one broadcast never "
                             "recovered the roster")
    return notes


@scenario("A9", "The owner leaving its own public channel", kind=PROBE)
def a9(env):
    """Matrix row A9. Undefined behavior: the owner still holds the
    authoritative subscriber list but is no longer subscribed itself.
    Records what the remaining subscribers see."""
    a, b, c, d = env.peers("A", "B", "C", "D")
    ch = a.create_channel("a9-public", "public")
    _join_all([b, c, d], ch, a)
    subscribers_converged([a, b, c, d], ch, timeout=DISCOVERY_TIMEOUT)

    a.send(ch, "seed")
    all_hold([b, c, d], ch, {"seed"}, timeout=DISCOVERY_TIMEOUT)

    a.leave(ch)
    c.send(ch, "after-owner-left")

    expected = {"seed", "after-owner-left"}
    got_b, _ = settle(lambda: b.contents(ch) == expected, "B to receive it", 45.0)
    got_d, _ = settle(lambda: d.contents(ch) == expected, "D to receive it", 45.0)
    got_a, _ = settle(lambda: a.contents(ch) == expected, "A to receive it", 15.0)
    return {
        "reached_other_subscribers": got_b and got_d,
        "reached_departed_owner": got_a,
        "owner_still_subscribed": ch in joined_hashes(a),
    }
