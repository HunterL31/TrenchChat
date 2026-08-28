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
    all_hold, discovered_hashes, hold_for, joined_hashes, roster, roster_views,
    rosters_identical, settle, wait_until, ScenarioFailure,
)
from flows import (
    go_offline, go_online, invite_and_accept, invite_only_channel, offer_invite,
    DISCOVERY_TIMEOUT, NEGATIVE_HOLD_SECS, RECONNECT_TIMEOUT,
)
from scenario import PROBE, scenario

# Permission strings, as the API takes them.
SEND_MESSAGE = "send_message"
INVITE = "invite"
KICK = "kick"
MANAGE_ROLES = "manage_roles"
MANAGE_CHANNEL = "manage_channel"
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


@scenario("invite11", "Kick cannot be granted to the base member role")
def b11(env):
    """Replaces the earlier expectation that granting kick to members works.

    Removing someone from the member list strips every permission they had,
    so kick is the authority to unmake other people's -- granting it to the
    base role would let every member do that to every other. The grant is now
    refused rather than stored, so nobody is left believing they have it.

    invite10 covers the mirror case (a member cannot kick the owner), and
    invite7 covers an admin using the permission legitimately, so the
    "endpoint refuses everything" reading this scenario used to rule out is
    still ruled out.
    """
    a, b, c = env.peers("A", "B", "C")
    ch = invite_only_channel(a, [b, c], "b11-private")

    a.set_permissions(ch, admin=ADMIN_DEFAULT, member=MEMBER_DEFAULT + [KICK])
    settled, _ = settle(lambda: KICK in c.permissions(ch)["member"],
                        "the kick grant to reach C", DISCOVERY_TIMEOUT)
    if settled:
        raise ScenarioFailure("kick was granted to the member role")

    if c.set_roles(ch, remove_members=[b.hash]):
        raise ScenarioFailure("a member removed another member")
    hold_for(lambda: b.hash in roster(a, ch),
             "B to stay in the owner's roster", 10.0)
    return {"kick_offered_to_members": False, "remaining": len(roster(a, ch))}


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


@scenario("invite14", "A promoted admin's kick takes effect everywhere")
def b14(env):
    """The rank invite6 and invite11 leave untested: an owner's kick works, a
    member's dies on the wire, and an admin -- a trusted signer holding KICK --
    is the highest rank below owner where the grant must demonstrably hold."""
    a, b, c, d = env.peers("A", "B", "C", "D")
    ch = invite_only_channel(a, [b, c, d], "b14-private")

    if not a.set_roles(ch, add_admins=[b.hash]):
        raise ScenarioFailure("promotion was rejected")
    # Every peer must hold the promotion before B publishes, or B's document
    # arrives from a signer that peer's stored list does not yet trust.
    wait_until(lambda: all(roster(p, ch).get(b.hash) == "admin" for p in (a, b, c, d)),
               "every peer to see B as admin", DISCOVERY_TIMEOUT)

    if not b.set_roles(ch, remove_members=[d.hash]):
        raise ScenarioFailure("the admin's kick was rejected locally")
    wait_until(lambda: all(d.hash not in roster(p, ch) for p in (a, b, c)),
               "D to be dropped from every remaining roster", DISCOVERY_TIMEOUT)

    d.send(ch, "after-admin-kick")
    hold_for(lambda: all("after-admin-kick" not in p.contents(ch) for p in (a, b, c)),
             "the kicked member's message to stay rejected", NEGATIVE_HOLD_SECS)
    return {"remaining": len(roster(a, ch))}


