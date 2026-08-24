"""
Family dm -- direct messages between two peers who hold each other as friends.

Two things here cannot be shown by pytest's in-process transport, which is
why this family exists:

  * The gate is enforced at both ends independently, so a one-sided
    friendship must produce nothing at all on the far side. Over a real
    network "refused" and "still in flight" look identical for a while --
    only holding for a stretch tells them apart.
  * A direct message to an absent peer has nobody to serve it later. It goes
    to a propagation node instead, and comes back only when the peer returns
    and asks for it. That needs a third process acting as the node, a peer
    that really goes away, and real time passing.

See docs/testenv-scenarios.md for the matrix these implement.
"""

from asserts import hold_for, settle, wait_until, ScenarioFailure
from flows import go_offline, go_online, DISCOVERY_TIMEOUT, NEGATIVE_HOLD_SECS
from scenario import PROBE, scenario

# A propagated message travels twice -- sender to node, node to recipient --
# with a link setup at each end, so it is given more room than a direct send.
PROPAGATION_TIMEOUT = 120.0


def befriend(a, b) -> None:
    """Take two peers through the handshake and wait for both sides to settle."""
    a.send_friend_request(b.hash, note="scenario")
    wait_until(lambda: a.hash in b.incoming_request_hashes(),
               f"{b.tag} to receive {a.tag}'s friend request", DISCOVERY_TIMEOUT)
    b.accept_friend_request(a.hash)
    wait_until(lambda: b.hash in a.friend_hashes(),
               f"{a.tag} to learn {b.tag} accepted", DISCOVERY_TIMEOUT)
    wait_until(lambda: a.hash in b.friend_hashes(),
               f"{b.tag} to hold {a.tag} as a friend", DISCOVERY_TIMEOUT)


def dm_holds(peer, conversation: str, expected: set[str]) -> bool:
    return expected.issubset(peer.dm_contents(conversation))


@scenario("dm1", "A friend request, accepted, carries a conversation both ways",
          peers="AB")
def f1(env):
    """The whole path over real links: request, accept, and a message each
    way. Both ends have to hold the other before anything moves at all."""
    a, b = env.peers("A", "B")
    befriend(a, b)

    conversation = a.open_dm(b.hash)
    a.send_dm(b.hash, "dm1-from-a")
    wait_until(lambda: dm_holds(b, conversation, {"dm1-from-a"}),
               f"{b.tag} to receive the direct message", DISCOVERY_TIMEOUT)

    b.send_dm(a.hash, "dm1-from-b")
    wait_until(lambda: dm_holds(a, conversation, {"dm1-from-b"}),
               f"{a.tag} to receive the reply", DISCOVERY_TIMEOUT)

    if b.open_dm(a.hash) != conversation:
        raise ScenarioFailure("the two peers derived different conversation addresses")
    return {"conversation": conversation[:12]}


@scenario("dm2", "A one-sided friendship delivers nothing", peers="AB")
def f2(env):
    """A adds B; B never adds A. The send is refused at B's end, so this asserts
    absence -- a hold, not a wait, because early silence proves nothing."""
    a, b = env.peers("A", "B")
    a.add_friend(b.hash)

    conversation = a.open_dm(b.hash)
    a.send_dm(b.hash, "dm2-should-not-land")

    hold_for(lambda: not dm_holds(b, conversation, {"dm2-should-not-land"}),
             f"{b.tag} to keep refusing a message from a non-friend",
             NEGATIVE_HOLD_SECS)
    # And the other direction is refused before it is even sent.
    status = b.try_send_dm(a.hash, "dm2-reply")
    if status != 403:
        raise ScenarioFailure(f"{b.tag} was allowed to send to a non-friend ({status})")
    return {"refused_with": status}


@scenario("dm3", "Unfriending stops the conversation in both directions",
          peers="AB")
def f3(env):
    """The transcript stays; nothing more passes. The queued-message path is
    covered in pytest -- what is checked here is the live one, over real links."""
    a, b = env.peers("A", "B")
    befriend(a, b)
    conversation = a.open_dm(b.hash)

    a.send_dm(b.hash, "dm3-before")
    wait_until(lambda: dm_holds(b, conversation, {"dm3-before"}),
               f"{b.tag} to receive the first message", DISCOVERY_TIMEOUT)

    b.remove_friend(a.hash)
    a.send_dm(b.hash, "dm3-after")
    hold_for(lambda: not dm_holds(b, conversation, {"dm3-after"}),
             f"{b.tag} to refuse messages after unfriending", NEGATIVE_HOLD_SECS)

    if not dm_holds(b, conversation, {"dm3-before"}):
        raise ScenarioFailure("unfriending discarded the existing transcript")
    return {}


@scenario("dm4", "A message to an absent friend waits at a propagation node",
          peers="ABC")
def f4(env):
    """C runs the node. B goes away, A sends, B comes back and collects.

    This is the scenario the whole propagation path exists for: a channel would
    have been caught up by any other member, and a conversation has none.
    """
    a, b, c = env.peers("A", "B", "C")
    befriend(a, b)
    conversation = a.open_dm(b.hash)

    c.update_settings(propagation_enabled=True)
    for peer in (a, b):
        wait_until(lambda peer=peer: peer.propagation()["selected"] is not None,
                   f"{peer.tag} to hear a propagation node", PROPAGATION_TIMEOUT)

    go_offline(b)
    a.send_dm(b.hash, "dm4-while-away")
    go_online(b)
    wait_until(lambda: b.collect_propagated() == 200,
               f"{b.tag} to be able to ask its node for held mail",
               PROPAGATION_TIMEOUT)
    wait_until(lambda: dm_holds(b, conversation, {"dm4-while-away"}),
               f"{b.tag} to collect the message left while away",
               PROPAGATION_TIMEOUT)
    return {"node": (c.propagation()["selected"] or "")[:12]}


@scenario("dm5", "The nearest announced node is chosen, and a pin overrides it",
          peers="ABC", kind=PROBE)
def f5(env):
    """A prediction about selection, not settled behaviour: with two nodes on
    the mesh, whether hop count actually separates them depends on the
    topology the harness builds, which is flat today."""
    a, b, c = env.peers("A", "B", "C")
    b.update_settings(propagation_enabled=True)
    c.update_settings(propagation_enabled=True)

    wait_until(lambda: len(a.propagation()["nodes"]) >= 2,
               f"{a.tag} to hear both nodes", PROPAGATION_TIMEOUT)
    automatic = a.propagation()["selected"]

    other = next(n["hash"] for n in a.propagation()["nodes"] if n["hash"] != automatic)
    a.pin_propagation_node(other)
    pinned = a.propagation()
    if pinned["selected"] != other:
        raise ScenarioFailure("pinning a node did not take effect")

    a.pin_propagation_node("")
    reselected, _ = settle(lambda: a.propagation()["selected"] is not None,
                           f"{a.tag} to re-select automatically", DISCOVERY_TIMEOUT)
    return {"automatic": (automatic or "")[:12], "pinned": other[:12],
            "reselected": reselected}
