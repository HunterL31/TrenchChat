"""
Family invite -- invite-only channels, membership and permissions.

Membership here is a signed member-list document, not a subscription: roles
exist, tenure is recorded, and every permission is enforced at the core
regardless of what a client sends. That makes this the family where a
permission grant and its absence are both worth asserting -- a check that
only ever runs against a compliant client proves nothing.

See docs/testenv-scenarios.md for the matrix these implement.
"""

from asserts import (
    all_hold, discovered_hashes, hold_for, roster, roster_views,
    rosters_identical, settle, wait_until, ScenarioFailure,
)
from flows import (
    invite_and_accept, invite_only_channel, DISCOVERY_TIMEOUT, NEGATIVE_HOLD_SECS,
)
from scenario import PROBE, scenario

# Permission strings, as the API takes them.
SEND_MESSAGE = "send_message"
INVITE = "invite"
KICK = "kick"
MANAGE_ROLES = "manage_roles"
FULL_SYNC = "full_sync"

ADMIN_DEFAULT = [SEND_MESSAGE, INVITE, KICK, MANAGE_ROLES]
MEMBER_DEFAULT = [SEND_MESSAGE]


@scenario("invite1", "An invite-only channel is never announced")
def b1(env):
    a, b, c, d = env.peers("A", "B", "C", "D")
    ch = a.create_channel("b1-private", "invite")

    # Nothing to wait for -- proving absence means holding the window open.
    hold_for(lambda: all(ch not in discovered_hashes(p) for p in (b, c, d)),
             "the channel to stay undiscovered", NEGATIVE_HOLD_SECS)
    if ch not in {c["hash"] for c in a.channels()}:
        raise ScenarioFailure("creator does not hold its own invite-only channel")
    return {}


@scenario("invite2", "An accepted invite produces one agreed roster")
def b2(env):
    a, b = env.peers("A", "B")
    ch = invite_only_channel(a, [b], "b2-private")

    agreed = rosters_identical([a, b], ch, timeout=DISCOVERY_TIMEOUT)
    if agreed.get(a.hash) != "owner" or agreed.get(b.hash) != "member":
        raise ScenarioFailure(f"unexpected roles: {roster_views([a, b], ch)}")

    a.send(ch, "after-admit")
    all_hold([b], ch, {"after-admit"}, timeout=DISCOVERY_TIMEOUT)
    return {"members": len(agreed)}


@scenario("invite3", "full_sync decides whether a new member sees history")
def b3(env):
    """The real full_sync test. Tenure filtering only engages on a channel that
    records tenure, which public channels never do (see public6), so this is the
    only place the permission actually changes an outcome."""
    a, b, c = env.peers("A", "B", "C")

    plain = a.create_channel("b3-plain", "invite")
    granted = a.create_channel("b3-fullsync", "invite")
    a.set_permissions(granted, admin=ADMIN_DEFAULT + [FULL_SYNC],
                      member=MEMBER_DEFAULT + [FULL_SYNC])

    backlog = {f"b3-{i}" for i in range(3)}
    for ch in (plain, granted):
        for content in sorted(backlog):
            a.send(ch, content)

    a.invite(plain, b.hash)
    invite_and_accept(a, b, plain)
    a.invite(granted, c.hash)
    invite_and_accept(a, c, granted)

    got_granted, granted_secs = settle(lambda: c.contents(granted) == backlog,
                                       "C to backfill with full_sync granted", 90.0)
    # The negative case only means anything once the positive one has landed:
    # until then "nothing yet" and "nothing ever" look identical.
    withheld = b.contents(plain)

    notes = {
        "with_full_sync": len(c.contents(granted)),
        "without_full_sync": len(withheld),
        "backfill_secs": round(granted_secs, 1) if got_granted else None,
    }
    if not got_granted:
        raise ScenarioFailure(f"full_sync did not backfill history: {notes}")
    if withheld:
        raise ScenarioFailure(
            f"history leaked to a member without full_sync: {sorted(withheld)}"
        )
    return notes