@scenario("invite15", "An admin granted manage_channel can edit permissions")
def b15(env):
    """invite13's refusal only means something if a granted edit demonstrably
    works. The owner opens manage_channel to admins; B's documents -- signed by
    an admin, not the owner -- must be applied by every peer, both the
    revocation and the re-grant that restores the silenced member."""
    a, b, c = env.peers("A", "B", "C")
    ch = invite_only_channel(a, [b, c], "b15-private")

    granted_admin = ADMIN_DEFAULT + [MANAGE_CHANNEL]
    if not a.set_permissions(ch, admin=granted_admin, member=MEMBER_DEFAULT):
        raise ScenarioFailure("the owner's manage_channel grant was rejected")
    a.set_roles(ch, add_admins=[b.hash])
    # B may only publish once every peer trusts it as a signer holding the
    # grant -- a document arriving ahead of the promotion is dropped for good.
    wait_until(lambda: all(roster(p, ch).get(b.hash) == "admin" for p in (a, b, c)),
               "every peer to see B as admin", DISCOVERY_TIMEOUT)
    wait_until(lambda: all(MANAGE_CHANNEL in p.permissions(ch)["admin"] for p in (b, c)),
               "every peer to hold the manage_channel grant", DISCOVERY_TIMEOUT)

    if not b.set_permissions(ch, admin=granted_admin, member=[]):
        raise ScenarioFailure("the admin's permission edit was rejected locally")
    wait_until(lambda: all(SEND_MESSAGE not in p.permissions(ch)["member"] for p in (a, c)),
               "the admin's revocation to be applied everywhere", DISCOVERY_TIMEOUT)

    c.send(ch, "while-silenced")
    hold_for(lambda: "while-silenced" not in a.contents(ch),
             "the silenced member's message to stay rejected", NEGATIVE_HOLD_SECS)

    if not b.set_permissions(ch, admin=granted_admin, member=MEMBER_DEFAULT):
        raise ScenarioFailure("the admin's re-grant was rejected locally")
    wait_until(lambda: SEND_MESSAGE in c.permissions(ch)["member"],
               "C to see send_message restored", DISCOVERY_TIMEOUT)

    c.send(ch, "after-regrant")
    all_hold([a, b], ch, {"after-regrant"}, timeout=DISCOVERY_TIMEOUT)
    return {}


@scenario("invite16", "A member granted invite can admit a new peer", kind=PROBE)
def b16(env):
    """The invite-path twin of invite11. The grant passes every local check --
    the token verifies, the join request is honoured -- but the admission
    document is published by the inviter, and a plain member is not a trusted
    signer. Prediction: the document is rejected everywhere, including by C's
    own _accept_document, so D's token is spent for no membership at all."""
    a, b, c, d = env.peers("A", "B", "C", "D")
    ch = invite_only_channel(a, [b, c], "b16-private",
                             permissions=(ADMIN_DEFAULT, MEMBER_DEFAULT + [INVITE]))
    wait_until(lambda: INVITE in c.permissions(ch)["member"],
               "C to hold the invite grant", DISCOVERY_TIMEOUT)

    c.invite(ch, d.hash)
    offer_invite(c, d, ch)
    d.accept_invite(ch)

    admitted_on_inviter, _ = settle(lambda: d.hash in roster(c, ch),
                                    "the inviter to admit D locally", 45.0)
    if admitted_on_inviter:
        admitted_everywhere, _ = settle(
            lambda: d.hash in roster(a, ch) and d.hash in roster(b, ch),
            "the owner and B to admit D", 45.0,
        )
    else:
        admitted_everywhere = False

    notes = {
        "admitted_on_inviter": admitted_on_inviter,
        "admitted_everywhere": admitted_everywhere,
        "views": roster_views([a, b, c, d], ch),
    }
    if admitted_everywhere:
        notes["surprise"] = "a member-published admission document was accepted"
    return notes


@scenario("invite17", "Leaving an invite-only channel is noticed by the peers", kind=PROBE)
def b17(env):
    """leave_channel() unsubscribes locally and notifies the creator's
    *subscriber* set -- nothing publishes a member-list update, so the
    prediction is that the departure is invisible to everyone else: every
    roster keeps the leaver and senders keep addressing them. The leaver's
    own is_subscribed gate is what actually goes quiet."""
    a, b, c = env.peers("A", "B", "C")
    ch = invite_only_channel(a, [b, c], "b17-private")
    a.send(ch, "before-leave")
    all_hold([b, c], ch, {"before-leave"}, timeout=DISCOVERY_TIMEOUT)

    if not c.leave(ch):
        raise ScenarioFailure("leave was refused")
    a.send(ch, "after-leave")
    all_hold([b], ch, {"before-leave", "after-leave"}, timeout=DISCOVERY_TIMEOUT)

    leaver_received, _ = settle(lambda: "after-leave" in c.contents(ch),
                                "the leaver to receive a post-leave send",
                                NEGATIVE_HOLD_SECS)
    dropped, _ = settle(
        lambda: c.hash not in roster(a, ch) and c.hash not in roster(b, ch),
        "the rosters to drop the leaver", 45.0,
    )
    notes = {
        "rosters_dropped_leaver": dropped,
        "leaver_received_after_leaving": leaver_received,
        "views": roster_views([a, b, c], ch),
    }
    if dropped:
        notes["surprise"] = "the departure did propagate to the rosters"
    return notes


