"""
Family G -- restart, persistence and ordering.

A link drop keeps everything in memory; a process restart keeps only what
reached SQLite and the identity file. Anything a manager holds in a plain
dict is gone, and this family exists to find the places where that matters.

See docs/testenv-scenarios.md for the matrix these implement.
"""

from asserts import (
    all_hold, roster, settle, subscriber_views, wait_until, ScenarioFailure,
)
from flows import (
    await_discovery, invite_and_accept, invite_only_channel, join_all,
    public_channel, DISCOVERY_TIMEOUT,
)
from scenario import PROBE, scenario

RESTART_SETTLE = 120.0


@scenario("G1", "A restarted owner's subscriber-list version resets", kind=PROBE)
def g1(env):
    """SubscriptionManager._subscriber_versions is an in-memory dict, so a
    restarted owner starts numbering from 1 again while its subscribers still
    hold the higher version from before. Receivers reject anything not newer
    than what they hold, so a list published after the restart should be
    discarded as a replay -- leaving existing subscribers permanently unaware
    of anyone who joins afterwards.
    """
    a, b, c, d = env.peers("A", "B", "C", "D")
    ch = public_channel(a, [b, c], "g1-public")
    await_discovery([d], ch)

    a.send(ch, "before-restart")
    all_hold([b, c], ch, {"before-restart"}, timeout=DISCOVERY_TIMEOUT)

    env.orch.restart(a.tag)
    env.wait_alive(a)
    wait_until(lambda: b.hash in a.subscribers(ch),
               "the restarted owner to reload its subscriber set from storage",
               RESTART_SETTLE)

    join_all([d], ch, a)

    b_learns, b_secs = settle(lambda: d.hash in b.subscribers(ch),
                              "B to learn about D after the owner restarted", 90.0)
    c_learns, _ = settle(lambda: d.hash in c.subscribers(ch),
                         "C to learn about D after the owner restarted", 30.0)

    # The user-visible consequence: B addresses its sends to a list without D.
    b.send(ch, "from-B-after-restart")
    reached, _ = settle(lambda: "from-B-after-restart" in d.contents(ch),
                        "B's message to reach the peer that joined after the restart",
                        60.0)
    notes = {
        "b_learned_about_d": b_learns,
        "c_learned_about_d": c_learns,
        "learn_secs": round(b_secs, 1) if b_learns else None,
        "b_message_reached_d": reached,
        "subscriber_views": subscriber_views([a, b, c, d], ch),
    }
    if not (b_learns and c_learns and reached):
        notes["surprise"] = ("existing subscribers never learned about a peer that "
                             "joined after the owner restarted")
    return notes


@scenario("G2", "Every peer's state survives a full restart")
def g2(env):
    a, b, c, d = env.peers("A", "B", "C", "D")
    public = public_channel(a, [b, c, d], "g2-public")
    private = invite_only_channel(a, [b], "g2-private")

    messages = {f"g2-{i}" for i in range(3)}
    for content in sorted(messages):
        a.send(public, content)
    all_hold([b, c, d], public, messages, timeout=DISCOVERY_TIMEOUT)

    identities = {p.tag: p.hash for p in (a, b, c, d)}
    private_roster = roster(a, private)

    for peer in (a, b, c, d):
        env.orch.restart(peer.tag)
    for peer in (a, b, c, d):
        env.wait_alive(peer)

    for peer in (a, b, c, d):
        if peer.me()["hash_hex"] != identities[peer.tag]:
            raise ScenarioFailure(f"{peer.tag} came back with a different identity")

    for peer in (a, b, c, d):
        wait_until(lambda peer=peer: peer.contents(public) == messages,
                   f"{peer.tag} to still hold its message history", RESTART_SETTLE)
    if roster(a, private) != private_roster:
        raise ScenarioFailure("the invite-only roster did not survive the restart")
    return {"messages": len(messages), "private_members": len(private_roster)}


@scenario("G3", "An invite sent before a path resolves is dropped", kind=PROBE)
def g3(env):
    """invite.py's _send_raw has no retry queue, so an invite issued the
    instant a channel is created -- before the invitee's path is known -- is
    dropped silently. Measures how many attempts it actually takes."""
    a, b = env.peers("A", "B")
    ch = a.create_channel("g3-private", "invite")

    attempts = 0
    landed = False
    for attempts in range(1, 5):
        a.invite(ch, b.hash)
        landed, _ = settle(
            lambda: any(i["channel_hash_hex"] == ch for i in b.invites()),
            "B to be offered the invite", 15.0,
        )
        if landed:
            break

    notes = {"attempts_needed": attempts if landed else None, "landed": landed}
    if attempts > 1:
        notes["surprise"] = f"the first {attempts - 1} invite(s) were dropped silently"
    return notes


@scenario("G4", "A send immediately after a roster change", kind=PROBE)
def g4(env):
    """Two independent LXMF sends with no ordering guarantee: messaging.py
    drops an inbound chat message if the receiver is not yet marked a member.
    Records whether a message sent with no settle actually lands."""
    a, b, c = env.peers("A", "B", "C")
    ch = invite_only_channel(a, [b], "g4-private")

    a.invite(ch, c.hash)
    invite_and_accept(a, c, ch)
    # Deliberately no wait for C's own roster to catch up.
    a.send(ch, "immediately-after-admit")

    landed, secs = settle(lambda: "immediately-after-admit" in c.contents(ch),
                          "the immediate message to reach the new member", 90.0)
    notes = {"landed": landed, "secs": round(secs, 1) if landed else None}
    if not landed:
        notes["surprise"] = "a message sent right after admission was dropped for good"
    return notes


@scenario("G5", "A wiped tester returns as a different identity")
def g5(env):
    a, b, c = env.peers("A", "B", "C")
    ch = public_channel(a, [b, c], "g5-public")
    old_hash = c.hash

    a.send(ch, "before-wipe")
    all_hold([b, c], ch, {"before-wipe"}, timeout=DISCOVERY_TIMEOUT)

    env.orch.reset_tester(c.tag)
    env.wait_alive(c)
    c.forget_hash()

    if c.hash == old_hash:
        raise ScenarioFailure("a wiped tester came back with its old identity")
    if c.channels():
        raise ScenarioFailure("a wiped tester came back holding channels")

    # The owner still lists the identity that will never return.
    if old_hash not in a.subscribers(ch):
        raise ScenarioFailure("the owner dropped the wiped peer without being told")
    return {"stale_subscriber_retained": True}