@scenario("invite4", "Four members converge on one roster")
def b4(env):
    a, b, c, d = env.peers("A", "B", "C", "D")
    ch = invite_only_channel(a, [b, c, d], "b4-private")

    agreed = rosters_identical([a, b, c, d], ch, timeout=DISCOVERY_TIMEOUT)
    if len(agreed) != 4:
        raise ScenarioFailure(f"expected 4 members, got {roster_views([a, b, c, d], ch)}")

    a.send(ch, "to-everyone")
    all_hold([b, c, d], ch, {"to-everyone"}, timeout=DISCOVERY_TIMEOUT)
    return {"members": len(agreed), "roles": sorted(set(agreed.values()))}


@scenario("invite5", "A declined invite admits nobody")
def b5(env):
    a, b, c = env.peers("A", "B", "C")
    ch = invite_only_channel(a, [b], "b5-private")

    a.invite(ch, c.hash)
    wait_until(lambda: any(i["channel_hash_hex"] == ch for i in c.invites()),
               "C to receive the invite", DISCOVERY_TIMEOUT)
    c.decline_invite(ch)

    hold_for(lambda: c.hash not in roster(a, ch) and c.hash not in roster(b, ch),
             "C to stay out of the roster", NEGATIVE_HOLD_SECS)
    if c.invites():
        raise ScenarioFailure("declined invite is still pending for C")
    return {}


@scenario("invite6", "A kicked member is dropped everywhere and can no longer send")
def b6(env):
    a, b, c, d = env.peers("A", "B", "C", "D")
    ch = invite_only_channel(a, [b, c, d], "b6-private")

    a.send(ch, "before-kick")
    all_hold([b, c, d], ch, {"before-kick"}, timeout=DISCOVERY_TIMEOUT)

    if not a.set_roles(ch, remove_members=[c.hash]):
        raise ScenarioFailure("owner's kick was rejected")

    wait_until(lambda: all(c.hash not in roster(p, ch) for p in (a, b, d)),
               "C to be dropped from every roster", DISCOVERY_TIMEOUT)

    c.send(ch, "after-kick")
    hold_for(lambda: all("after-kick" not in p.contents(ch) for p in (a, b, d)),
             "a kicked member's message to be rejected", NEGATIVE_HOLD_SECS)
    return {"remaining": len(roster(a, ch))}


@scenario("invite7", "A promoted admin can invite")
def b7(env):
    a, b, c, d = env.peers("A", "B", "C", "D")
    ch = invite_only_channel(a, [b, c], "b7-private")

    if not a.set_roles(ch, add_admins=[b.hash]):
        raise ScenarioFailure("promotion was rejected")
    wait_until(lambda: roster(b, ch).get(b.hash) == "admin",
               "B to see itself as admin", DISCOVERY_TIMEOUT)

    b.invite(ch, d.hash)
    invite_and_accept(b, d, ch)

    agreed = rosters_identical([a, b, c, d], ch, timeout=DISCOVERY_TIMEOUT)
    if agreed.get(b.hash) != "admin" or agreed.get(d.hash) != "member":
        raise ScenarioFailure(f"unexpected roles: {roster_views([a, b, c, d], ch)}")
    return {"members": len(agreed)}


@scenario("invite8", "A demoted member cannot invite")
def b8(env):
    a, b, d = env.peers("A", "B", "D")
    ch = invite_only_channel(a, [b], "b8-private")

    a.set_roles(ch, add_admins=[b.hash])
    wait_until(lambda: roster(b, ch).get(b.hash) == "admin", "B to become admin",
               DISCOVERY_TIMEOUT)
    a.set_roles(ch, remove_admins=[b.hash])
    wait_until(lambda: roster(b, ch).get(b.hash) == "member", "B to be demoted",
               DISCOVERY_TIMEOUT)

    b.invite(ch, d.hash)
    # D may still be offered the invite -- the guard is that the join request it
    # produces is refused, so D never lands in anyone's roster.
    settle(lambda: any(i["channel_hash_hex"] == ch for i in d.invites()),
           "D to be offered the invite", 20.0)
    d.accept_invite(ch) if d.invites() else None

    hold_for(lambda: d.hash not in roster(a, ch),
             "D to stay out of the roster", NEGATIVE_HOLD_SECS)
    return {"members": len(roster(a, ch))}


