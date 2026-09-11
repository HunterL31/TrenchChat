"""
Family upgrade -- the direct IP session two members open between themselves.

Everything about this is invisible to pytest's shim. The offer and the answer
travel over real LXMF, the probes are real UDP datagrams sent at real
candidates, and what comes up at the end is a real QUIC session whose HELLO
authenticates against the same identity keys the member list holds. Only three
things decide whether it works, and all three are timing: whether an offer is
sent before the pair are members of anything (upgrade1), whether it is sent at
all to someone who should never see this node's address (upgrade2), and what
happens in the seconds after a session goes away (upgrade3 and upgrade4).

The gate is the point of the family. A session discloses this node's addresses,
so upgrade2 is the row that matters most: a peer who shares only a public
channel is never offered one, however long it waits and whatever it does.

See docs/testenv-scenarios.md for the matrix these implement.
"""

import time

from asserts import hold_for, settle, wait_until, ScenarioFailure
from flows import (
    invite_only_channel, public_channel, BACKFILL_TIMEOUT, DISCOVERY_TIMEOUT,
    NEGATIVE_HOLD_SECS,
)
from scenario import scenario
from trenchchat.core.upgrade import REASON_INELIGIBLE
from trenchchat.network.base import PATH_DIRECT, PATH_RETICULUM

# An upgrade waits on a sighting, and a sighting waits on the announce
# heartbeat the testenv runs at ten seconds. The punch itself takes under a
# second on this loopback, so nearly all of this is waiting to be noticed.
UPGRADE_TIMEOUT = 120.0

# After a session is dropped, the next offer is one tick away, and a tick is a
# second. This is the window the pair has to notice and come back.
RECOVERY_TIMEOUT = 90.0

# What "gone within a second" is measured against.
TEARDOWN_TIMEOUT = 2.0


def _session_with(peer, other) -> dict | None:
    """One peer's own record of its session with another, if it holds one."""
    for entry in peer.upgrade_sessions()["sessions"]:
        if entry["peer"] == other.hash:
            return entry
    return None


def _upgraded(peer, other) -> bool:
    """Whether this peer holds a direct session with the other."""
    return _session_with(peer, other) is not None


def await_upgrade(a, b, timeout: float = UPGRADE_TIMEOUT) -> float:
    """Wait for both ends to hold a session with each other. Returns the seconds."""
    started = time.time()
    wait_until(lambda: _upgraded(a, b), f"{a.tag} to open a session with {b.tag}",
               timeout)
    wait_until(lambda: _upgraded(b, a), f"{b.tag} to hold the far side of it",
               timeout)
    return round(time.time() - started, 1)


@scenario("upgrade1", "Two members of an invite-only channel come up direct",
          peers="AB")
def u1(env):
    """The whole handshake over real signalling: offer, answer, punch, session.

    Asserted from both ends and from both surfaces, because they are different
    claims: /upgrade/sessions is what this node knows about its own session,
    and the member row's path is what a client draws from it.
    """
    a, b = env.peers("A", "B")
    channel = invite_only_channel(a, [b], "upgrade1-room")

    seconds = await_upgrade(a, b)
    wait_until(lambda: a.member_path(channel, b.hash) == PATH_DIRECT,
               f"{a.tag}'s roster to show {b.tag} as direct", UPGRADE_TIMEOUT)
    wait_until(lambda: b.member_path(channel, a.hash) == PATH_DIRECT,
               f"{b.tag}'s roster to show {a.tag} as direct", UPGRADE_TIMEOUT)

    a.send(channel, "upgrade1-over-the-session")
    wait_until(lambda: "upgrade1-over-the-session" in b.contents(channel),
               f"{b.tag} to receive the message", DISCOVERY_TIMEOUT)

    session = _session_with(a, b)
    if a.upgrade_sessions()["last_failure"].get(b.hash):
        raise ScenarioFailure(f"{a.tag} recorded a failure for a pair that is up")
    return {"upgrade_secs": seconds,
            "bytes_each_way": f"{session['bytes_in']}/{session['bytes_out']}",
            "round_trip_ms": (round(session["round_trip_secs"] * 1000, 1)
                              if session["round_trip_secs"] else None)}


@scenario("upgrade2", "A public channel co-subscriber is never offered a session",
          peers="ABC")
