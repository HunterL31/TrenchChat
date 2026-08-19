"""
Family servers -- servers.

A server is one membership and one role assignment shared by many channels.
The interesting property is that a single invite admits a peer to every
channel at once, and that permissions edited at the server mirror into each
child channel rather than being editable per channel.

See docs/testenv-scenarios.md for the matrix these implement.
"""

from asserts import (
    all_hold, hold_for, roster, rosters_identical, settle,
    wait_until, ScenarioFailure,
)
from flows import invite_and_accept, DISCOVERY_TIMEOUT, NEGATIVE_HOLD_SECS
from scenario import PROBE, scenario

SEND_MESSAGE = "send_message"
INVITE = "invite"
KICK = "kick"
MANAGE_ROLES = "manage_roles"
CREATE_CHANNEL = "create_channel"
FULL_SYNC = "full_sync"

ADMIN_DEFAULT = [SEND_MESSAGE, INVITE, KICK, MANAGE_ROLES, CREATE_CHANNEL]
MEMBER_DEFAULT = [SEND_MESSAGE]


def _server_with_channels(owner, name: str, channel_names: list[str]) -> tuple[str, list[str]]:
    server_hash = owner.create_server(name)
    hashes = [owner.create_server_channel(server_hash, n)["hash"] for n in channel_names]
    return server_hash, hashes


@scenario("servers1", "One invite admits a peer to every channel in a server")
def e1(env):
    a, b = env.peers("A", "B")
    server, channels = _server_with_channels(a, "e1-server", ["general", "second", "third"])

    a.invite_to_server(server, b.hash)
    invite_and_accept(a, b, server)

    wait_until(lambda: len(b.server_channels(server)) == len(channels),
               "B to receive every channel in the server", DISCOVERY_TIMEOUT)
    rosters_identical([a, b], server, timeout=DISCOVERY_TIMEOUT)

    # Prove the membership is real on a channel that was never invited to
    # directly -- the third one, not the first.
    a.send(channels[-1], "to-the-last-channel")
    all_hold([b], channels[-1], {"to-the-last-channel"}, timeout=DISCOVERY_TIMEOUT)
    return {"channels": len(b.server_channels(server))}


@scenario("servers2", "An admin with create_channel adds a channel everyone receives")
def e2(env):
    a, b, c = env.peers("A", "B", "C")
    server, channels = _server_with_channels(a, "e2-server", ["general"])

    for peer in (b, c):
        a.invite_to_server(server, peer.hash)
        invite_and_accept(a, peer, server)

    a.set_server_permissions(server, admin=ADMIN_DEFAULT, member=MEMBER_DEFAULT)
    a.set_server_roles(server, add_admins=[b.hash])
    wait_until(lambda: roster(b, server).get(b.hash) == "admin",
               "B to become a server admin", DISCOVERY_TIMEOUT)

    status, body = b.post_status(f"/servers/{server}/channels", {"name": "added-by-B"})
    if status != 200:
        raise ScenarioFailure(f"an admin with create_channel was refused: {status} {body}")

    expected = len(channels) + 1
    for peer in (a, c):
        wait_until(lambda peer=peer: len(peer.server_channels(server)) == expected,
                   f"{peer.tag} to receive the new channel", DISCOVERY_TIMEOUT)
    return {"channels": expected}


@scenario("servers3", "Server permissions mirror into channels and cannot be overridden")
def e3(env):
    a, b = env.peers("A", "B")
    server, channels = _server_with_channels(a, "e3-server", ["general", "second"])
    a.invite_to_server(server, b.hash)
    invite_and_accept(a, b, server)

    a.set_server_permissions(server, admin=ADMIN_DEFAULT,
                             member=MEMBER_DEFAULT + [FULL_SYNC])
    for ch in channels:
        wait_until(lambda ch=ch: FULL_SYNC in a.permissions(ch)["member"],
                   f"the grant to mirror into {ch[:8]}", DISCOVERY_TIMEOUT)

    status, body = a.post_status(f"/channels/{channels[0]}/permissions",
                                 {"admin": [], "member": []})
    if status != 409:
        raise ScenarioFailure(
            f"a per-channel override of server permissions was not refused: {status} {body}"
        )
    hold_for(lambda: FULL_SYNC in a.permissions(channels[0])["member"],
             "the mirrored grant to survive the refused override", NEGATIVE_HOLD_SECS)
    return {"mirrored_to": len(channels)}


@scenario("servers4", "Leaving a server drops every one of its channels")
def e4(env):
    a, b, c = env.peers("A", "B", "C")
    server, channels = _server_with_channels(a, "e4-server", ["general", "second"])
    for peer in (b, c):
        a.invite_to_server(server, peer.hash)
        invite_and_accept(a, peer, server)

    a.send(channels[0], "before-leave")
    all_hold([b, c], channels[0], {"before-leave"}, timeout=DISCOVERY_TIMEOUT)

    b.leave_server(server)
    joined = {ch["hash"] for ch in b.channels()}
    if joined & set(channels):
        raise ScenarioFailure(f"B still holds server channels after leaving: {joined}")

    a.send(channels[0], "after-leave")
    all_hold([c], channels[0], {"before-leave", "after-leave"}, timeout=DISCOVERY_TIMEOUT)
    hold_for(lambda: "after-leave" not in b.contents(channels[0]),
             "B to receive nothing after leaving", NEGATIVE_HOLD_SECS)
    return {}


@scenario("servers5", "Kicking from a server removes a peer from all its channels")
def e5(env):
    a, b, c = env.peers("A", "B", "C")
    server, channels = _server_with_channels(a, "e5-server", ["general", "second"])
    for peer in (b, c):
        a.invite_to_server(server, peer.hash)
        invite_and_accept(a, peer, server)

    if not a.set_server_roles(server, remove_members=[c.hash]):
        raise ScenarioFailure("the owner's server kick was rejected")
    wait_until(lambda: c.hash not in roster(a, server),
               "C to be dropped from the server roster", DISCOVERY_TIMEOUT)
    wait_until(lambda: c.hash not in roster(b, server),
               "B to see C dropped", DISCOVERY_TIMEOUT)

    c.send(channels[0], "after-server-kick")
    hold_for(lambda: "after-server-kick" not in a.contents(channels[0]),
             "a server-kicked peer's message to be rejected", NEGATIVE_HOLD_SECS)
    return {"remaining": len(roster(a, server))}


@scenario("servers6", "A server-level full_sync grant backfills every channel", kind=PROBE)
def e6(env):
    """Server-scoped tenure has to resolve per channel for this to work, so it
    is worth checking separately from invite3's single-channel case."""
    a, b = env.peers("A", "B")
    server, channels = _server_with_channels(a, "e6-server", ["general", "second"])
    a.set_server_permissions(server, admin=ADMIN_DEFAULT + [FULL_SYNC],
                             member=MEMBER_DEFAULT + [FULL_SYNC])

    backlog = {f"e6-{i}" for i in range(3)}
    for ch in channels:
        for content in sorted(backlog):
            a.send(ch, content)

    a.invite_to_server(server, b.hash)
    invite_and_accept(a, b, server)

    results = {}
    for i, ch in enumerate(channels):
        got, secs = settle(lambda ch=ch: b.contents(ch) == backlog,
                           f"B to backfill channel {i}", 90.0)
        results[f"channel_{i}"] = {"backfilled": got,
                                   "held": len(b.contents(ch)),
                                   "secs": round(secs, 1) if got else None}
    notes = {"per_channel": results}
    if not all(r["backfilled"] for r in results.values()):
        notes["surprise"] = "a server full_sync grant did not backfill every channel"
    return notes
