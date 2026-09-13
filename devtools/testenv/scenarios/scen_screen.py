"""
Family screen -- screen share, over direct sessions and over nothing else.

A share lives inside a voice session and travels the direct session two
members already hold (docs/screen-share-plan.md). Nothing about it crosses the
mesh: not the frames, not the signalling, not the fact that a share exists. The
pytest suite proves that with a fake transport whose outbox it can read; these
rows prove it on a real network, where the only witness is what each tester
holds and what its watch socket delivers.

screen2 is the row that matters most, the way upgrade2 is for sessions: a
participant of the same voice session who holds no direct session with the
sharer is never told there is anything to watch. The rest are the life of a
share, on the real path: a viewer's frames, a session dropped and restored, a
viewer kicked, and a viewer too slow for the sharer's frame rate.

Headless testers share a generated picture (core/screen/capture.py's
MovingBoxSource), so every frame differs from the last in one tile and a
watcher always has something to count.

See docs/testenv-scenarios.md for the matrix these implement.
"""

import time

from asserts import hold_for, wait_until, ScenarioFailure
from flows import invite_only_channel, public_channel, NEGATIVE_HOLD_SECS
from scen_upgrade import RECOVERY_TIMEOUT, TEARDOWN_TIMEOUT, _upgraded, await_upgrade
from scen_voice import _await_mesh, _join_voice_all
from scenario import scenario

# A share is told to a direct participant the moment it starts, so this is
# one request over a session that is already up.
TOLD_TIMEOUT = 15.0

# Updates flow at the tester's frame rate once a watch is answered; a handful
# is enough to know they are flowing.
UPDATES_TIMEOUT = 20.0
ENOUGH_UPDATES = 5

# The sweep that drops a viewer runs once a second.
SWEEP_TIMEOUT = 3.0

# How long a slow viewer is left to fall behind, and how slowly it answers.
SLOW_WINDOW_SECS = 6.0
SLOW_ACK_SECS = 0.5


def _held(peer, sharer) -> bool:
    return sharer.hash in peer.held_shares()


def _start_share(sharer, channel: str) -> None:
    started = sharer.screen_start()
    if not started.get("ok"):
        raise ScenarioFailure(f"{sharer.tag} could not share: {started}")
    wait_until(lambda: sharer.screen_status()["sharing"] is not None,
               f"{sharer.tag} to report its share")


def _call_with_share(env, tags, name):
    """The peers named, in one invite-only channel and one voice session, the
    first of them sharing its screen, with a direct session between the first
    two."""
    peers = env.peers(*tags)
    channel = invite_only_channel(peers[0], peers[1:], name)
    await_upgrade(peers[0], peers[1])
    _join_voice_all(peers, channel)
    _await_mesh(peers, channel)
    _start_share(peers[0], channel)
    return peers, channel


@scenario("screen1", "A direct participant is told of the share and watches it",
          peers="AB")
def s1(env):
    """The whole life of a share on the real path: started reaches B over the
    session, B's watch socket delivers a full frame and then tiles, A counts B
    as a viewer, and stopping drops both."""
    (a, b), channel = _call_with_share(env, "AB", "screen1-room")
    wait_until(lambda: _held(b, a), f"{b.tag} to be told of {a.tag}'s share",
               TOLD_TIMEOUT)
    share = b.held_shares()[a.hash]
    if share["channel"] != channel:
        raise ScenarioFailure(f"{b.tag} holds the share on the wrong channel")

    started = time.time()
    with b.watch_screen(a.hash) as watcher:
        wait_until(lambda: watcher.snapshot()["updates"] >= ENOUGH_UPDATES,
                   f"{b.tag}'s watch socket to deliver updates", UPDATES_TIMEOUT)
        first_frames = round(time.time() - started, 1)
        wait_until(lambda: b.hash in a.screen_viewers(),
                   f"{a.tag} to count {b.tag} as a viewer", TOLD_TIMEOUT)
        seen = watcher.snapshot()
    if seen["full_frames"] < 1:
        raise ScenarioFailure(f"{b.tag} never received a full frame: {seen}")
    if (seen["width"], seen["height"]) != (share["width"], share["height"]):
        raise ScenarioFailure(f"{b.tag} received {seen['width']}x{seen['height']} "
                              f"for a share of {share['width']}x{share['height']}")
    wait_until(lambda: b.hash not in a.screen_viewers(),
               f"{a.tag} to drop {b.tag} once its socket closed", SWEEP_TIMEOUT * 4)

    if not a.screen_stop():
        raise ScenarioFailure(f"{a.tag} had no share to stop")
    wait_until(lambda: not _held(b, a), f"{b.tag} to drop the ended share",
               TOLD_TIMEOUT)
    for peer in (a, b):
        peer.leave_voice()
    return {"first_frames_secs": first_frames, "updates": seen["updates"],
            "full_frames": seen["full_frames"], "bytes": seen["bytes"]}