@scenario("invite18", "A kicked member can be re-invited")
def b18(env):
    """A kick revokes the target's outstanding invite tokens at every peer,
    and invite_revoked_at is deliberately a moment rather than a flag so that
    a fresh invite issued after the kick still works. This is the flow that
    keeps moderation reversible."""
    a, b, c = env.peers("A", "B", "C")
    ch = invite_only_channel(a, [b, c], "b18-private")

    if not a.set_roles(ch, remove_members=[c.hash]):
        raise ScenarioFailure("the kick was rejected")
    wait_until(lambda: all(c.hash not in roster(p, ch) for p in (a, b)),
               "C to be dropped everywhere", DISCOVERY_TIMEOUT)

    a.invite(ch, c.hash)
    invite_and_accept(a, c, ch)
    agreed = rosters_identical([a, b, c], ch, timeout=DISCOVERY_TIMEOUT)
    if agreed.get(c.hash) != "member":
        raise ScenarioFailure(
            f"unexpected roles after re-admission: {roster_views([a, b, c], ch)}"
        )

    a.send(ch, "welcome-back")
    all_hold([b, c], ch, {"welcome-back"}, timeout=DISCOVERY_TIMEOUT)
    return {"members": len(agreed)}


@scenario("invite19", "A re-invited member gets their channel back")
def b19(env):
    """Leaving drops the subscription and keeps the channels row, and the
    subscribe on re-admission was gated on that row being absent -- so a
    returning member was admitted to a channel that never came back to their
    sidebar, and every listing filters on the subscription."""
    a, b = env.peers("A", "B")
    ch = invite_only_channel(a, [b], "b19-private")

    b.leave(ch)
    wait_until(lambda: ch not in joined_hashes(b), "B to drop the channel",
               DISCOVERY_TIMEOUT)
    if not a.set_roles(ch, remove_members=[b.hash]):
        raise ScenarioFailure("the removal was rejected")
    wait_until(lambda: b.hash not in roster(a, ch), "A to drop B",
               DISCOVERY_TIMEOUT)

    a.invite(ch, b.hash)
    invite_and_accept(a, b, ch)

    wait_until(lambda: ch in joined_hashes(b),
               "the re-invited channel to return to B's sidebar", DISCOVERY_TIMEOUT)
    a.send(ch, "welcome-back")
    all_hold([b], ch, {"welcome-back"}, timeout=DISCOVERY_TIMEOUT)
    return {}


@scenario("invite20", "A roster change lost with the sender's queue still converges")
def b20(env):
    """A membership document is sent once, and the queue that holds one for an
    unreachable peer is in memory. A peer that was down when the only copy went
    out, and whose sender then restarted, stayed behind on every other node's
    roster with no way back but a fresh invite -- so an announce now re-sends
    the current document to a member, version-ordered and idempotent.

    Killing B rather than dropping its link is the point: LXMF's own outbound
    queue recovers a dropped link on its own, and restarting A is what takes
    the retry queue with it.
    """
    a, b, c = env.peers("A", "B", "C")
    ch = invite_only_channel(a, [b, c], "b20-private")

    env.orch.kill(b.tag)
    if not a.set_roles(ch, add_admins=[c.hash]):
        raise ScenarioFailure("the promotion was rejected")
    wait_until(lambda: roster(a, ch).get(c.hash) == "admin",
               "A to record C as an admin", DISCOVERY_TIMEOUT)

    env.orch.restart(a.tag)
    env.orch.start(b.tag)
    env.wait_alive(a)
    env.wait_alive(b)

    wait_until(lambda: roster(b, ch).get(c.hash) == "admin",
               "B to converge on the roster change it was down for",
               RECONNECT_TIMEOUT)
    return {"role": roster(b, ch).get(c.hash)}
