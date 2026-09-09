"""
Family rrc -- public chat over Reticulum Relay Chat.

One tester hosts an rrc.hub, the others hear its announce, link to it,
join a room and talk. This is the layer pytest's FakeRRCTransport cannot
reach: real announce propagation, real path resolution, a real Link, and
identify racing the first HELLO.

The properties worth proving on a real network are the ones RRC is honest
about. A hub keeps nothing, so a client that was not there when a line was
said never gets it, and a link that drops takes the whole session with it.
Both are tested here as behaviour rather than treated as defects.

Manual interop check (not automated): run rrcd from its own checkout against
a testenv TCP interface, join a room on it from TrenchChat, and point
rrc-gui or rrc-web at a TrenchChat-hosted hub. Our own suite agreeing with
itself proves nothing about compatibility.

See docs/testenv-scenarios.md for the matrix these implement.
"""

from asserts import hold_for, settle, wait_until, ScenarioFailure
from scenario import scenario

# A hub announces when hosting is switched on and then on a long interval,
# so a missed announce is re-triggered by re-enabling rather than waited out.
_DISCOVER_CHUNK_SECS = 30.0
_DISCOVER_ATTEMPTS = 3
_CONVERGE_SECS = 45.0
_ROOM = "#general"


def _host_and_discover(host, *clients, hub_name: str = "scenario hub") -> str:
    """Switch hosting on and wait until every client has heard the announce.

    Returns the hub's destination hash, which is what a client dials.
    """
    hub_hash = None
    for _ in range(_DISCOVER_ATTEMPTS):
        status = host.rrc_set_hosting(enabled=True, hub_name=hub_name)
        hub_hash = status["hub_hash"]
        heard, _ = settle(
            lambda: all(any(h["hash"] == hub_hash for h in c.rrc_hubs())
                        for c in clients),
            f"{[c.tag for c in clients]} to hear {host.tag}'s hub announce",
            _DISCOVER_CHUNK_SECS)
        if heard:
            break
    if hub_hash is None:
        raise ScenarioFailure(f"{host.tag} did not report a hub address")
    missing = [c.tag for c in clients
               if not any(h["hash"] == hub_hash for h in c.rrc_hubs())]
    if missing:
        raise ScenarioFailure(
            f"{missing} never heard {host.tag}'s rrc.hub announce")
    return hub_hash


def _join(peer, hub_hash: str, room: str = _ROOM) -> None:
    peer.rrc_connect(hub_hash)
    wait_until(lambda: peer.rrc_state()["session"]["state"] == "active",
               f"{peer.tag} to be welcomed by the hub", _CONVERGE_SECS)
    peer.rrc_join(room)
    wait_until(lambda: peer.rrc_rooms().get(room) == "joined",
               f"{peer.tag} to join {room}", _CONVERGE_SECS)


@scenario("rrc1", "A hub is announced, heard and linked to", peers="AB")
def rrc1(env):
    """The whole path: announce, discover, dial, identify, HELLO, WELCOME."""
    a, b = env.peers("A", "B")
    hub_hash = _host_and_discover(a, b)
    b.rrc_connect(hub_hash)
    wait_until(lambda: b.rrc_state()["session"]["state"] == "active",
               "B to be welcomed", _CONVERGE_SECS)
    session = b.rrc_state()["session"]
    if session["name"] != "scenario hub":
        raise ScenarioFailure(
            f"B saw hub name {session['name']!r}, not the one A advertised")
    if a.rrc_hosting()["clients"] != 1:
        raise ScenarioFailure("A's hub does not report B as connected")


@scenario("rrc2", "Two clients talk in a room on a third node's hub",
          peers="ABC")
def rrc2(env):
    """The ordinary case, and the one a person actually uses."""
    a, b, c = env.peers("A", "B", "C")
    hub_hash = _host_and_discover(a, b, c)
    _join(b, hub_hash)
    _join(c, hub_hash)
    wait_until(lambda: a.rrc_hosting()["rooms"].get(_ROOM) == 2,
               "the hub to hold both clients in the room", _CONVERGE_SECS)

    b.rrc_say(_ROOM, "hello from B")
    wait_until(lambda: "hello from B" in c.rrc_texts(_ROOM),
               "C to receive B's line", _CONVERGE_SECS)
    c.rrc_say(_ROOM, "and back from C")
    wait_until(lambda: "and back from C" in b.rrc_texts(_ROOM),
               "B to receive C's line", _CONVERGE_SECS)


@scenario("rrc3", "A client that was not there does not get the backlog",
          peers="ABC")
def rrc3(env):
    """RRC's central bargain, stated as a test rather than a caveat.

    A hub buffers nothing, so a line said before C arrived is gone for C.
    Asserting this keeps anyone from quietly adding a store to the hub and
    calling it an improvement.
    """
    a, b, c = env.peers("A", "B", "C")
    hub_hash = _host_and_discover(a, b, c)
    _join(b, hub_hash)
    b.rrc_say(_ROOM, "said before C arrived")

    _join(c, hub_hash)
    hold_for(lambda: "said before C arrived" not in c.rrc_texts(_ROOM),
             "C to stay without a backlog it was not present for", 15.0)

    b.rrc_say(_ROOM, "said after C arrived")
    wait_until(lambda: "said after C arrived" in c.rrc_texts(_ROOM),
               "C to receive a line said while it was present", _CONVERGE_SECS)


@scenario("rrc4", "Parting stops delivery and empties the room", peers="ABC")
def rrc4(env):
    a, b, c = env.peers("A", "B", "C")
    hub_hash = _host_and_discover(a, b, c)
    _join(b, hub_hash)
    _join(c, hub_hash)

    c.rrc_part(_ROOM)
    wait_until(lambda: _ROOM not in c.rrc_rooms(),
               "C to leave the room", _CONVERGE_SECS)
    b.rrc_say(_ROOM, "C should not see this")
    hold_for(lambda: "C should not see this" not in c.rrc_texts(_ROOM),
             "C to receive nothing in a room it parted", 15.0)

    b.rrc_part(_ROOM)
    wait_until(lambda: a.rrc_hosting()["rooms"].get(_ROOM) is None,
               "the room to stop existing once empty", _CONVERGE_SECS)


@scenario("rrc5", "A hub going away takes the session with it", peers="AB")
def rrc5(env):
    """A link is the session. There is no continuity across one, and the
    client has to show that rather than leaving rooms looking joined."""
    a, b = env.peers("A", "B")
    hub_hash = _host_and_discover(a, b)
    _join(b, hub_hash)

    a.rrc_set_hosting(enabled=False)
    wait_until(lambda: b.rrc_state()["session"]["state"] != "active",
               "B's session to end when the hub stops", _CONVERGE_SECS)
    if b.rrc_rooms():
        raise ScenarioFailure(
            f"B still holds rooms {b.rrc_rooms()} after losing the hub")

    a.rrc_set_hosting(enabled=True, hub_name="scenario hub")
    _join(b, hub_hash)
    if b.rrc_texts(_ROOM):
        raise ScenarioFailure(
            "B's transcript survived a new session; each link is a new one")