def u2(env):
    """The gate, from the outside. C shares a public channel with A and B and
    nothing else, so it must never be offered an address and never hold a
    session, however long everybody waits."""
    a, b, c = env.peers("A", "B", "C")
    private = invite_only_channel(a, [b], "upgrade2-private")
    public = public_channel(a, [b, c], "upgrade2-public")

    await_upgrade(a, b)
    a.send(public, "upgrade2-everyone")
    wait_until(lambda: "upgrade2-everyone" in c.contents(public),
               f"{c.tag} to receive the public message", BACKFILL_TIMEOUT)

    hold_for(lambda: not _upgraded(a, c) and not _upgraded(c, a),
             f"{c.tag} to stay off {a.tag}'s direct path", NEGATIVE_HOLD_SECS)
    if _upgraded(b, c) or _upgraded(c, b):
        raise ScenarioFailure(f"{c.tag} opened a session with {b.tag}")
    if c.upgrade_sessions()["sessions"]:
        raise ScenarioFailure(f"{c.tag} holds a session with somebody")
    if a.member_path(private, b.hash) != PATH_DIRECT:
        raise ScenarioFailure(f"{a.tag} lost its session with {b.tag} meanwhile")

    refusal = a.upgrade_failure(c.hash)
    return {"refusal": (refusal or {}).get("reason", "never considered"),
            "c_sessions": len(c.upgrade_sessions()["sessions"])}


@scenario("upgrade3", "A session dropped mid-conversation loses no message",
          peers="AB")
def u3(env):
    """A direct session is an upgrade, never a replacement. Dropping it has to
    cost nothing but speed: the message sent while it is down goes over
    Reticulum, and the pair comes back without anybody asking."""
    a, b = env.peers("A", "B")
    channel = invite_only_channel(a, [b], "upgrade3-room")
    first_upgrade = await_upgrade(a, b)

    a.send(channel, "upgrade3-before")
    wait_until(lambda: "upgrade3-before" in b.contents(channel),
               f"{b.tag} to receive the first message", DISCOVERY_TIMEOUT)

    if not a.upgrade_close(b.hash)["ok"]:
        raise ScenarioFailure(f"{a.tag} had no session to drop")
    wait_until(lambda: not _upgraded(a, b) and not _upgraded(b, a),
               "the session to be gone from both ends", TEARDOWN_TIMEOUT * 10)
    dropped, _ = settle(lambda: a.member_path(channel, b.hash) == PATH_RETICULUM,
                        f"{a.tag} to show {b.tag} back on the mesh", 20.0)

    a.send(channel, "upgrade3-while-down")
    wait_until(lambda: "upgrade3-while-down" in b.contents(channel),
               f"{b.tag} to receive the message sent over the mesh",
               BACKFILL_TIMEOUT)

    back = await_upgrade(a, b, RECOVERY_TIMEOUT)
    a.send(channel, "upgrade3-after")
    wait_until(lambda: "upgrade3-after" in b.contents(channel),
               f"{b.tag} to receive the message after the session returned",
               DISCOVERY_TIMEOUT)

    expected = {"upgrade3-before", "upgrade3-while-down", "upgrade3-after"}
    missing = expected - b.contents(channel)
    if missing:
        raise ScenarioFailure(f"{b.tag} is missing {sorted(missing)}")
    return {"first_upgrade_secs": first_upgrade, "recovery_secs": back,
            "path_flipped": dropped}


@scenario("upgrade4", "A kicked member's session is gone within a second",
          peers="AB")
def u4(env):
    """The once-a-second re-check, which is the only layer that reaches a
    session already up. Membership is what a session is held on, so losing it
    has to close the session rather than wait for the peer to notice."""
    a, b = env.peers("A", "B")
    channel = invite_only_channel(a, [b], "upgrade4-room")
    await_upgrade(a, b)

    if not a.set_roles(channel, remove_members=[b.hash]):
        raise ScenarioFailure(f"{a.tag} could not kick {b.tag}")
    kicked_at = time.time()
    wait_until(lambda: not _upgraded(a, b),
               f"{a.tag} to close the kicked member's session", TEARDOWN_TIMEOUT)
    closed_in = round(time.time() - kicked_at, 2)

    wait_until(lambda: a.member_path(channel, b.hash) is None,
               f"{b.tag} to leave {a.tag}'s roster", DISCOVERY_TIMEOUT)
    wait_until(lambda: not _upgraded(b, a),
               f"{b.tag} to lose its side of the session", DISCOVERY_TIMEOUT)
    hold_for(lambda: not _upgraded(a, b),
             "the session to stay closed", NEGATIVE_HOLD_SECS)

    refusal = a.upgrade_failure(b.hash) or {}
    if refusal.get("reason") != REASON_INELIGIBLE:
        raise ScenarioFailure(
            f"{a.tag} recorded {refusal.get('reason', 'nothing')} rather than "
            f"{REASON_INELIGIBLE}")
    return {"closed_in_secs": closed_in, "reason": refusal["reason"]}
