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

upgrade6 to upgrade8 are the file plane on this path, because the files family
switches sessions off to stay on the mesh: what a pull costs here, a holder
that dies with one in flight, and a downloader killed and restarted.

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

# A pull over a session that is already up has no dial and no path to resolve,
# so this is a ceiling on the transfer itself and nothing else.
FETCH_OVER_A_SESSION = 300.0


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


# What upgrade5 and upgrade6 need from the other families, imported rather
# than copied so a change to how a file is shared or a voice mesh is waited on
# reaches these rows too.
from scen_files import (                                          # noqa: E402
    _await_manifest, _await_done, _await_partial, _chunks,
    _no_download_because_it_finished, _payload, _share, _status, _verify,
    BIG_BODY_TIMEOUT,
)
from scen_voice import (                                          # noqa: E402
    _await_mesh, _join_voice_all, _rx_quality, MESH_TIMEOUT, TONE_WINDOW_SECS,
)

# Tens of megabytes: large enough that the two paths are telling different
# stories about the same file, small enough to be a scenario and not an
# afternoon. 640 chunks.
SIZE_20MB = 20 * 1024 * 1024

# The mesh plane asks sixteen chunks at a time where the direct plane asks two
# hundred and fifty six, so the mesh side of upgrade6 is the long one.
MESH_FETCH_TIMEOUT = 1800.0

# Twenty megabytes cross a session in about two seconds, so a poll that wants
# to see the middle of one has to be quicker than the files family's.
DIRECT_SAMPLE_SECS = 0.05

# What a holder that stops answering costs here. A killed process's socket
# is closed by its kernel, so the session fails at once and the next member is
# asked in about a second; this is the ceiling for the case where it is not
# noticed and a whole stall timeout is spent on the dead holder first.
HOLDER_REPLACEMENT_TIMEOUT = 420.0


@scenario("upgrade5", "A voice session with a direct pair and a mesh pair in it",
          peers="ABC")
def u5(env):
    """One encoder, two planes, and a roster that says which is which.

    C has direct connections switched off, which is the client gate: it never
    offers one and never answers one, so its pairs stay on the mesh while A and
    B upgrade between themselves. Every pair still has to stream, because a
    session that carried only its fast half would be worse than one that had
    never upgraded at all.
    """
    a, b, c = env.peers("A", "B", "C")
    c.set_direct_connections(False)
    channel = invite_only_channel(a, [b, c], "upgrade5-room")

    await_upgrade(a, b)
    hold_for(lambda: not _upgraded(a, c) and not _upgraded(b, c),
             f"{c.tag} to hold no session with the switch off", 10.0)

    _join_voice_all([a, b, c], channel)
    mesh_secs = _await_mesh([a, b, c], channel)

    paths = {peer.tag: peer.voice_paths(channel) for peer in (a, b, c)}
    if paths["A"].get(b.hash) != PATH_DIRECT:
        raise ScenarioFailure(
            f"{a.tag}'s voice roster puts {b.tag} on {paths['A'].get(b.hash)}")
    if paths["A"].get(c.hash) != PATH_RETICULUM:
        raise ScenarioFailure(
            f"{a.tag}'s voice roster puts {c.tag} on {paths['A'].get(c.hash)}")
    if paths["C"].get(a.hash) != PATH_RETICULUM or \
            paths["C"].get(b.hash) != PATH_RETICULUM:
        raise ScenarioFailure(f"{c.tag} reads a direct pair it cannot have: "
                              f"{paths['C']}")
    if paths["A"].get(a.hash) is not None:
        raise ScenarioFailure(f"{a.tag} gave itself a path: {paths['A']}")

    for peer in (a, b, c):
        status, body = peer.set_test_tone(True)
        if status != 200:
            raise ScenarioFailure(f"{peer.tag} has no tone pipeline: {status} {body}")
        peer.set_voice_muted(False)

    pairs = [(listener, sender) for listener in (a, b, c)
             for sender in (a, b, c) if listener is not sender]
    wait_until(lambda: all(_rx_quality(listener, sender).get("received", 0) > 0
                           for listener, sender in pairs),
               "every pair to carry frames, on whichever plane it is on",
               MESH_TIMEOUT)
    time.sleep(TONE_WINDOW_SECS)

    received = {f"{listener.tag}<-{sender.tag}":
                _rx_quality(listener, sender).get("received", 0)
                for listener, sender in pairs}
    silent = [pair for pair, frames in received.items() if frames < 100]
    for peer in (a, b, c):
        peer.leave_voice()
    if silent:
        raise ScenarioFailure(f"pairs that carried almost nothing: {silent}")
    return {"mesh_secs": round(mesh_secs, 1), "received": received,
            "bitrate_bps": a.voice_status()["stats"].get("bitrate_bps"),
            "paths": {tag: sorted(set(p for p in row.values() if p))
                      for tag, row in paths.items()}}