@scenario("screen2", "A mesh-only participant is never told of the share",
          peers="ABC")
def s2(env):
    """Decision 1 on a real network. C is in the same voice session as A and B
    and shares only a public channel with them, so it holds no direct session
    with anybody; it must never learn that A is sharing, however long it
    waits, and nothing it holds may say otherwise."""
    a, b, c = env.peers("A", "B", "C")
    private = invite_only_channel(a, [b], "screen2-private")
    public = public_channel(a, [b, c], "screen2-public")
    await_upgrade(a, b)
    hold_for(lambda: not _upgraded(a, c) and not _upgraded(c, a),
             f"{c.tag} to hold no session", 5.0)

    _join_voice_all([a, b, c], public)
    _await_mesh([a, b, c], public)
    _start_share(a, public)
    wait_until(lambda: _held(b, a), f"{b.tag} to be told of the share", TOLD_TIMEOUT)

    hold_for(lambda: not _held(c, a), f"{c.tag} to stay untold", NEGATIVE_HOLD_SECS)
    status = c.screen_status()
    if status["shares"] or status["watching"]:
        raise ScenarioFailure(f"{c.tag} holds screen state it cannot have: {status}")
    with c.watch_screen(a.hash) as watcher:
        wait_until(lambda: watcher.snapshot()["ended"] is not None,
                   f"{c.tag}'s watch to be refused", TOLD_TIMEOUT)
        refused = watcher.snapshot()["ended"]
    if refused != "no_share":
        raise ScenarioFailure(f"{c.tag}'s watch ended with {refused!r}, "
                              f"not no_share")
    if c.hash in a.screen_viewers():
        raise ScenarioFailure(f"{a.tag} counts {c.tag} as a viewer")

    a.screen_stop()
    for peer in (a, b, c):
        peer.leave_voice()
    return {"c_told": False, "c_refused": refused,
            "private": private[:12]}


@scenario("screen3", "A dropped session ends the watch and a restored one "
                     "tells the viewer again", peers="AB")
def s3(env):
    """A share never falls back: with the session gone B's watch ends and B
    drops the share, nothing crosses the mesh meanwhile, and when the pair
    comes back B is told again without anyone asking."""
    (a, b), channel = _call_with_share(env, "AB", "screen3-room")
    wait_until(lambda: _held(b, a), f"{b.tag} to be told of the share", TOLD_TIMEOUT)

    watcher = b.watch_screen(a.hash)
    try:
        wait_until(lambda: watcher.snapshot()["updates"] >= ENOUGH_UPDATES,
                   f"{b.tag} to receive updates", UPDATES_TIMEOUT)
        if not a.upgrade_close(b.hash)["ok"]:
            raise ScenarioFailure(f"{a.tag} had no session to drop")
        dropped_at = time.time()
        wait_until(lambda: watcher.snapshot()["ended"] is not None,
                   f"{b.tag}'s watch to end with the session", TEARDOWN_TIMEOUT * 10)
        ended_in = round(time.time() - dropped_at, 1)
        wait_until(lambda: not _held(b, a), f"{b.tag} to drop the share",
                   TEARDOWN_TIMEOUT * 10)
        wait_until(lambda: b.hash not in a.screen_viewers(),
                   f"{a.tag} to drop {b.tag} as a viewer", TEARDOWN_TIMEOUT * 10)
        ended = watcher.snapshot()["ended"]
    finally:
        watcher.close()
    if a.screen_status()["sharing"] is None:
        raise ScenarioFailure(f"{a.tag}'s share ended with the session; it must "
                              f"outlive it")

    back = await_upgrade(a, b, RECOVERY_TIMEOUT)
    wait_until(lambda: _held(b, a), f"{b.tag} to be told again after the "
               f"session returned", TOLD_TIMEOUT)
    with b.watch_screen(a.hash) as again:
        wait_until(lambda: again.snapshot()["updates"] >= 1,
                   f"{b.tag} to receive an update over the new session",
                   UPDATES_TIMEOUT)
    a.screen_stop()
    for peer in (a, b):
        peer.leave_voice()
    return {"watch_ended_in_secs": ended_in, "ended": ended,
            "recovery_secs": back}