@scenario("invite9", "Revoking send_message silences members but not admins")
def b9(env):
    a, b, c = env.peers("A", "B", "C")
    ch = invite_only_channel(a, [b, c], "b9-private")
    a.set_roles(ch, add_admins=[b.hash])
    wait_until(lambda: roster(b, ch).get(b.hash) == "admin", "B to become admin",
               DISCOVERY_TIMEOUT)

    a.set_permissions(ch, admin=ADMIN_DEFAULT, member=[])
    wait_until(lambda: SEND_MESSAGE not in c.permissions(ch)["member"],
               "C to see send_message revoked", DISCOVERY_TIMEOUT)

    c.send(ch, "from-silenced-member")
    b.send(ch, "from-admin")

    all_hold([a, c], ch, {"from-admin"}, timeout=DISCOVERY_TIMEOUT)
    hold_for(lambda: "from-silenced-member" not in a.contents(ch),
             "the silenced member's message to stay rejected", NEGATIVE_HOLD_SECS)
    return {}


@scenario("invite10", "A member cannot kick the owner")
def b10(env):
    """Adversarial: calls the roles endpoint directly, which is the same core
    path a malicious client would reach. The UI gate is not involved."""
    a, b, c = env.peers("A", "B", "C")
    ch = invite_only_channel(a, [b, c], "b10-private")

    applied = c.set_roles(ch, remove_members=[a.hash])
    if applied:
        raise ScenarioFailure("a plain member's kick of the owner was accepted")

    hold_for(lambda: a.hash in roster(a, ch) and a.hash in roster(b, ch),
             "the owner to stay in every roster", NEGATIVE_HOLD_SECS)
    return {"rejected": True}


@scenario("invite11", "A granted kick permission actually takes effect")
def b11(env):
    """The mirror of invite10: proves the rejection there is the permission working,
    not the endpoint refusing everything."""
    a, b, c = env.peers("A", "B", "C")
    ch = invite_only_channel(a, [b, c], "b11-private")

    a.set_permissions(ch, admin=ADMIN_DEFAULT, member=MEMBER_DEFAULT + [KICK])
    wait_until(lambda: KICK in c.permissions(ch)["member"],
               "C to see the kick grant", DISCOVERY_TIMEOUT)

    if not c.set_roles(ch, remove_members=[b.hash]):
        raise ScenarioFailure("a granted kick was rejected")
    wait_until(lambda: b.hash not in roster(a, ch),
               "B to be dropped from the owner's roster", DISCOVERY_TIMEOUT)
    return {"remaining": len(roster(a, ch))}


@scenario("invite12", "Two roster changes at once still converge", kind=PROBE)
def b12(env):
    """Both documents validate against stored state, so the question is only
    whether the peers agree on the result rather than splitting."""
    a, b, c, d = env.peers("A", "B", "C", "D")
    ch = invite_only_channel(a, [b, c, d], "b12-private")
    a.set_roles(ch, add_admins=[b.hash])
    wait_until(lambda: roster(b, ch).get(b.hash) == "admin", "B to become admin",
               DISCOVERY_TIMEOUT)

    a.set_roles(ch, remove_members=[c.hash])
    b.set_roles(ch, remove_members=[d.hash])

    agreed, _ = settle(
        lambda: len({tuple(sorted(roster(p, ch).items())) for p in (a, b)}) == 1,
        "A and B to agree on the roster", 90.0,
    )
    notes = {"converged": agreed, "views": roster_views([a, b], ch)}
    if not agreed:
        notes["surprise"] = "concurrent roster changes left the peers disagreeing"
    return notes


@scenario("invite13", "A member cannot edit channel permissions")
def b13(env):
    a, b, c = env.peers("A", "B", "C")
    ch = invite_only_channel(a, [b, c], "b13-private")
    before = a.permissions(ch)

    if c.set_permissions(ch, admin=[], member=ADMIN_DEFAULT):
        raise ScenarioFailure("a plain member's permission edit was accepted")

    hold_for(lambda: a.permissions(ch) == before,
             "stored permissions to stay unchanged", NEGATIVE_HOLD_SECS)
    return {"rejected": True}
