"""
Family public -- public (open-join) channels.

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
from flows import (
    await_discovery as _await_discovery, go_offline, go_online,
    join_all as _join_all, public_channel, BACKFILL_TIMEOUT, DISCOVERY_TIMEOUT,
    NEGATIVE_HOLD_SECS,
)
from scenario import PROBE, scenario


@scenario("public1", "A creates a public channel; B, C, D discover it unjoined")
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


@scenario("public2", "B joins; A's subscriber set updates so A's next send reaches B")
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


@scenario("public3", "A sends to two subscribers; a non-subscriber gets nothing")
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


@scenario("public4", "A subscriber's send reaches the owner and other subscribers")
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


@scenario("public5", "A late public-channel joiner gets no history, ever")
def a5(env):
    """Public channels are live-only: joining subscribes to what comes next
    and nothing more. The pre-join backlog must never arrive, and the sync
    tracker must say why -- the channel is live, not stuck unsynced."""
    a, b, c, d = env.peers("A", "B", "C", "D")
    ch = a.create_channel("a5-public", "public")
    _join_all([b, c], ch, a)

    backlog = {f"a5-{i}" for i in range(5)}
    for content in sorted(backlog):
        a.send(ch, content)
    all_hold([b, c], ch, backlog, timeout=DISCOVERY_TIMEOUT)

    _await_discovery([d], ch)
    d.join(ch)
    if d.contents(ch):
        raise ScenarioFailure("history was present the instant D joined")

    hold_for(lambda: d.contents(ch) == set(),
             "the late joiner to stay without pre-join history", 30.0)
    state = d.sync_status(ch).get("state")
    if state != "live":
        raise ScenarioFailure(
            f"a public channel reports sync state {state!r}, expected 'live'"
        )
    return {"backlog_held_back": len(backlog), "sync_state": state}


@scenario("public6", "full_sync makes no difference on a public channel")
def a6(env):
    """A public channel serves no history at all, so a full_sync grant there
    is inert: both channels hold back the backlog from a late joiner. Runs
    both side by side; the failure mode is either one backfilling."""
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

    hold_for(lambda: d.contents(plain) == set() and d.contents(granted) == set(),
             "both public channels to hold back their backlog", 30.0)
    return {"member_perms_granted": a.permissions(granted)["member"],
            "backlog_held_back": len(backlog)}


@scenario("public7", "Leaving stops delivery without erasing received history")
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


@scenario("public8", "All four subscribers converge on every message")
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


@scenario("public10", "A subscriber offline across a roster change catches up", kind=PROBE)
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

    go_offline(c)

    # The broadcast C is meant to miss: A re-sends the subscriber list to
    # everyone each time someone joins, and D joining is the last one.
    d.join(ch)
    wait_until(lambda: d.hash in a.subscribers(ch), "A to register D as a subscriber",
               DISCOVERY_TIMEOUT)
    wait_until(lambda: d.hash in b.subscribers(ch), "B to learn about D",
               DISCOVERY_TIMEOUT)

    go_online(c)

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


@scenario("public9", "The owner leaving its own public channel", kind=PROBE)
def a9(env):
    """Matrix row public9. Undefined behavior: the owner still holds the
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


@scenario("public11", "Leaving and rejoining a public channel")
def a11(env):
    """The round trip public7 stops halfway through: a subscriber that left
    comes back, and the owner's next send must reach it again."""
    a, b, c = env.peers("A", "B", "C")
    ch = public_channel(a, [b, c], "a11-public")

    a.send(ch, "before-leave")
    all_hold([b, c], ch, {"before-leave"}, timeout=DISCOVERY_TIMEOUT)

    if not b.leave(ch):
        raise ScenarioFailure("leave was refused")
    wait_until(lambda: b.hash not in a.subscribers(ch),
               "A to drop B from the subscriber set", DISCOVERY_TIMEOUT)
    a.send(ch, "while-away")
    all_hold([c], ch, {"before-leave", "while-away"}, timeout=DISCOVERY_TIMEOUT)

    _join_all([b], ch, a)
    subscribers_converged([a, b, c], ch, timeout=DISCOVERY_TIMEOUT)
    a.send(ch, "after-return")
    wait_until(lambda: "after-return" in b.contents(ch),
               "B to receive the post-return send", BACKFILL_TIMEOUT)
    wait_until(lambda: "after-return" in c.contents(ch),
               "C to receive it too", DISCOVERY_TIMEOUT)

    # Live-only: the message B missed while away must never follow it back.
    hold_for(lambda: "while-away" not in b.contents(ch),
             "the missed message to stay missed on a live-only channel", 30.0)
    return {"missed_stayed_missed": True}