@scenario("screen4", "A kicked viewer is out of the fan-out within a sweep",
          peers="AB")
def s4(env):
    """Membership is what a share is held on. Kicking B closes its session
    (upgrade4), and the sweep drops it as a viewer whichever happens first."""
    (a, b), channel = _call_with_share(env, "AB", "screen4-room")
    wait_until(lambda: _held(b, a), f"{b.tag} to be told of the share", TOLD_TIMEOUT)
    watcher = b.watch_screen(a.hash)
    try:
        wait_until(lambda: b.hash in a.screen_viewers(),
                   f"{a.tag} to count {b.tag} as a viewer", TOLD_TIMEOUT)
        if not a.set_roles(channel, remove_members=[b.hash]):
            raise ScenarioFailure(f"{a.tag} could not kick {b.tag}")
        kicked_at = time.time()
        wait_until(lambda: b.hash not in a.screen_viewers(),
                   f"{a.tag} to drop the kicked viewer", SWEEP_TIMEOUT)
        dropped_in = round(time.time() - kicked_at, 2)
        wait_until(lambda: watcher.snapshot()["ended"] is not None,
                   f"{b.tag}'s watch to end", TEARDOWN_TIMEOUT * 10)
        hold_for(lambda: b.hash not in a.screen_viewers(),
                 f"{b.tag} to stay out of the fan-out", 5.0)
        ended = watcher.snapshot()["ended"]
    finally:
        watcher.close()
    a.screen_stop()
    a.leave_voice()
    b.leave_voice()
    return {"dropped_in_secs": dropped_in, "ended": ended}


@scenario("screen5", "A slow viewer gets fewer, larger updates and the fast "
                     "one is unaffected", peers="ABC")
def s5(env):
    """The credit model: B acknowledges each update only after a delay, so
    the sharer coalesces for it and keeps C at the frame rate. Neither is
    ever sent an update it did not make room for."""
    a, b, c = env.peers("A", "B", "C")
    channel = invite_only_channel(a, [b, c], "screen5-room")
    await_upgrade(a, b)
    await_upgrade(a, c)
    _join_voice_all([a, b, c], channel)
    _await_mesh([a, b, c], channel)
    _start_share(a, channel)
    for viewer in (b, c):
        wait_until(lambda v=viewer: _held(v, a),
                   f"{viewer.tag} to be told of the share", TOLD_TIMEOUT)

    slow = b.watch_screen(a.hash, ack_delay=SLOW_ACK_SECS)
    fast = c.watch_screen(a.hash)
    try:
        wait_until(lambda: fast.snapshot()["updates"] >= 1
                   and slow.snapshot()["updates"] >= 1,
                   "both viewers to receive their first update", UPDATES_TIMEOUT)
        time.sleep(SLOW_WINDOW_SECS)
        slow_seen, fast_seen = slow.snapshot(), fast.snapshot()
        viewers = a.screen_status()["sharing"]["viewers"]
    finally:
        slow.close()
        fast.close()
    if slow_seen["updates"] >= fast_seen["updates"]:
        raise ScenarioFailure(
            f"the slow viewer received {slow_seen['updates']} updates against "
            f"the fast one's {fast_seen['updates']}; nothing was coalesced")
    if slow_seen["last_seq"] < fast_seen["last_seq"] - a.screen_status()["sharing"]["fps"] * 2:
        raise ScenarioFailure(
            f"the slow viewer fell {fast_seen['last_seq'] - slow_seen['last_seq']} "
            f"updates behind; a backlog was queued for it")
    if {v["peer"] for v in viewers} != {b.hash, c.hash}:
        raise ScenarioFailure(f"{a.tag} counted the wrong viewers: {viewers}")
    a.screen_stop()
    for peer in (a, b, c):
        peer.leave_voice()
    return {"slow": {k: slow_seen[k] for k in ("updates", "bytes", "last_seq")},
            "fast": {k: fast_seen[k] for k in ("updates", "bytes", "last_seq")}}