@scenario("upgrade6", "Twenty megabytes in seconds on one path and at the "
                      "mesh's pace on the other", peers="ABC")
def u6(env):
    """The size the direct path is for, and the cost of it on the other one.

    Nobody pays for a file they did not ask for: that is what lets a share be
    two hundred megabytes at all. C, with direct connections off, pulls the
    same file over the mesh plane sixteen chunks at a time, and the ratio
    between the two numbers is what this row is for.
    """
    a, b, c = env.peers("A", "B", "C")
    c.set_direct_connections(False)
    channel = invite_only_channel(a, [b, c], "upgrade6-room")
    await_upgrade(a, b)

    data = _payload(SIZE_20MB, 6)
    a.set_http_timeout(BIG_BODY_TIMEOUT)
    message_id, file_hash = _share(a, channel, "upgrade6-20mb.bin", data,
                                   "upgrade6-file")
    a.set_http_timeout()

    secs = {}
    for peer, timeout in ((b, FETCH_OVER_A_SESSION), (c, MESH_FETCH_TIMEOUT)):
        _await_manifest(peer, channel, "upgrade6-file", message_id)
        started = peer.start_file_fetch(channel, file_hash, message_id)
        if not started.get("ok"):
            raise ScenarioFailure(f"{peer.tag} was refused the download: {started}")
        secs[peer.tag] = round(_await_done(peer, channel, file_hash, timeout), 1)
        peer.set_http_timeout(BIG_BODY_TIMEOUT)
        _verify(peer, channel, file_hash, data)
        peer.set_http_timeout()

    if secs["B"] >= secs["C"]:
        raise ScenarioFailure(
            f"the direct pull took {secs['B']}s against the mesh's {secs['C']}s")
    return {"file_bytes": len(data), "direct_secs": secs["B"],
            "mesh_secs": secs["C"],
            "ratio": round(secs["C"] / secs["B"], 1) if secs["B"] else None}


@scenario("upgrade7", "A holder that dies mid-transfer is replaced over a "
                      "session", peers="ABC")
def u7(env):
    """files6's claim on this path, where the whole transfer is seconds long.

    Both holders are reached over a session, so what B loses when A dies is
    the request in flight and the session carrying it, and what it keeps is
    every chunk already verified. Twenty megabytes rather than files6's two,
    because two would be over before a poll could see the middle of it.
    """
    a, b, c = env.peers("A", "B", "C")
    channel = invite_only_channel(a, [b, c], "upgrade7-room")
    await_upgrade(a, b)
    await_upgrade(a, c)
    await_upgrade(b, c)

    data = _payload(SIZE_20MB, 7)
    total = _chunks(len(data))
    a.set_http_timeout(BIG_BODY_TIMEOUT)
    message_id, file_hash = _share(a, channel, "upgrade7-20mb.bin", data,
                                   "upgrade7-file")
    a.set_http_timeout()

    _await_manifest(c, channel, "upgrade7-file", message_id)
    if not c.start_file_fetch(channel, file_hash, message_id).get("ok"):
        raise ScenarioFailure(f"{c.tag} was refused the download")
    _await_done(c, channel, file_hash, FETCH_OVER_A_SESSION)

    _await_manifest(b, channel, "upgrade7-file", message_id)
    if not b.start_file_fetch(channel, file_hash, message_id).get("ok"):
        raise ScenarioFailure(f"{b.tag} was refused the download")
    held_at_kill = _await_partial(b, channel, file_hash, total,
                                  DIRECT_SAMPLE_SECS)
    paths = {peer.tag: b.member_path(channel, peer.hash) for peer in (a, c)}
    if set(paths.values()) != {PATH_DIRECT}:
        raise ScenarioFailure(
            f"{b.tag} is not pulling over a session: {paths}")

    env.orch.kill(a.tag)
    wait_until(lambda: not a.alive(), f"{a.tag}'s process to go away", 60.0)

    held: list[int] = []

    def _finished() -> bool:
        status = _status(b, channel, file_hash)
        held.append(status.get("chunks_held", 0))
        return status.get("state") == "done"

    secs = wait_until(_finished, f"{b.tag} to finish from the other holder",
                      HOLDER_REPLACEMENT_TIMEOUT, interval=0.5)
    b.set_http_timeout(BIG_BODY_TIMEOUT)
    _verify(b, channel, file_hash, data)
    b.set_http_timeout()

    notes = {"file_bytes": len(data), "chunks": total,
             "chunks_held_at_the_poll": held_at_kill,
             "chunks_held_when_the_sender_died": held[0],
             "chunks_taken_from_the_other_holder": total - held[0],
             "finish_secs": round(secs, 1),
             "path_to_the_other_holder": b.member_path(channel, c.hash)}
    if held[0] >= total:
        raise ScenarioFailure(
            f"{b.tag} had the whole file before {a.tag} died, so this run "
            f"measures nothing: {notes}")
    dropped = [(held[i], held[i + 1]) for i in range(len(held) - 1)
               if held[i + 1] < held[i]]
    if dropped:
        raise ScenarioFailure(
            f"{b.tag}'s verified chunk count went backwards {dropped}, so "
            f"work already paid for was re-fetched: {notes}")
    if min(held) < held_at_kill:
        raise ScenarioFailure(
            f"{b.tag} resumed below the {held_at_kill} chunks it held when "
            f"the sender died: {notes}")
    if a.alive():
        raise ScenarioFailure(f"{a.tag} came back to life before {b.tag} finished")
    return notes


@scenario("upgrade8", "A restarted downloader keeps what its session stored",
          peers="AB")
def u8(env):
    """files7's claim on this path: a process kill mid-transfer, and a resume.

    A verified chunk is a row and a slot on disk whichever plane carried it,
    so what the restart costs is the request in flight and the session it was
    on. What this row adds is the trigger afterwards: nothing re-asks, and the
    pair coming back is a sighting like any other.
    """
    a, b = env.peers("A", "B")
    channel = invite_only_channel(a, [b], "upgrade8-room")
    await_upgrade(a, b)

    data = _payload(SIZE_20MB, 8)
    total = _chunks(len(data))
    a.set_http_timeout(BIG_BODY_TIMEOUT)
    message_id, file_hash = _share(a, channel, "upgrade8-20mb.bin", data,
                                   "upgrade8-file")
    a.set_http_timeout()

    _await_manifest(b, channel, "upgrade8-file", message_id)
    if not b.start_file_fetch(channel, file_hash, message_id).get("ok"):
        raise ScenarioFailure(f"{b.tag} was refused the download")
    held_before = _await_partial(b, channel, file_hash, total,
                                 DIRECT_SAMPLE_SECS)
    if b.member_path(channel, a.hash) != PATH_DIRECT:
        raise ScenarioFailure(f"{b.tag} is not pulling over a session")

    env.orch.kill(b.tag)
    wait_until(lambda: not b.alive(), f"{b.tag}'s process to die", 60.0)
    env.orch.start(b.tag)
    env.wait_alive(b)

    restored = _status(b, channel, file_hash)
    if not restored and _no_download_because_it_finished(b, channel, file_hash):
        raise ScenarioFailure(
            f"{b.tag} finished the file before the kill landed, so this run "
            f"measures nothing: there was no download left to resume")
    if restored.get("chunks_held", 0) < held_before:
        raise ScenarioFailure(
            f"{b.tag} came back holding {restored.get('chunks_held')} of the "
            f"{held_before} chunks it had verified: {restored}")
    restored_held = restored["chunks_held"]
    resume_secs = wait_until(
        lambda: _status(b, channel, file_hash).get("chunks_held", 0)
        > restored_held, f"{b.tag} to resume without being asked again",
        FETCH_OVER_A_SESSION)
    secs = _await_done(b, channel, file_hash, FETCH_OVER_A_SESSION)
    b.set_http_timeout(BIG_BODY_TIMEOUT)
    _verify(b, channel, file_hash, data)
    b.set_http_timeout()
    return {"file_bytes": len(data), "chunks": total,
            "chunks_held_before_kill": held_before,
            "chunks_held_after_restart": restored_held,
            "resume_secs": round(resume_secs, 1),
            "finish_secs": round(secs, 1),
            "path_when_it_finished": b.member_path(channel, a.hash)}
